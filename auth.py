"""Team logins with roles.

Roles, from least to most:
  reviewer  look at every course, read and download files, try built courses, and send change requests on a design
  designer  everything a reviewer can, plus start briefs, approve designs, run the paid steps, edit, and talk to the team
  admin     everything, plus add and remove team members and download backups

Passwords are stored as scrypt hashes (Python's standard library, no extra service). A sign-in creates a
random session code kept in an HttpOnly cookie; only its SHA-256 is stored in the database. New members
and password resets use one-time "set your password" links that an Admin shares.

Before the first account exists, the old DASHBOARD_KEY link opens the one-time owner setup page.
Once someone has signed up, the key no longer opens anything by itself.

Course links in emails keep working without a sign-in: each course has its own random code
(see jobs.links), exactly as before.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from urllib.parse import quote, urlparse

from starlette.requests import Request

import db

ROLES = {"reviewer": 1, "designer": 2, "admin": 3}
ROLE_NAMES = {"reviewer": "Reviewer", "designer": "Designer", "admin": "Admin"}
ROLE_HELP = {
    "reviewer": "Can look at every course, download files, try built courses and request changes to a design. Can't spend money or edit.",
    "designer": "Can do all the course work: start briefs, approve designs, run the paid steps, edit courses and talk to the team.",
    "admin": "Everything a Designer can do, plus add or remove team members and download backups.",
}
COOKIE = "modulo_session"
SESSION_DAYS = int(os.environ.get("MODULO_SESSION_DAYS", "30"))
INVITE_DAYS = 7
MIN_PASSWORD = 10
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------- passwords

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${h.hex()}"


def check_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        _, n, r, p, salt, h = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=len(h) // 2)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), h)


def password_problem(password: str) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"Please use at least {MIN_PASSWORD} characters. A short sentence works well."
    if password.lower() in {"password123", "modulo12345", "1234567890", "qwertyuiop"}:
        return "That password is too easy to guess."
    return None


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------- users

def has_users() -> bool:
    return bool(db.one("SELECT COUNT(*) AS n FROM users")["n"])


def users() -> list[dict]:
    return db.query("SELECT id, email, name, role, active, created, last_login, pw_hash IS NOT NULL AS has_password, "
                    "invite_expires FROM users ORDER BY created")


def get_user(user_id: str) -> dict | None:
    return db.one("SELECT * FROM users WHERE id = ?", (user_id,))


def by_email(email: str) -> dict | None:
    return db.one("SELECT * FROM users WHERE email = ?", (email.strip().lower(),))


def active_admins() -> int:
    return db.one("SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND active = 1")["n"]


def create_user(email: str, name: str, role: str, password: str | None = None) -> tuple[dict, str | None]:
    """Adds a member. Without a password, returns a one-time set-password code to share with them."""
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise ValueError("Please give a valid email address.")
    if role not in ROLES:
        raise ValueError("Please pick a role.")
    if by_email(email):
        raise ValueError("Someone with that email is already on the team.")
    if password is not None and (why := password_problem(password)):
        raise ValueError(why)
    uid = secrets.token_hex(8)
    code = None if password else secrets.token_urlsafe(24)
    db.execute("INSERT INTO users (id, email, name, role, pw_hash, active, created, invite_hash, invite_expires) "
               "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
               (uid, email, name.strip()[:80] or email, role, hash_password(password) if password else None, time.time(),
                _sha(code) if code else None, time.time() + INVITE_DAYS * 86400 if code else None))
    return get_user(uid), code


def new_invite(user_id: str) -> str:
    """A fresh one-time set-password link code (for a new member, or a forgotten password)."""
    code = secrets.token_urlsafe(24)
    db.execute("UPDATE users SET invite_hash = ?, invite_expires = ? WHERE id = ?",
               (_sha(code), time.time() + INVITE_DAYS * 86400, user_id))
    return code


def user_for_invite(code: str) -> dict | None:
    if not code:
        return None
    u = db.one("SELECT * FROM users WHERE invite_hash = ?", (_sha(code),))
    if not u or not u["active"] or (u["invite_expires"] or 0) < time.time():
        return None
    return u


def set_password(user_id: str, password: str) -> None:
    if why := password_problem(password):
        raise ValueError(why)
    with db.tx() as c:
        c.execute("UPDATE users SET pw_hash = ?, invite_hash = NULL, invite_expires = NULL WHERE id = ?",
                  (hash_password(password), user_id))
        c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))  # signs out everywhere else


def update_user(user_id: str, role: str | None = None, active: bool | None = None, name: str | None = None) -> None:
    u = get_user(user_id)
    if not u:
        raise ValueError("That team member was not found.")
    new_role = role if role is not None else u["role"]
    new_active = int(active) if active is not None else u["active"]
    if new_role not in ROLES:
        raise ValueError("Please pick a role.")
    if u["role"] == "admin" and u["active"] and (new_role != "admin" or not new_active) and active_admins() <= 1:
        raise ValueError("There must always be at least one Admin. Make someone else an Admin first.")
    with db.tx() as c:
        c.execute("UPDATE users SET role = ?, active = ?, name = ? WHERE id = ?",
                  (new_role, new_active, (name or u["name"]).strip()[:80], user_id))
        if not new_active:
            c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


# ---------------------------------------------------------------- sessions

def start_session(user_id: str) -> str:
    code = secrets.token_urlsafe(32)
    now = time.time()
    with db.tx() as c:
        c.execute("INSERT INTO sessions (id_hash, user_id, created, expires) VALUES (?, ?, ?, ?)",
                  (_sha(code), user_id, now, now + SESSION_DAYS * 86400))
        c.execute("UPDATE users SET last_login = ? WHERE id = ?", (now, user_id))
        c.execute("DELETE FROM sessions WHERE expires < ?", (now,))
    return code


def end_session(code: str) -> None:
    if code:
        db.execute("DELETE FROM sessions WHERE id_hash = ?", (_sha(code),))


def set_cookie(response, request: Request, code: str) -> None:
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(COOKIE, code, max_age=SESSION_DAYS * 86400, httponly=True, secure=secure, samesite="lax", path="/")


def clear_cookie(response) -> None:
    response.delete_cookie(COOKIE, path="/")


def current_user(request: Request) -> dict | None:
    if "user" in request.scope.get("state", {}):
        return request.state.user
    user = None
    code = request.cookies.get(COOKIE, "")
    if code:
        row = db.one("SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id_hash = ? AND s.expires > ?",
                     (_sha(code), time.time()))
        if row and row["active"]:
            user = row
    request.state.user = user
    return user


def can(user: dict | None, need: str) -> bool:
    return bool(user) and ROLES.get(user["role"], 0) >= ROLES[need]


def who(request: Request) -> str | None:
    u = current_user(request)
    return u["name"] if u else None


def same_origin(request: Request) -> bool:
    """Blocks other websites from submitting forms on a signed-in member's behalf."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return True
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return True  # old browsers; the SameSite cookie still protects
    return urlparse(origin).netloc == request.headers.get("host", request.url.netloc)


def login_url(request: Request) -> str:
    nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    nxt = re.sub(r"([?&])key=[^&]*&?", r"\1", nxt).rstrip("?&")
    return "/login?next=" + quote(nxt, safe="")


def safe_next(nxt: str | None) -> str:
    nxt = nxt or "/dashboard"
    return nxt if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt else "/dashboard"


# ---------------------------------------------------------------- sign-in throttling

_fails: dict[str, list[float]] = {}


def too_many_attempts(key: str) -> bool:
    now = time.time()
    recent = [t for t in _fails.get(key, []) if now - t < 900]
    _fails[key] = recent
    return len(recent) >= 8


def record_failure(key: str) -> None:
    _fails.setdefault(key, []).append(time.time())
