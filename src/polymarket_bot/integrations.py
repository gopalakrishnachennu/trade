"""LLM provider integrations: encrypted key store, connection test, chat call.

Keys:
  * arrive only over HTTPS with the dashboard header token,
  * are stored only in data/secrets/integrations.enc (mode 0600, dir 0700),
    encrypted with Fernet (AES-128-CBC + HMAC-SHA256) using a server-side key in
    data/secrets/master.key (0600). This protects a leaked copy of the key file
    (backup, archive, screenshot of a directory); it does not protect against
    root on the droplet, which can read both files.
  * are never returned to the browser (masked to the last 4 characters),
    never logged, never written to events, archives or git.

Only endpoints documented by each provider are used. Cursor is listed but has
no public chat-completions API, so it cannot be connected as a reviewer.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

PROVIDERS: dict[str, dict[str, Any]] = {
    "openai": {
        "label": "OpenAI (GPT / Codex models)", "kind": "openai", "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini", "test": "GET /models",
        "notes": "Platform API key from platform.openai.com. Test lists models (no tokens billed).",
    },
    "anthropic": {
        "label": "Anthropic (Claude)", "kind": "anthropic", "base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-3-5-haiku-latest", "test": "GET /models",
        "notes": "API key from console.anthropic.com. Test lists models (no tokens billed).",
    },
    "gemini": {
        "label": "Google Gemini", "kind": "openai", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_model": "gemini-2.0-flash", "test": "GET /models",
        "notes": "AI Studio key; uses Google's OpenAI-compatible endpoint.",
    },
    "xai": {
        "label": "xAI (Grok)", "kind": "openai", "base_url": "https://api.x.ai/v1",
        "default_model": "grok-3-mini", "test": "GET /models",
        "notes": "API key from console.x.ai (OpenAI-compatible).",
    },
    "openrouter": {
        "label": "OpenRouter", "kind": "openai", "base_url": "https://openrouter.ai/api/v1",
        "default_model": "openai/gpt-4o-mini", "test": "GET /key",
        "notes": "One key for many models. Test reads the key's own status (GET /key).",
    },
    "custom": {
        "label": "OpenAI-compatible (custom base URL)", "kind": "openai", "base_url": "",
        "default_model": "", "test": "GET /models",
        "notes": "Any server that implements /v1/models and /v1/chat/completions (vLLM, Ollama, LM Studio, Together, Groq…). Base URL must be https:// (or http://127.0.0.1).",
    },
    "cursor": {
        "label": "Cursor", "kind": "none", "base_url": "",
        "default_model": "", "test": "n/a",
        "notes": "Cursor has no public chat-completions API that a server can call with a key, so it cannot act as the reviewer. Use OpenAI, Anthropic or OpenRouter instead.",
    },
}

_KEY_PAT = re.compile(r"(sk-[A-Za-z0-9_\-]{3,}|xai-[A-Za-z0-9_\-]{3,}|AIza[0-9A-Za-z_\-]{10,}|[A-Za-z0-9_\-]{32,})")


def scrub(text: str, key: Optional[str] = None) -> str:
    s = str(text or "")
    if key:
        s = s.replace(key, "***")
    return _KEY_PAT.sub("***", s)[:400]


def mask(key: str) -> str:
    return "••••" + key[-4:] if key and len(key) >= 8 else "••••"


class KeyStore:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir) / "secrets"
        self.enc_path = self.dir / "integrations.enc"
        self.key_path = self.dir / "master.key"
        self.meta_path = Path(data_dir) / "integrations.json"  # non-secret metadata only

    # ---- crypto
    def _ensure_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)

    def _fernet(self):
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            return None
        self._ensure_dir()
        if not self.key_path.exists():
            fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        os.chmod(self.key_path, 0o600)
        return Fernet(self.key_path.read_bytes().strip())

    def encrypted(self) -> bool:
        return self._fernet() is not None

    def _load(self) -> dict[str, dict]:
        if not self.enc_path.exists():
            return {}
        raw = self.enc_path.read_bytes()
        f = self._fernet()
        if f is not None and not raw.startswith(b"{"):
            raw = f.decrypt(raw)
        return json.loads(raw.decode())

    def _save(self, data: dict[str, dict]) -> None:
        self._ensure_dir()
        body = json.dumps(data).encode()
        f = self._fernet()
        if f is not None:
            body = f.encrypt(body)
        tmp = self.enc_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.enc_path)
        os.chmod(self.enc_path, 0o600)

    # ---- metadata (no secrets)
    def meta(self) -> dict[str, Any]:
        try:
            return json.loads(self.meta_path.read_text())
        except Exception:
            return {"providers": {}, "reviewer": {"provider": None, "model": None}}

    def _save_meta(self, meta: dict) -> None:
        tmp = self.meta_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, indent=2))
        os.replace(tmp, self.meta_path)

    # ---- public API
    def set(self, provider: str, key: str, base_url: Optional[str] = None, model: Optional[str] = None) -> dict:
        if provider not in PROVIDERS or PROVIDERS[provider]["kind"] == "none":
            raise ValueError(f"provider {provider!r} cannot be connected")
        key = (key or "").strip()
        if len(key) < 8 or len(key) > 400 or any(c.isspace() for c in key):
            raise ValueError("key looks malformed")
        base = (base_url or PROVIDERS[provider]["base_url"] or "").strip().rstrip("/")
        if provider == "custom":
            if not (base.startswith("https://") or base.startswith("http://127.0.0.1") or base.startswith("http://localhost")):
                raise ValueError("base URL must be https:// (or http://127.0.0.1 / localhost)")
        data = self._load()
        data[provider] = {"key": key, "base_url": base}
        self._save(data)
        meta = self.meta()
        meta.setdefault("providers", {})[provider] = {
            "connected": True, "last4": key[-4:], "base_url": base,
            "model": (model or PROVIDERS[provider]["default_model"] or "").strip()[:120],
            "connected_at": time.time(), "last_test": None,
        }
        self._save_meta(meta)
        return self.public()["providers"][provider]

    def delete(self, provider: str) -> None:
        data = self._load()
        data.pop(provider, None)
        self._save(data)
        meta = self.meta()
        meta.get("providers", {}).pop(provider, None)
        if (meta.get("reviewer") or {}).get("provider") == provider:
            meta["reviewer"] = {"provider": None, "model": None}
        self._save_meta(meta)

    def secret(self, provider: str) -> Optional[dict]:
        return self._load().get(provider)

    def set_reviewer(self, provider: Optional[str], model: Optional[str]) -> dict:
        meta = self.meta()
        if provider:
            if provider not in (meta.get("providers") or {}):
                raise ValueError("connect the provider first")
            model = (model or meta["providers"][provider].get("model") or PROVIDERS[provider]["default_model"]).strip()[:120]
            if not re.fullmatch(r"[A-Za-z0-9._:/\-@]+", model or ""):
                raise ValueError("model name has invalid characters")
        meta["reviewer"] = {"provider": provider or None, "model": model if provider else None}
        self._save_meta(meta)
        return meta["reviewer"]

    def record_test(self, provider: str, result: dict) -> None:
        meta = self.meta()
        if provider in meta.get("providers", {}):
            meta["providers"][provider]["last_test"] = {k: result.get(k) for k in ("ok", "status", "detail", "latency_ms", "ts")}
            self._save_meta(meta)

    def public(self) -> dict[str, Any]:
        meta = self.meta()
        prov = {}
        for pid, spec in PROVIDERS.items():
            m = (meta.get("providers") or {}).get(pid) or {}
            prov[pid] = {
                "id": pid, "label": spec["label"], "kind": spec["kind"], "notes": spec["notes"],
                "default_model": spec["default_model"], "test": spec["test"],
                "connectable": spec["kind"] != "none",
                "connected": bool(m.get("connected")),
                "masked": ("••••" + m["last4"]) if m.get("last4") else None,
                "base_url": m.get("base_url") or spec["base_url"],
                "model": m.get("model") or spec["default_model"],
                "connected_at": m.get("connected_at"),
                "last_test": m.get("last_test"),
            }
        return {"providers": prov, "reviewer": meta.get("reviewer") or {"provider": None, "model": None},
                "encrypted_at_rest": self.encrypted()}


# ----------------------------------------------------------------- HTTP

def _http(method: str, url: str, headers: dict, body: Optional[dict] = None, timeout: float = 15.0) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "User-Agent": "polymarket-paper-bot/1.0", "Accept": "application/json",
        **({"Content-Type": "application/json"} if data else {}), **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read(2_000_000).decode(errors="replace")
            try:
                return r.status, json.loads(txt)
            except ValueError:
                return r.status, txt
    except urllib.error.HTTPError as e:
        txt = e.read(20_000).decode(errors="replace")
        try:
            return e.code, json.loads(txt)
        except ValueError:
            return e.code, txt


def _auth(provider: str, key: str) -> dict:
    if PROVIDERS[provider]["kind"] == "anthropic":
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}
    return {"Authorization": f"Bearer {key}"}


def test_connection(store: KeyStore, provider: str) -> dict:
    t0 = time.time()
    sec = store.secret(provider)
    if not sec:
        return {"ok": False, "status": None, "detail": "not connected", "latency_ms": 0, "ts": time.time()}
    key, base = sec["key"], sec["base_url"]
    path = "/key" if provider == "openrouter" else "/models"
    try:
        status, body = _http("GET", base + path, _auth(provider, key), timeout=12)
        ok = 200 <= status < 300
        if ok:
            n = len(body.get("data") or []) if isinstance(body, dict) else 0
            detail = f"authenticated; {n} models visible" if path == "/models" else "authenticated"
        else:
            msg = body.get("error") if isinstance(body, dict) else body
            if isinstance(msg, dict):
                msg = msg.get("message") or msg.get("type")
            detail = f"HTTP {status}: {scrub(str(msg), key)}"
    except Exception as e:  # network
        status, ok, detail = None, False, scrub(f"{type(e).__name__}: {e}", key)
    res = {"ok": ok, "status": status, "detail": detail, "latency_ms": int((time.time() - t0) * 1000), "ts": time.time()}
    store.record_test(provider, res)
    return res


def chat(store: KeyStore, provider: str, model: str, system: str, user: str, timeout: float = 90.0) -> str:
    """One chat completion; returns the text. Raises RuntimeError with a scrubbed message."""
    sec = store.secret(provider)
    if not sec:
        raise RuntimeError(f"{provider} not connected")
    key, base = sec["key"], sec["base_url"]
    kind = PROVIDERS[provider]["kind"]
    try:
        if kind == "anthropic":
            status, body = _http("POST", base + "/messages", _auth(provider, key), {
                "model": model, "max_tokens": 1500, "system": system,
                "messages": [{"role": "user", "content": user}], "temperature": 0}, timeout=timeout)
            if status != 200:
                raise RuntimeError(f"HTTP {status}: {scrub(json.dumps(body)[:300], key)}")
            return "".join(b.get("text", "") for b in body.get("content") or [] if b.get("type") == "text")
        if kind == "openai":
            status, body = _http("POST", base + "/chat/completions", _auth(provider, key), {
                "model": model, "temperature": 0, "max_tokens": 1500,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}, timeout=timeout)
            if status != 200:
                raise RuntimeError(f"HTTP {status}: {scrub(json.dumps(body)[:300], key)}")
            return body["choices"][0]["message"]["content"] or ""
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(scrub(f"{type(e).__name__}: {e}", key))
    raise RuntimeError(f"{provider} has no chat API")
