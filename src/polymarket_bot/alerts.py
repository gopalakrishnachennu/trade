"""Alert hooks (Telegram, email). Disabled until configured.

Configuration lives in data/secrets/alerts.json (mode 0600, never committed):
  {"telegram": {"bot_token": "...", "chat_id": "..."},
   "email": {"smtp_host": "...", "smtp_port": 587, "username": "...", "password": "...",
             "from": "...", "to": "..."}}
With no file (the default) every notify() is a no-op and Health shows
"alerts: disabled". Failures are swallowed (alerts never affect trading).
Rate limit: the same message key at most once per 15 minutes.
"""

from __future__ import annotations

import json
import smtplib
import ssl
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

_last: dict[str, float] = {}
RATE_S = 900


def _cfg(data_dir: Path) -> dict:
    try:
        return json.loads((Path(data_dir) / "secrets" / "alerts.json").read_text())
    except Exception:
        return {}


def status(data_dir: Path) -> dict:
    c = _cfg(data_dir)
    return {"telegram": bool((c.get("telegram") or {}).get("bot_token")), "email": bool((c.get("email") or {}).get("smtp_host"))}


def notify(data_dir: Path, key: str, text: str, now: Optional[float] = None, *, wait: bool = False) -> list[str]:
    """Send in a background thread (never blocks the supervisor loop). Returns enabled channels."""
    now = now or time.time()
    st = status(data_dir)
    chans = [k for k, v in st.items() if v]
    if not chans or now - _last.get(key, 0) < RATE_S:
        return []
    _last[key] = now
    import threading
    th = threading.Thread(target=_send, args=(data_dir, text), daemon=True)
    th.start()
    if wait:
        th.join(30)
    return chans


def _send(data_dir: Path, text: str) -> list[str]:
    c = _cfg(data_dir)
    sent = []
    tg = c.get("telegram") or {}
    if tg.get("bot_token") and tg.get("chat_id"):
        try:
            # Documented Bot API method: https://core.telegram.org/bots/api#sendmessage
            data = urllib.parse.urlencode({"chat_id": tg["chat_id"], "text": text[:3500]}).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage", data=data, timeout=10).read()
            sent.append("telegram")
        except Exception:
            pass
    em = c.get("email") or {}
    if em.get("smtp_host") and em.get("to"):
        try:
            msg = EmailMessage()
            msg["Subject"] = f"[polymarket paper bot] {text[:80]}"
            msg["From"] = em.get("from") or em.get("username")
            msg["To"] = em["to"]
            msg.set_content(text)
            with smtplib.SMTP(em["smtp_host"], int(em.get("smtp_port") or 587), timeout=15) as s:
                s.starttls(context=ssl.create_default_context())
                if em.get("username"):
                    s.login(em["username"], em.get("password") or "")
                s.send_message(msg)
            sent.append("email")
        except Exception:
            pass
    return sent
