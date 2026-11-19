"""Sends email through Resend (https://resend.com).

Needs RESEND_API_KEY and NOTIFY_EMAIL. Without a key, emails are written to
DATA_DIR/outbox/ instead, so the rest of the service still works.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path

import httpx

log = logging.getLogger("modulo.email")

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent / "data"))
# onboarding@resend.dev works straight away, but only for sending to the email
# address you signed up to Resend with. Verify a domain in Resend to change it.
EMAIL_FROM = os.environ.get("EMAIL_FROM") or "Modulo course team <onboarding@resend.dev>"


def notify_address() -> str:
    return os.environ.get("NOTIFY_EMAIL", "").strip()


def can_email_clients() -> bool:
    """Resend's shared test sender (onboarding@resend.dev) only delivers to the Resend account's own address.
    Emails to clients need a verified domain in Resend and EMAIL_FROM set to an address on it."""
    return bool(os.environ.get("RESEND_API_KEY", "").strip()) and "resend.dev" not in EMAIL_FROM.lower()


def send(subject: str, html: str, attachments: list[Path] | None = None, to: str | None = None) -> bool:
    """Email the studio owner, or `to` (a client) when given. Returns True if the email was accepted."""
    to = to or notify_address()
    payload = {
        "from": EMAIL_FROM,
        "to": [to] if to else [],
        "subject": subject,
        "html": html,
        "attachments": [
            {"filename": p.name, "content": base64.b64encode(p.read_bytes()).decode()}
            for p in (attachments or []) if p.exists()
        ],
    }
    key = os.environ.get("RESEND_API_KEY", "").strip()
    if not key or not to:
        outbox = DATA_DIR / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{abs(hash(subject)) % 10000:04d}"
        (outbox / f"{name}.html").write_text(html, encoding="utf-8")
        (outbox / f"{name}.json").write_text(json.dumps(
            {**payload, "attachments": [a["filename"] for a in payload["attachments"]]}, indent=2))
        log.warning("Email not sent (RESEND_API_KEY or NOTIFY_EMAIL missing). Saved to %s", outbox / name)
        return False
    for attempt in range(3):
        try:
            r = httpx.post("https://api.resend.com/emails", json=payload,
                           headers={"Authorization": f"Bearer {key}"}, timeout=30)
            if r.status_code < 300:
                return True
            log.error("Resend refused the email (%s): %s", r.status_code, r.text[:500])
            if r.status_code < 500 and r.status_code != 429:
                return False
        except httpx.HTTPError as e:
            log.error("Could not reach Resend: %s", e)
        time.sleep(2 * (attempt + 1))
    return False
