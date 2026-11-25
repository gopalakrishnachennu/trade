"""Size-triggered archiving of live data into data/archive/part-NNNN-<range>.tar.zst.

Design (see OPERATIONS.md › Archiving):
  * The trading ledger stays continuous: data/paper_trades.sqlite is never
    replaced. Open/settled orders are never removed from it.
  * Each archive contains a consistent VACUUM INTO snapshot of the sqlite ledger,
    every CSV row dated before today (ET) moved out of the live CSVs (under the
    same flock the writers use), rotated event files, and copy-truncated logs.
  * A manifest (part-NNNN-….manifest.json) lists every file with sha256, row
    count, byte size and time range, plus the archive's own sha256.
  * Nothing is deleted until the Mac acknowledges a part with the matching
    sha256 (POST /api/archives/<name>/ack). After an ack:
      - old *rejected* orders and old equity snapshots (before the part's
        cutoff date) are pruned from the live sqlite (they are in the snapshot);
      - the archive file itself is deleted after ACK_KEEP_DAYS more days.
    Unacknowledged parts are never deleted.
"""

from __future__ import annotations

import csv
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

THRESHOLD_BYTES = int(float(os.environ.get("ARCHIVE_THRESHOLD_MB", "300")) * 1024 * 1024)
ACK_KEEP_DAYS = float(os.environ.get("ARCHIVE_ACK_KEEP_DAYS", "3"))
NAME_RE = re.compile(r"^part-(\d{4})-(\d{8})-(\d{8})\.tar\.(zst|gz)$")


def archive_dir(data_dir: Path) -> Path:
    d = Path(data_dir) / "archive"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def live_files(data_dir: Path) -> list[Path]:
    data_dir = Path(data_dir)
    out: list[Path] = []
    for pat in ("*.csv", "*.sqlite", "*.jsonl"):
        out += sorted(data_dir.glob(pat))
    rt = data_dir / "runtime"
    out += sorted(rt.glob("events*.jsonl*"))
    out += sorted(rt.glob("*.log"))
    out += sorted((rt / "rotated").glob("*")) if (rt / "rotated").exists() else []
    sh = data_dir / "strategies" / "shadow"
    if sh.exists():
        out += sorted(p for p in sh.rglob("*") if p.is_file())
    pt = data_dir / "patterns"
    if pt.exists():
        out += sorted(pt.glob("evaluations.jsonl*"))
    return [p for p in out if p.is_file()]


def live_bytes(data_dir: Path) -> int:
    return sum(p.stat().st_size for p in live_files(data_dir))


def _sqlite_bytes(data_dir: Path) -> int:
    return sum(p.stat().st_size for p in Path(data_dir).glob("*.sqlite"))


def trigger_bytes(data_dir: Path) -> int:
    """Bytes that count toward the threshold: everything movable, plus sqlite growth since the last snapshot.

    The live sqlite ledger is never moved, so its full size would re-trigger forever.
    """
    total = live_bytes(data_dir)
    sq = _sqlite_bytes(data_dir)
    base = int(read_state(data_dir).get("sqlite_bytes_at_last") or 0)
    return total - sq + max(0, sq - base)


def append_csv_locked(path: Path, fields: list[str], row: dict) -> None:
    """Append one row under an exclusive flock (the archiver takes the same lock)."""
    with path.open("a", newline="") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            if f.tell() == 0:
                csv.DictWriter(f, fieldnames=fields).writeheader()
            csv.DictWriter(f, fieldnames=fields).writerow(row)
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _row_day(ts: str, tz: ZoneInfo) -> Optional[str]:
    s = (ts or "").strip()
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s[:10] if re.match(r"\d{4}-\d{2}-\d{2}", s) else None
    if d.tzinfo is None:
        d = d.replace(tzinfo=tz)
    return d.astimezone(tz).date().isoformat()


def split_csv(path: Path, dest: Path, today: str, tz: ZoneInfo) -> dict[str, Any]:
    """Move rows dated before `today` from `path` to `dest` (in place, under flock)."""
    info = {"rows": 0, "ts_min": None, "ts_max": None}
    with path.open("r+", newline="") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            text = f.read()
            if not text:
                return info
            lines = text.splitlines(keepends=True)
            header, body = lines[0], lines[1:]
            fields = next(csv.reader([header]))
            ts_i = fields.index("ts") if "ts" in fields else 0
            old, keep = [], []
            for ln in body:
                try:
                    vals = next(csv.reader([ln]))
                    day = _row_day(vals[ts_i] if ts_i < len(vals) else "", tz)
                except Exception:
                    day = None
                if day is not None and day < today:
                    old.append(ln)
                    info["ts_min"] = day if info["ts_min"] is None else min(info["ts_min"], day)
                    info["ts_max"] = day if info["ts_max"] is None else max(info["ts_max"], day)
                else:
                    keep.append(ln)
            if not old:
                return info
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("w", newline="") as out:
                out.write(header)
                out.writelines(old)
                out.flush()
                os.fsync(out.fileno())
            f.seek(0)
            f.write(header)
            f.writelines(keep)
            f.truncate()
            f.flush()
            os.fsync(f.fileno())
            info["rows"] = len(old)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return info


def _copytruncate(src: Path, dest: Path) -> dict[str, Any]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with src.open("rb+") as f:
        data = f.read()
        dest.write_bytes(data)
        f.seek(0)
        f.truncate()
    return {"rows": data.count(b"\n")}


def _sqlite_snapshot(db: Path, dest: Path) -> dict[str, Any]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db), timeout=10)
    try:
        con.execute("VACUUM INTO ?", (str(dest),))
    finally:
        con.close()
    s = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    try:
        by = dict(s.execute("SELECT status, COUNT(*) FROM paper_orders GROUP BY status").fetchall())
        rng = s.execute("SELECT MIN(ts), MAX(ts) FROM paper_orders").fetchone()
        eq = s.execute("SELECT COUNT(*) FROM equity").fetchone()[0]
        ok = s.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        s.close()
    return {"rows": sum(by.values()), "orders_by_status": by, "equity_rows": eq,
            "ts_min": rng[0], "ts_max": rng[1], "integrity": ok}


def _state_path(data_dir: Path) -> Path:
    return archive_dir(data_dir) / "state.json"


def read_state(data_dir: Path) -> dict[str, Any]:
    try:
        return json.loads(_state_path(data_dir).read_text())
    except Exception:
        return {}


def _write_json(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str))
    os.replace(tmp, path)


def _update_state(data_dir: Path, **kw) -> dict:
    st = read_state(data_dir)
    st.update(kw)
    _write_json(_state_path(data_dir), st)
    return st


def list_archives(data_dir: Path) -> list[dict[str, Any]]:
    out = []
    for p in sorted(archive_dir(data_dir).glob("part-*.tar.*")):
        if not NAME_RE.match(p.name):
            continue
        man = manifest_path(p)
        m = json.loads(man.read_text()) if man.exists() else {}
        out.append({"name": p.name, "bytes": p.stat().st_size, "sha256": m.get("sha256"),
                    "created": m.get("created"), "range": m.get("range"), "acked_at": m.get("acked_at"),
                    "pruned_at": m.get("pruned_at"), "files": m.get("files", []),
                    "manifest": man.name if man.exists() else None})
    return out


def manifest_path(tar: Path) -> Path:
    return tar.with_name(re.sub(r"\.tar\.(zst|gz)$", ".manifest.json", tar.name))


def resolve_name(data_dir: Path, name: str) -> Optional[Path]:
    """Map a client-supplied name to a file in data/archive (archives and manifests only)."""
    if not (NAME_RE.match(name) or re.match(r"^part-\d{4}-\d{8}-\d{8}\.manifest\.json$", name)):
        return None
    p = archive_dir(data_dir) / name
    return p if p.is_file() else None


def run_archive(data_dir: Path, tz_name: str = "America/New_York", *, force: bool = False,
                threshold: Optional[int] = None, now: Optional[datetime] = None) -> Optional[dict]:
    data_dir = Path(data_dir)
    adir = archive_dir(data_dir)
    lock_fd = os.open(adir / ".archive.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        threshold = THRESHOLD_BYTES if threshold is None else threshold
        size = live_bytes(data_dir)
        trig = trigger_bytes(data_dir)
        st = _update_state(data_dir, last_check=time.time(), live_bytes=size, trigger_bytes=trig,
                           threshold_bytes=threshold)
        if not force and (trig < threshold or time.time() - float(st.get("last_run") or 0) < 3600):
            return None
        free = shutil.disk_usage(adir).free
        if free < size * 1.5 + 50 * 1024 * 1024:
            raise RuntimeError(f"not enough free disk to archive safely ({free/1e6:.0f} MB free, {size/1e6:.0f} MB live)")
        tz = ZoneInfo(tz_name)
        now = now or datetime.now(tz)
        today = now.astimezone(tz).date().isoformat()
        nums = [int(m.group(1)) for p in adir.glob("part-*") if (m := re.match(r"part-(\d{4})-", p.name))]
        part = max(nums, default=0) + 1
        stage = adir / f".staging-{part:04d}"
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir()
        files: list[dict] = []
        days: list[str] = []

        def add(rel: str, info: dict) -> None:
            p = stage / rel
            if not p.exists():
                return
            files.append({"path": rel, "bytes": p.stat().st_size, "sha256": _sha256(p), **info})
            for k in ("ts_min", "ts_max"):
                v = info.get(k)
                if v:
                    days.append(str(v)[:10])

        # 1. CSV rows before today (main book + shadow books)
        csvs = sorted(data_dir.glob("*.csv"))
        sh = data_dir / "strategies" / "shadow"
        if sh.exists():
            csvs += sorted(sh.rglob("*.csv"))
        for c in csvs:
            rel = str(c.relative_to(data_dir))
            info = split_csv(c, stage / rel, today, tz)
            if info["rows"]:
                add(rel, info)
        # 2. rotated event files (moved), current events are kept live
        rt = data_dir / "runtime"
        for p in sorted((rt / "rotated").glob("*")) if (rt / "rotated").exists() else []:
            rel = str(p.relative_to(data_dir))
            (stage / rel).parent.mkdir(parents=True, exist_ok=True)
            os.replace(p, stage / rel)
            add(rel, {"rows": (stage / rel).read_bytes().count(b"\n")})
        legacy = rt / "events.jsonl.1"
        if legacy.exists():
            rel = f"runtime/rotated/events-legacy-{int(time.time())}.jsonl"
            (stage / rel).parent.mkdir(parents=True, exist_ok=True)
            os.replace(legacy, stage / rel)
            add(rel, {"rows": (stage / rel).read_bytes().count(b"\n")})
        # 3. logs: copy-truncate (writers use O_APPEND)
        for p in sorted(rt.glob("*.log")):
            if p.stat().st_size > 0:
                rel = f"runtime/logs/{p.name}"
                add_info = _copytruncate(p, stage / rel)
                add(rel, add_info)
        # 4. consistent ledger snapshot (ledger itself stays live and continuous)
        for db in sorted(data_dir.glob("*.sqlite")):
            rel = f"sqlite/{db.stem}.snapshot.sqlite"
            info = _sqlite_snapshot(db, stage / rel)
            add(rel, info)
        d0 = min(days) if days else today
        d1 = max(days) if days else today
        name = f"part-{part:04d}-{d0.replace('-', '')}-{d1.replace('-', '')}"
        manifest = {
            "part": part, "name": name, "created": now.isoformat(timespec="seconds"),
            "range": {"start": d0, "end": d1}, "cutoff_day": today,
            "files": files, "live_bytes_before": size,
            "prune_after_ack": {"sqlite": "rejected orders and equity snapshots with ts < cutoff_day"},
            "acked_at": None, "pruned_at": None,
        }
        _write_json(stage / "MANIFEST.json", manifest)
        zstd = shutil.which("zstd")
        ext = "zst" if zstd else "gz"
        tar_path = adir / f"{name}.tar.{ext}"
        tmp = tar_path.with_suffix(tar_path.suffix + ".tmp")
        comp = [zstd, "-q", "-6", "-T1"] if zstd else ["gzip", "-6"]
        with tmp.open("wb") as out:
            p1 = subprocess.Popen(["tar", "-C", str(stage), "-cf", "-", "."], stdout=subprocess.PIPE)
            p2 = subprocess.run(comp, stdin=p1.stdout, stdout=out, check=False)
            p1.stdout.close()
            rc1 = p1.wait()
            if rc1 != 0 or p2.returncode != 0:
                raise RuntimeError(f"tar/compress failed rc={rc1}/{p2.returncode}")
            out.flush()
            os.fsync(out.fileno())
        # verify the archive lists cleanly before removing the staging copy
        dec = [zstd, "-dc"] if zstd else ["gzip", "-dc"]
        p1 = subprocess.Popen(dec + [str(tmp)], stdout=subprocess.PIPE)
        listing = subprocess.run(["tar", "-tf", "-"], stdin=p1.stdout, capture_output=True, text=True)
        p1.stdout.close()
        p1.wait()
        if listing.returncode != 0 or "./MANIFEST.json" not in listing.stdout:
            raise RuntimeError("archive verification failed")
        os.replace(tmp, tar_path)
        manifest["sha256"] = _sha256(tar_path)
        manifest["bytes"] = tar_path.stat().st_size
        manifest["archive"] = tar_path.name
        _write_json(manifest_path(tar_path), manifest)
        shutil.rmtree(stage)
        _update_state(data_dir, last_run=time.time(), last_part=tar_path.name, last_error=None,
                      live_bytes=live_bytes(data_dir), sqlite_bytes_at_last=_sqlite_bytes(data_dir),
                      trigger_bytes=trigger_bytes(data_dir))
        return manifest
    except Exception as e:
        _update_state(data_dir, last_error=f"{type(e).__name__}: {e}", last_error_at=time.time())
        raise
    finally:
        os.close(lock_fd)


def ack(data_dir: Path, name: str, sha256: str, sqlite_path: Optional[Path] = None) -> dict:
    tar = resolve_name(data_dir, name)
    if tar is None or not NAME_RE.match(name):
        raise ValueError("unknown archive")
    man_p = manifest_path(tar)
    man = json.loads(man_p.read_text())
    if not sha256 or sha256.lower() != str(man.get("sha256")).lower():
        raise ValueError("sha256 does not match; not acknowledged")
    if not man.get("acked_at"):
        man["acked_at"] = time.time()
        _write_json(man_p, man)
    if not man.get("pruned_at"):
        pruned = prune_sqlite(sqlite_path or (Path(data_dir) / "paper_trades.sqlite"), man["cutoff_day"])
        man["pruned_at"] = time.time()
        man["pruned"] = pruned
        _write_json(man_p, man)
    _update_state(data_dir, last_ack=time.time(), last_ack_part=name)
    return man


def prune_sqlite(db: Path, cutoff_day: str) -> dict:
    """Delete rejected orders and equity snapshots older than cutoff_day. Never touches open/settled orders."""
    if not db.exists():
        return {}
    con = sqlite3.connect(str(db), timeout=10)
    try:
        r1 = con.execute("DELETE FROM paper_orders WHERE status='rejected' AND substr(ts,1,10) < ?", (cutoff_day,)).rowcount
        r2 = con.execute("DELETE FROM equity WHERE substr(ts,1,10) < ?", (cutoff_day,)).rowcount
        con.commit()
    finally:
        con.close()
    return {"rejected_orders": r1, "equity_rows": r2}


def prune_acked(data_dir: Path, keep_days: float = ACK_KEEP_DAYS, now: Optional[float] = None) -> list[str]:
    """Delete archive files acknowledged more than keep_days ago. Unacked parts are never deleted."""
    now = now or time.time()
    gone = []
    for a in list_archives(data_dir):
        if a.get("acked_at") and now - float(a["acked_at"]) > keep_days * 86400:
            tar = archive_dir(data_dir) / a["name"]
            man_p = manifest_path(tar)
            man = json.loads(man_p.read_text())
            man["file_deleted_at"] = now
            # keep the manifest as a record (tiny); delete only the acked tarball
            done = archive_dir(data_dir) / "acked"
            done.mkdir(exist_ok=True)
            _write_json(done / man_p.name, man)
            tar.unlink(missing_ok=True)
            man_p.unlink(missing_ok=True)
            gone.append(a["name"])
    return gone


def unacked_age_hours(data_dir: Path, now: Optional[float] = None) -> Optional[float]:
    now = now or time.time()
    ages = []
    for a in list_archives(data_dir):
        if not a.get("acked_at"):
            try:
                created = datetime.fromisoformat(a["created"]).timestamp()
            except Exception:
                created = (archive_dir(data_dir) / a["name"]).stat().st_mtime
            ages.append((now - created) / 3600)
    return max(ages) if ages else None
