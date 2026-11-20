"""Cleo, the marketing agent: social posts about Modulo for LinkedIn (and X, when switched on).

  /marketing                         drafts, scheduled and posted posts, accounts and settings (Designers and Admins)
  /marketing/act                     the page's buttons (POST)
  /marketing/connect/<platform>      Admins: sign in to LinkedIn or X so Modulo may post for you
  /marketing/callback/<platform>     where LinkedIn or X sends you back after signing in

Safe by default: Cleo only writes drafts. A post goes out when an Admin presses "Approve and post",
or at the time an Admin scheduled it. Auto-post (off unless an Admin switches it on) lets Cleo's
weekly posts be scheduled without that review; they can still be edited or cancelled until they go out.

Posts are stored in the `posts` table. Sign-in tokens are kept in the `meta` table under
`social_token_<platform>` and are left out of backups (see db.export_all).

Keys (Render environment):
  LINKEDIN_CLIENT_ID, LINKEDIN_CLIENT_SECRET   LinkedIn developer app with "Share on LinkedIn" and
                                               "Sign In with LinkedIn using OpenID Connect"; posts to your own profile
  X_CLIENT_ID, X_CLIENT_SECRET                 optional: X developer app (OAuth 2.0, pay-per-use credit)
  LINKEDIN_VERSION                             optional: LinkedIn API version, YYYYMM (default below)
  MODULO_MARKETING_MODEL                       optional: model Cleo writes with (default claude-opus-5-5)
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import html
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anthropic
import httpx
from starlette.requests import Request
from starlette.responses import RedirectResponse
from starlette.routing import Route

import auth
import db
import emailer
import jobs

log = logging.getLogger("modulo.marketing")
HERE = Path(__file__).resolve().parent
MODEL = os.environ.get("MODULO_MARKETING_MODEL", "claude-opus-5-5")
LINKEDIN_VERSION = os.environ.get("LINKEDIN_VERSION", "202610")
FAKE = os.environ.get("MODULO_FAKE", "").lower() in {"1", "true", "yes"}
TIMEOUT = httpx.Timeout(30.0)

PLATFORMS = {"linkedin": "LinkedIn", "x": "X"}
LIMITS = {"linkedin": 3000, "x": 280}
STATUS_NAMES = {"draft": "Draft", "scheduled": "Scheduled", "posting": "Posting now", "posted": "Posted",
                "failed": "Didn't post", "discarded": "Discarded"}

DEFAULTS = {
    "weekly": True,        # Cleo writes the coming week's posts every Monday morning (one small Claude call)
    "per_week": 3,         # posts per week, per connected platform
    "auto_post": False,    # schedule Cleo's weekly posts without review. Off until an Admin switches it on.
    "post_hour": 9,        # the time of day scheduled posts go out
    "tz": "UTC",
    "links_linkedin": True,
    "links_x": False,      # a link in an X post costs about 13 times more on X's pay-per-use pricing
    "notes": "",           # standing notes for Cleo: offers, themes, things to avoid
}

POSTS_SCHEMA = """CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    status TEXT NOT NULL,
    text TEXT NOT NULL,
    angle TEXT,
    created DOUBLE PRECISION NOT NULL,
    updated DOUBLE PRECISION NOT NULL,
    scheduled_at DOUBLE PRECISION,
    posted_at DOUBLE PRECISION,
    ext_id TEXT,
    url TEXT,
    error TEXT,
    made_by TEXT,
    approved_by TEXT,
    auto INTEGER NOT NULL DEFAULT 0
)"""


def _A():
    import app
    return app


# ---------------------------------------------------------------- settings and accounts

def settings() -> dict:
    try:
        saved = json.loads(db.get_meta("marketing_settings") or "{}")
    except json.JSONDecodeError:
        saved = {}
    return {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}


def save_settings(s: dict) -> None:
    db.set_meta("marketing_settings", json.dumps({k: s[k] for k in DEFAULTS if k in s}))


def zone(s: dict | None = None) -> ZoneInfo:
    try:
        return ZoneInfo((s or settings())["tz"])
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo("UTC")


def _env(name: str) -> str:
    return os.environ.get(name, "").strip().strip('"').strip("'").strip()


def configured(platform: str) -> bool:
    """The developer app keys are in Render."""
    pre = "LINKEDIN" if platform == "linkedin" else "X"
    return bool(_env(f"{pre}_CLIENT_ID") and _env(f"{pre}_CLIENT_SECRET"))


def token(platform: str) -> dict | None:
    raw = db.get_meta(f"social_token_{platform}")
    if not raw:
        return None
    try:
        t = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return t if t.get("access_token") else None


def _save_token(platform: str, t: dict | None) -> None:
    if t is None:
        db.execute("DELETE FROM meta WHERE key = ?", (f"social_token_{platform}",))
    else:
        db.set_meta(f"social_token_{platform}", json.dumps(t))


def connected(platform: str) -> bool:
    """Signed in, and (for LinkedIn, which has no refresh) not expired."""
    t = token(platform)
    if not t or not configured(platform):
        return False
    if platform == "linkedin" and t.get("expires_at", 0) < time.time():
        return False
    return not t.get("broken")


def available() -> list[str]:
    """Platforms Cleo writes for: LinkedIn always (you can draft before connecting), X only once its keys are added."""
    return [p for p in PLATFORMS if p == "linkedin" or configured(p)]


def redirect_uri(platform: str) -> str:
    return f"{jobs.public_url()}/marketing/callback/{platform}"


# ---------------------------------------------------------------- posts

def init() -> None:
    db.execute(POSTS_SCHEMA if db.is_postgres() else POSTS_SCHEMA.replace("DOUBLE PRECISION", "REAL"))
    db.execute("CREATE INDEX IF NOT EXISTS posts_status ON posts (status)")
    # A post that was being sent when the service restarted may or may not have gone out. Never send it twice
    # by ourselves: mark it so an Admin can check LinkedIn or X and retry if needed.
    db.execute("UPDATE posts SET status = 'failed', error = ?, updated = ? WHERE status = 'posting'",
               ("The service restarted while this was being posted. Check your profile: if it isn't there, press Retry.",
                time.time()))


def get_post(post_id: str) -> dict | None:
    return db.one("SELECT * FROM posts WHERE id = ?", (post_id,))


def posts(statuses: tuple[str, ...], order: str = "created DESC", limit: int = 100) -> list[dict]:
    marks = ", ".join("?" * len(statuses))
    return db.query(f"SELECT * FROM posts WHERE status IN ({marks}) ORDER BY {order} LIMIT {int(limit)}", list(statuses))


def add_post(platform: str, text: str, angle: str = "", made_by: str = "Cleo", auto: bool = False) -> dict:
    now = time.time()
    p = {"id": secrets.token_hex(6), "platform": platform, "status": "draft", "text": text.strip(), "angle": angle,
         "created": now, "updated": now, "made_by": made_by, "auto": 1 if auto else 0}
    db.execute("INSERT INTO posts (id, platform, status, text, angle, created, updated, made_by, auto) "
               "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (p["id"], platform, "draft", p["text"], angle, now, now, made_by, p["auto"]))
    return p


def update_post(post_id: str, **fields) -> None:
    fields["updated"] = time.time()
    cols = ", ".join(f"{k} = ?" for k in fields)
    db.execute(f"UPDATE posts SET {cols} WHERE id = ?", [*fields.values(), post_id])


URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)


def x_length(text: str) -> int:
    """X's weighted count, closely enough: links count as 23, most non-Latin characters and emoji as 2."""
    text = URL_RE.sub("x" * 23, text)
    n = 0
    for ch in text:
        c = ord(ch)
        light = c <= 0x10FF or 0x2000 <= c <= 0x200D or 0x2010 <= c <= 0x201F or 0x2032 <= c <= 0x2037
        n += 1 if light else 2
    return n


def length(platform: str, text: str) -> int:
    return x_length(text) if platform == "x" else len(text)


def problems(platform: str, text: str) -> list[str]:
    out = []
    if not text.strip():
        out.append("The post is empty.")
    if length(platform, text) > LIMITS[platform]:
        out.append(f"Too long for {PLATFORMS[platform]}: {length(platform, text)} of {LIMITS[platform]} characters.")
    return out


# ---------------------------------------------------------------- Cleo writes posts

def _about_modulo() -> str:
    """The website's own words, so Cleo only says what the site says."""
    try:
        raw = (HERE / "public" / "index.html").read_text(encoding="utf-8")
    except OSError:
        return "Modulo is an eLearning studio run by a team of AI agents, with a human owner approving each step."
    raw = re.sub(r"(?is)<(script|style|svg)\b.*?</\1>", " ", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", text).strip()[:7000]


def _instructions() -> str:
    return (HERE / "pipeline" / "agents" / "marketing_manager.md").read_text(encoding="utf-8")


def _schema(platforms: list[str]) -> dict:
    return {
        "type": "object", "additionalProperties": False, "required": ["posts"],
        "properties": {"posts": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["platform", "angle", "text"],
            "properties": {"platform": {"type": "string", "enum": platforms},
                           "angle": {"type": "string", "description": "a few words naming the post's angle"},
                           "text": {"type": "string"}}}}},
    }


_FAKE_POSTS = {
    "linkedin": ("Behind the scenes", "Most training projects stall at the same place: waiting for the first draft.\n\n"
                 "At Modulo, our team of AI agents turns a short chat into a course design in hours. Then I read it and "
                 "approve it before anything else happens.\n\nSpeed from the agents, judgement from a human.\n\n"
                 "What slows your course projects down the most?\n\n#eLearning #InstructionalDesign"),
    "x": ("Tip", "A good quiz question tests a decision, not a definition. Ask what the learner would do next. #eLearning"),
}


async def write_drafts(platforms: list[str], count: int, note: str = "", made_by: str = "Cleo",
                       auto: bool = False) -> list[dict]:
    """Ask Cleo for `count` posts per platform and save them as drafts."""
    s = settings()
    if FAKE:
        await asyncio.sleep(float(os.environ.get("MODULO_FAKE_SECONDS", "1")))
        return [await asyncio.to_thread(add_post, p, _FAKE_POSTS[p][1], _FAKE_POSTS[p][0], made_by, auto)
                for p in platforms for _ in range(count)]
    recent = await asyncio.to_thread(posts, ("draft", "scheduled", "posted"), "created DESC", 12)
    links = [f"{PLATFORMS[p]}: {'links allowed, use ' + jobs.public_url() if s['links_' + p] else 'no links'}" for p in platforms]
    prompt = (
        f"<about_modulo>\n{_about_modulo()}\n</about_modulo>\n\n"
        f"Write {count} post(s) for each of these platforms: {', '.join(PLATFORMS[p] for p in platforms)}.\n"
        f"Website links: {'; '.join(links)}.\n"
        + (f"\nThe owner's standing notes for you:\n{s['notes'].strip()}\n" if s["notes"].strip() else "")
        + (f"\nThe owner's note for this batch:\n{note.strip()}\n" if note.strip() else "")
        + ("\nRecent posts (don't repeat their angles or opening lines):\n"
           + "\n".join(f"- [{PLATFORMS.get(r['platform'], r['platform'])}] {r['text'][:160]}" for r in recent)
           if recent else "")
    )
    client = anthropic.AsyncAnthropic()
    response = await client.beta.messages.create(
        model=MODEL, max_tokens=8000,
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": _schema(platforms)}},
        system=_instructions(),
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Cleo declined to write these posts. Try a different note.")
    data = json.loads("".join(b.text for b in response.content if b.type == "text"))
    made = []
    for item in data.get("posts", []):
        p, text = item.get("platform"), (item.get("text") or "").strip().replace("—", ", ")
        if p in platforms and text:
            made.append(await asyncio.to_thread(add_post, p, text, (item.get("angle") or "")[:60], made_by, auto))
    log.info("Cleo wrote %d drafts (%s)", len(made), ", ".join(platforms))
    return made


_writing: asyncio.Task | None = None


def writing() -> bool:
    return _writing is not None and not _writing.done()


def start_writing(platforms: list[str], count: int, note: str, who: str) -> bool:
    """Run write_drafts in the background so the page doesn't hang. False if Cleo is already writing."""
    global _writing
    if writing():
        return False
    db.execute("DELETE FROM meta WHERE key = ?", ("marketing_write_error",))

    async def run():
        try:
            await write_drafts(platforms, count, note, made_by=f"Cleo, asked by {who}")
        except Exception as e:  # noqa: BLE001
            log.exception("Cleo could not write drafts")
            msg = "Your Claude key was not accepted." if isinstance(e, anthropic.AuthenticationError) else str(e)[:300]
            await asyncio.to_thread(db.set_meta, "marketing_write_error", msg)

    _writing = asyncio.get_running_loop().create_task(run(), name="cleo-writing")
    return True


# ---------------------------------------------------------------- posting to LinkedIn

_LITTLE = re.compile(r"([|{}@\[\]()<>#\\*_~])")
_HASHTAG = re.compile(r"(?<![\w&])#(\w+)")


def linkedin_commentary(text: str) -> str:
    """LinkedIn's "little text" format: reserved characters must be escaped or the post is cut off there.
    Hashtags become LinkedIn hashtags."""
    out, last = [], 0
    for m in _HASHTAG.finditer(text):
        out.append(_LITTLE.sub(r"\\\1", text[last:m.start()]))
        out.append("{hashtag|\\#|" + _LITTLE.sub(r"\\\1", m.group(1)) + "}")
        last = m.end()
    out.append(_LITTLE.sub(r"\\\1", text[last:]))
    return "".join(out)


class PostError(Exception):
    pass


def _api_error(platform: str, r: httpx.Response) -> PostError:
    try:
        detail = r.json()
        detail = detail.get("message") or detail.get("detail") or detail.get("title") or json.dumps(detail)[:300]
    except ValueError:
        detail = r.text[:300]
    name = PLATFORMS[platform]
    if r.status_code == 401:
        _mark_broken(platform)
        return PostError(f"{name} sign-in has expired or was withdrawn. Press Connect {name} on the Marketing page, then Retry.")
    if r.status_code == 402 or (platform == "x" and "credit" in str(detail).lower()):
        return PostError(f"X says the account has no credit left. Add credit in the X developer console, then Retry. ({detail})")
    if r.status_code == 429:
        return PostError(f"{name} says too many posts for now. Try again later. ({detail})")
    if platform == "linkedin" and "version" in str(detail).lower():
        return PostError(f"LinkedIn didn't accept API version {LINKEDIN_VERSION}. Set LINKEDIN_VERSION in Render to a "
                         f"recent month (YYYYMM). ({detail})")
    return PostError(f"{name} said no ({r.status_code}): {detail}")


def _mark_broken(platform: str) -> None:
    t = token(platform)
    if t:
        t["broken"] = True
        _save_token(platform, t)


async def post_linkedin(text: str) -> tuple[str, str]:
    t = token("linkedin")
    if not t or not connected("linkedin"):
        raise PostError("LinkedIn isn't connected. Press Connect LinkedIn on the Marketing page.")
    body = {"author": t["author"], "commentary": linkedin_commentary(text), "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False}
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post("https://api.linkedin.com/rest/posts", json=body, headers={
            "Authorization": f"Bearer {t['access_token']}", "LinkedIn-Version": LINKEDIN_VERSION,
            "X-Restli-Protocol-Version": "2.0.0"})
    if r.status_code not in (200, 201):
        raise _api_error("linkedin", r)
    urn = r.headers.get("x-restli-id") or r.headers.get("x-linkedin-id") or ""
    return urn, f"https://www.linkedin.com/feed/update/{urn}/" if urn else "https://www.linkedin.com/in/me/recent-activity/all/"


# ---------------------------------------------------------------- posting to X

_x_lock = asyncio.Lock()


def _x_basic() -> str:
    return "Basic " + base64.b64encode(f"{_env('X_CLIENT_ID')}:{_env('X_CLIENT_SECRET')}".encode()).decode()


async def _x_access() -> str:
    """A fresh X access token. X's tokens last 2 hours; each refresh gives a new refresh token (the old one stops working)."""
    async with _x_lock:
        t = await asyncio.to_thread(token, "x")
        if not t or t.get("broken"):
            raise PostError("X isn't connected. Press Connect X on the Marketing page.")
        if t.get("expires_at", 0) > time.time() + 300:
            return t["access_token"]
        if not t.get("refresh_token"):
            await asyncio.to_thread(_mark_broken, "x")
            raise PostError("X sign-in has expired. Press Connect X on the Marketing page, then Retry.")
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.post("https://api.x.com/2/oauth2/token", headers={"Authorization": _x_basic()}, data={
                "grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": _env("X_CLIENT_ID")})
        if r.status_code != 200:
            log.error("X token refresh failed %s: %s", r.status_code, r.text[:300])
            await asyncio.to_thread(_mark_broken, "x")
            raise PostError("X sign-in has expired. Press Connect X on the Marketing page, then Retry.")
        new = r.json()
        t.update(access_token=new["access_token"], refresh_token=new.get("refresh_token", t["refresh_token"]),
                 expires_at=time.time() + int(new.get("expires_in", 7200)))
        await asyncio.to_thread(_save_token, "x", t)
        return t["access_token"]


async def post_x(text: str) -> tuple[str, str]:
    access = await _x_access()
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post("https://api.x.com/2/tweets", json={"text": text}, headers={"Authorization": f"Bearer {access}"})
    if r.status_code not in (200, 201):
        raise _api_error("x", r)
    pid = r.json().get("data", {}).get("id", "")
    user = (token("x") or {}).get("username", "i")
    return pid, f"https://x.com/{user}/status/{pid}"


# ---------------------------------------------------------------- publishing

async def publish(post_id: str, who: str | None = None) -> tuple[bool, str]:
    """Send one post. Only one caller can claim it, so a double click or the scheduler can't post it twice."""
    claimed = await asyncio.to_thread(
        db.execute, "UPDATE posts SET status = 'posting', updated = ? WHERE id = ? AND status IN ('draft', 'scheduled', 'failed')",
        (time.time(), post_id))
    if not claimed:
        return False, "This post was already sent or is being sent."
    p = await asyncio.to_thread(get_post, post_id)
    try:
        if issues := problems(p["platform"], p["text"]):
            raise PostError(" ".join(issues))
        if FAKE:
            ext, url = "fake-" + post_id, "#"
        elif p["platform"] == "linkedin":
            ext, url = await post_linkedin(p["text"])
        else:
            ext, url = await post_x(p["text"])
    except PostError as e:
        await asyncio.to_thread(update_post, post_id, status="failed", error=str(e))
        return False, str(e)
    except Exception as e:  # noqa: BLE001  network trouble and the like
        log.exception("Posting %s failed", post_id)
        msg = f"Couldn't reach {PLATFORMS[p['platform']]}: {e}. Check your profile before pressing Retry."
        await asyncio.to_thread(update_post, post_id, status="failed", error=msg[:500])
        return False, msg
    fields = {"status": "posted", "posted_at": time.time(), "ext_id": ext, "url": url, "error": None}
    if who:
        fields["approved_by"] = who
    await asyncio.to_thread(update_post, post_id, **fields)
    log.info("Posted %s to %s: %s", post_id, p["platform"], url)
    return True, url


def slots(s: dict, start: dt.datetime, n_platform_posts: int) -> list[float]:
    """Posting times for the week starting at `start` (local time): spread over weekdays at post_hour."""
    days = {1: [1], 2: [1, 3], 3: [0, 2, 4], 4: [0, 1, 3, 4], 5: [0, 1, 2, 3, 4], 6: [0, 1, 2, 3, 4, 5],
            7: [0, 1, 2, 3, 4, 5, 6]}[max(1, min(7, n_platform_posts))]
    monday = (start - dt.timedelta(days=start.weekday())).replace(hour=int(s["post_hour"]), minute=0, second=0, microsecond=0)
    out = []
    for d in days:
        when = monday + dt.timedelta(days=d)
        if when <= start + dt.timedelta(minutes=5):
            when += dt.timedelta(days=7)
        out.append(when.timestamp())
    return sorted(out)


def _email(subject: str, body: str) -> None:
    try:
        emailer.send(subject, jobs._wrap(body))
    except Exception:  # noqa: BLE001
        log.exception("Marketing email failed")


async def weekly_batch(now: dt.datetime | None = None) -> int:
    """Monday morning: Cleo writes the week's posts for each connected platform. With auto-post on they are
    scheduled straight away; otherwise they wait as drafts and the owner gets an email."""
    s = await asyncio.to_thread(settings)
    plats = [p for p in PLATFORMS if await asyncio.to_thread(connected, p)]
    if not plats:
        return 0
    now = now or dt.datetime.now(zone(s))
    made = await write_drafts(plats, int(s["per_week"]), made_by="Cleo (weekly)", auto=s["auto_post"])
    link = f"{jobs.public_url()}/marketing"
    if s["auto_post"]:
        for p in plats:
            mine = [m for m in made if m["platform"] == p]
            for m, when in zip(mine, slots(s, now, len(mine))):
                await asyncio.to_thread(update_post, m["id"], status="scheduled", scheduled_at=when)
        await asyncio.to_thread(_email, f"Cleo scheduled {len(made)} posts for this week", (
            f"<p>Auto-post is on, so Cleo wrote and scheduled {len(made)} posts for this week. "
            "They go out by themselves at the times shown.</p>"
            f"<p>You can still read, edit or cancel any of them before then: <a href='{link}'>open Marketing</a>.</p>"))
    elif made:
        await asyncio.to_thread(_email, f"Cleo wrote {len(made)} posts for you to check", (
            f"<p>Cleo wrote {len(made)} draft posts for this week. Nothing is posted until you approve it.</p>"
            f"<p><a href='{link}'>Read and approve the drafts</a></p>"))
    return len(made)


def _iso_week(d: dt.datetime) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


async def tick(now_ts: float | None = None) -> None:
    """One pass of the background loop: post what's due, write the weekly batch, remind about expiring sign-ins."""
    now_ts = now_ts or time.time()
    due = await asyncio.to_thread(db.query, "SELECT id, platform FROM posts WHERE status = 'scheduled' AND scheduled_at <= ? "
                                  "ORDER BY scheduled_at", (now_ts,))
    for row in due:
        ok, msg = await publish(row["id"])
        if not ok and "already" not in msg:
            await asyncio.to_thread(_email, f"Modulo: a scheduled {PLATFORMS[row['platform']]} post didn't go out", (
                f"<p>A scheduled post could not be published:</p><p>{html.escape(msg)}</p>"
                f"<p><a href='{jobs.public_url()}/marketing'>Open Marketing</a> to fix it and press Retry.</p>"))
    s = await asyncio.to_thread(settings)
    local = dt.datetime.fromtimestamp(now_ts, zone(s))
    if s["weekly"] and (local.weekday() > 0 or local.hour >= 8):
        week = _iso_week(local)
        if await asyncio.to_thread(db.get_meta, "marketing_last_batch") != week:
            if any([await asyncio.to_thread(connected, p) for p in PLATFORMS]):
                await asyncio.to_thread(db.set_meta, "marketing_last_batch", week)  # first, so a failure isn't retried every minute
                try:
                    await weekly_batch(local)
                except Exception as e:  # noqa: BLE001
                    log.exception("Weekly marketing batch failed")
                    await asyncio.to_thread(_email, "Modulo: Cleo couldn't write this week's posts", (
                        f"<p>{html.escape(str(e)[:400])}</p><p>You can ask Cleo again on the "
                        f"<a href='{jobs.public_url()}/marketing'>Marketing page</a>.</p>"))
    t = await asyncio.to_thread(token, "linkedin")
    if t and not t.get("broken"):
        left = t.get("expires_at", 0) - now_ts
        key = f"marketing_reminded_{int(t.get('expires_at', 0))}"
        if 0 < left < 7 * 86400 and not await asyncio.to_thread(db.get_meta, key):
            await asyncio.to_thread(db.set_meta, key, "1")
            await asyncio.to_thread(_email, "Reconnect LinkedIn for Modulo this week", (
                f"<p>LinkedIn asks you to sign in again every 60 days. Your connection ends in {max(1, int(left // 86400))} days, "
                "and Cleo's posts can't go out after that.</p>"
                f"<p><a href='{jobs.public_url()}/marketing'>Open Marketing</a> and press <b>Reconnect LinkedIn</b>. It takes a minute.</p>"))


async def scheduler() -> None:
    """Runs forever inside the web server, once a minute. (init() runs at startup, before this.)"""
    while True:
        try:
            await tick()
        except Exception:  # noqa: BLE001
            log.exception("Marketing scheduler problem")
        await asyncio.sleep(60)


# ---------------------------------------------------------------- signing in to LinkedIn and X

def _new_state(platform: str, user_id: str, verifier: str = "") -> str:
    state = secrets.token_urlsafe(24)
    db.set_meta(f"oauth_state_{state}", json.dumps({"p": platform, "u": user_id, "exp": time.time() + 900, "v": verifier}))
    return state


def _take_state(state: str) -> dict | None:
    if not re.fullmatch(r"[\w-]{20,64}", state or ""):
        return None
    key = f"oauth_state_{state}"
    raw = db.get_meta(key)
    db.execute("DELETE FROM meta WHERE key = ?", (key,))
    if not raw:
        return None
    s = json.loads(raw)
    return s if s["exp"] > time.time() else None


async def connect(request: Request):
    A = _A()
    if blocked := A.gate(request, "admin"):
        return blocked
    platform = request.path_params["platform"]
    if platform not in PLATFORMS or not configured(platform):
        return RedirectResponse("/marketing?m=nokeys", 303)
    user = auth.current_user(request)
    if platform == "linkedin":
        state = await asyncio.to_thread(_new_state, platform, user["id"])
        q = {"response_type": "code", "client_id": _env("LINKEDIN_CLIENT_ID"), "redirect_uri": redirect_uri(platform),
             "state": state, "scope": "openid profile w_member_social"}
        return RedirectResponse("https://www.linkedin.com/oauth/v2/authorization?" + urlencode(q), 303)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = await asyncio.to_thread(_new_state, platform, user["id"], verifier)
    q = {"response_type": "code", "client_id": _env("X_CLIENT_ID"), "redirect_uri": redirect_uri(platform), "state": state,
         "scope": "tweet.read tweet.write users.read offline.access", "code_challenge": challenge,
         "code_challenge_method": "S256"}
    return RedirectResponse("https://x.com/i/oauth2/authorize?" + urlencode(q), 303)


async def callback(request: Request):
    A = _A()
    if blocked := A.gate(request, "admin"):
        return blocked
    platform = request.path_params["platform"]
    qp = request.query_params
    st = await asyncio.to_thread(_take_state, qp.get("state", ""))
    user = auth.current_user(request)
    if platform not in PLATFORMS or not st or st["p"] != platform or st["u"] != user["id"]:
        return RedirectResponse("/marketing?m=badstate", 303)
    if qp.get("error") or not qp.get("code"):
        return RedirectResponse("/marketing?m=cancelled", 303)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            if platform == "linkedin":
                r = await c.post("https://www.linkedin.com/oauth/v2/accessToken", data={
                    "grant_type": "authorization_code", "code": qp["code"], "redirect_uri": redirect_uri(platform),
                    "client_id": _env("LINKEDIN_CLIENT_ID"), "client_secret": _env("LINKEDIN_CLIENT_SECRET")})
                r.raise_for_status()
                tok = r.json()
                me = await c.get("https://api.linkedin.com/v2/userinfo",
                                 headers={"Authorization": f"Bearer {tok['access_token']}"})
                me.raise_for_status()
                info = me.json()
                t = {"access_token": tok["access_token"], "expires_at": time.time() + int(tok.get("expires_in", 5184000)),
                     "author": f"urn:li:person:{info['sub']}", "name": info.get("name", ""), "by": user["name"],
                     "connected": time.time()}
            else:
                r = await c.post("https://api.x.com/2/oauth2/token", headers={"Authorization": _x_basic()}, data={
                    "grant_type": "authorization_code", "code": qp["code"], "redirect_uri": redirect_uri(platform),
                    "code_verifier": st["v"], "client_id": _env("X_CLIENT_ID")})
                r.raise_for_status()
                tok = r.json()
                me = await c.get("https://api.x.com/2/users/me", headers={"Authorization": f"Bearer {tok['access_token']}"})
                me.raise_for_status()
                info = me.json().get("data", {})
                t = {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token", ""),
                     "expires_at": time.time() + int(tok.get("expires_in", 7200)), "username": info.get("username", ""),
                     "name": info.get("name", ""), "by": user["name"], "connected": time.time()}
    except (httpx.HTTPError, KeyError, ValueError) as e:
        body = getattr(getattr(e, "response", None), "text", "")[:300]
        log.error("Connecting %s failed: %s %s", platform, e, body)
        return RedirectResponse(f"/marketing?m=connectfail_{platform}", 303)
    await asyncio.to_thread(_save_token, platform, t)
    log.info("%s connected by %s as %s", PLATFORMS[platform], user["email"], t.get("name"))
    return RedirectResponse(f"/marketing?m=connected_{platform}", 303)


# ---------------------------------------------------------------- the page

MESSAGES = {
    "writing": ("good", "Cleo is writing. The drafts appear here in about a minute."),
    "busy": ("warn", "Cleo is already writing. The drafts appear here in a moment."),
    "saved": ("good", "Saved."),
    "settings": ("good", "Settings saved."),
    "discarded": ("good", "Draft discarded."),
    "scheduled": ("good", "Scheduled. It goes out by itself at that time."),
    "unscheduled": ("good", "Moved back to drafts. It won't go out until you approve it."),
    "posted": ("good", "Posted."),
    "postfail": ("bad", "It didn't post. The reason is shown on the post below."),
    "nokeys": ("warn", "Add this platform's keys in Render first (see the steps on this page)."),
    "badstate": ("bad", "That sign-in link was old or not yours. Press Connect again."),
    "cancelled": ("warn", "Sign-in was cancelled, nothing changed."),
    "connected_linkedin": ("good", "LinkedIn is connected. Posts you approve now go to your LinkedIn profile."),
    "connected_x": ("good", "X is connected."),
    "connectfail_linkedin": ("bad", "LinkedIn didn't finish connecting. Check that the app has the products "
                             "\"Share on LinkedIn\" and \"Sign In with LinkedIn using OpenID Connect\", and that the "
                             "redirect URL below is added exactly. Then try again."),
    "connectfail_x": ("bad", "X didn't finish connecting. Check that the X app uses OAuth 2.0 with Read and write, "
                      "type Web App, and that the callback URL below is added exactly. Then try again."),
    "disconnected": ("good", "Disconnected. Modulo can no longer post there."),
    "toolong": ("bad", "That's too long for the platform. Shorten it and save again."),
    "past": ("bad", "Pick a time in the future."),
    "nopost": ("bad", "That post was already sent, or no longer exists."),
    "confirm": ("bad", "To switch on auto-post, tick the box that says you understand posts will go out without review."),
    "notz": ("bad", "That time zone wasn't recognised. Use a name like Europe/London, America/New_York or Asia/Kolkata."),
}

PAGE_CSS = """
.post{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px 18px;margin:0 0 12px;box-shadow:var(--shadow)}
.post .top{display:flex;gap:8px;align-items:center;flex-wrap:wrap;justify-content:space-between;margin-bottom:8px}
.post .txt{white-space:pre-wrap;overflow-wrap:anywhere;margin:0 0 10px;font-size:15px}
.post .acts{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.post .acts form{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:0}
.post input[type=datetime-local]{width:auto}.post details{margin-top:8px}.post details summary{cursor:pointer;color:var(--accent);font-weight:600;font-size:14px}
.badge{font:700 12px/1 var(--body);padding:5px 9px;border-radius:999px;background:var(--surface-2);color:var(--fg)}
.badge.linkedin{background:#0A66C2;color:#fff}.badge.x{background:var(--fg);color:var(--bg)}
.acct{display:flex;gap:12px;justify-content:space-between;align-items:center;flex-wrap:wrap;padding:12px 0;border-bottom:1px solid var(--line)}
.acct:last-child{border-bottom:0}.count{font-size:13px;color:var(--muted)}.count.over{color:var(--bad);font-weight:600}
.cleo{display:flex;gap:14px;align-items:center}.cleo .face{flex:none;width:48px;height:48px;border-radius:50%;background:var(--mark);color:#13201B;display:grid;place-items:center;font:800 20px var(--display)}
code.copy{display:inline-block;background:var(--surface-2);padding:3px 8px;border-radius:8px;font-size:13px;overflow-wrap:anywhere}
ol.steps-help li{margin:4px 0}.switch{display:flex;gap:10px;align-items:flex-start;font-weight:500}.switch input{width:auto;margin-top:5px}
"""

COUNTER_JS = """<script>
document.querySelectorAll('textarea[data-limit]').forEach(t=>{const out=t.parentElement.querySelector('.count');const lim=+t.dataset.limit;
const x=t.dataset.platform==='x';const n=s=>x?[...s.replace(/https?:\\/\\/\\S+/g,'x'.repeat(23))].reduce((a,c)=>a+(c.codePointAt(0)<=0x10FF?1:2),0):s.length;
const f=()=>{const k=n(t.value);out.textContent=k+' of '+lim+' characters';out.classList.toggle('over',k>lim)};t.addEventListener('input',f);f()});
</script>"""


def _local(ts: float | None, tz: ZoneInfo) -> str:
    if not ts:
        return ""
    return dt.datetime.fromtimestamp(ts, tz).strftime("%a %d %b, %H:%M")


def _post_card(p: dict, admin: bool, tz: ZoneInfo, s: dict) -> str:
    e = html.escape
    pid, plat = e(p["id"]), p["platform"]
    n = length(plat, p["text"])
    over = n > LIMITS[plat]
    meta = [f"<span class='badge {plat}'>{PLATFORMS[plat]}</span>"]
    if p.get("angle"):
        meta.append(f"<span class=muted>{e(p['angle'])}</span>")
    when = ""
    if p["status"] == "scheduled":
        when = f"Goes out {_local(p['scheduled_at'], tz)}" + (" (auto-post)" if p.get("auto") else "")
    elif p["status"] == "posted":
        when = f"Posted {_local(p['posted_at'], tz)}" + (f" · approved by {e(p['approved_by'])}" if p.get("approved_by") else "")
    else:
        when = f"Written {_local(p['created'], tz)} by {e(p.get('made_by') or 'Cleo')}"
    head = (f"<div class=top><div class=row>{''.join(meta)}</div><span class=muted small>{e(when)}</span></div>"
            f"<p class=txt>{e(p['text'])}</p>")
    warn = []
    if over:
        warn.append(f"<p class=err small>Too long: {n} of {LIMITS[plat]} characters. Edit it before posting.</p>")
    if plat == "x" and URL_RE.search(p["text"]) and p["status"] in ("draft", "scheduled"):
        warn.append("<p class=muted small>This X post has a link, so X charges about 20 cents for it instead of 1.5 cents.</p>")
    if p["status"] == "failed" and p.get("error"):
        warn.append(f"<div class='note bad'><span class=ic>!</span><p>{e(p['error'])}</p></div>")
    acts = []
    hidden = f"<input type=hidden name=id value='{pid}'>"
    if p["status"] in ("draft", "failed", "scheduled") and admin:
        live = connected(plat)
        label = "Retry" if p["status"] == "failed" else ("Post now" if p["status"] == "scheduled" else "Approve and post")
        if live and not over:
            acts.append(f"<form method=post action='/marketing/act'>{hidden}<input type=hidden name=action value=post>"
                        f"<button class=sm>{label}</button></form>")
        elif not live:
            acts.append(f"<span class='muted small'>Connect {PLATFORMS[plat]} below to post this.</span>")
        default = dt.datetime.fromtimestamp(p["scheduled_at"] or slots(s, dt.datetime.now(tz), 1)[0], tz).strftime("%Y-%m-%dT%H:%M")
        if live and not over:
            acts.append(f"<form method=post action='/marketing/act'>{hidden}<input type=hidden name=action value=schedule>"
                        f"<input type=datetime-local name=when value='{default}' aria-label='Date and time to post' required>"
                        f"<button class='sm ghost'>{'Change time' if p['status'] == 'scheduled' else 'Schedule'}</button></form>")
        if p["status"] == "scheduled":
            acts.append(f"<form method=post action='/marketing/act'>{hidden}<input type=hidden name=action value=unschedule>"
                        "<button class='sm ghost'>Back to drafts</button></form>")
    if p["status"] in ("draft", "failed"):
        acts.append(f"<form method=post action='/marketing/act'>{hidden}<input type=hidden name=action value=discard>"
                    "<button class='sm link'>Discard</button></form>")
    if p["status"] == "posted" and p.get("url") and p["url"] != "#":
        acts.append(f"<a href='{e(p['url'])}' target=_blank rel=noopener>See it on {PLATFORMS[plat]}</a>")
    edit = ""
    if p["status"] in ("draft", "failed", "scheduled"):
        edit = (f"<details{' open' if over else ''}><summary>Edit</summary><form method=post action='/marketing/act'>{hidden}"
                f"<input type=hidden name=action value=edit><textarea name=text data-limit={LIMITS[plat]} data-platform={plat} "
                f"aria-label='Post text'>{e(p['text'])}</textarea><div class=row style='justify-content:space-between;margin-top:6px'>"
                "<span class=count></span><button class=sm>Save</button></div></form></details>")
    return f"<article class=post id='p{pid}'>{head}{''.join(warn)}<div class=acts>{''.join(acts)}</div>{edit}</article>"


def _accounts_card(admin: bool) -> str:
    e = html.escape
    rows = []
    for plat, name in PLATFORMS.items():
        t = token(plat)
        if plat == "x" and not configured("x"):
            rows.append("<div class=acct><div><b>X (Twitter)</b><br><span class='muted small'>Switched off. X charges per post "
                        "(about 1.5 cents, or 20 cents with a link), paid from credit you add on X. To switch it on later, "
                        "add <code>X_CLIENT_ID</code> and <code>X_CLIENT_SECRET</code> in Render.</span></div></div>")
            continue
        if not configured(plat):
            status = "<span class=pill>Not set up yet</span>"
            detail = "Add the LinkedIn app keys in Render first (steps below)."
            btn = ""
        elif connected(plat):
            who = t.get("name") or t.get("username") or ""
            if plat == "linkedin":
                days = int((t["expires_at"] - time.time()) // 86400)
                detail = f"Posting as <b>{e(who)}</b>. LinkedIn asks you to sign in again in {days} days; you'll get an email a week before."
            else:
                detail = f"Posting as <b>@{e(t.get('username', ''))}</b>."
            status = "<span class='pill done'>Connected</span>"
            btn = (f"<a class='btn sm ghost' href='/marketing/connect/{plat}'>Reconnect</a>"
                   f"<form method=post action='/marketing/act' style='display:inline'><input type=hidden name=action value=disconnect>"
                   f"<input type=hidden name=platform value={plat}><button class='sm link'>Disconnect</button></form>")
        else:
            expired = bool(t)
            status = f"<span class='pill wait'>{'Sign-in expired' if expired else 'Not connected'}</span>"
            detail = (f"Sign in to {name} once so Modulo may post for you." if not expired else
                      f"{name} needs you to sign in again. Nothing posts there until you do.")
            btn = f"<a class='btn sm' href='/marketing/connect/{plat}'>{'Reconnect' if expired else 'Connect'} {name}</a>"
        rows.append(f"<div class=acct><div><b>{name}</b> {status}<br><span class='muted small'>{detail}</span></div>"
                    f"<div class=row>{btn if admin else ''}</div></div>")
    return "<div class=card><h2>Accounts</h2>" + "".join(rows) + "</div>"


def _setup_help() -> str:
    li, xx = html.escape(redirect_uri("linkedin")), html.escape(redirect_uri("x"))
    open_ = "" if configured("linkedin") else " open"
    return f"""<details class=card{open_}><summary>How to connect LinkedIn <span class=muted>(about 10 minutes, free)</span></summary>
<ol class=steps-help>
<li>Go to <a href="https://www.linkedin.com/developers/apps/new" target=_blank rel=noopener>linkedin.com/developers</a> and press <b>Create app</b>.
LinkedIn asks for a Company Page to link the app to: pick your Modulo page, or create a simple one first. Add a logo and accept the terms.</li>
<li>In the app, open <b>Products</b> and press <b>Request access</b> on <b>Share on LinkedIn</b> and on
<b>Sign In with LinkedIn using OpenID Connect</b>. Both are approved straight away.</li>
<li>Open <b>Auth</b>. Under <b>Authorized redirect URLs</b> add exactly: <code class=copy>{li}</code></li>
<li>On the same page, copy the <b>Client ID</b> and <b>Primary Client Secret</b>.</li>
<li>In Render, open your Modulo service, then <b>Environment</b>. Add <code>LINKEDIN_CLIENT_ID</code> and
<code>LINKEDIN_CLIENT_SECRET</code> with those two values, and save. Render restarts the service (a minute).</li>
<li>Come back here and press <b>Connect LinkedIn</b>. Sign in and allow access.</li>
</ol>
<p class="muted small">Posts appear on your personal LinkedIn profile. Posting as a Company Page needs LinkedIn's separate
"Community Management" review, which can be added later.</p>
<h3 style="margin-top:18px">X (optional, paid)</h3>
<p class="muted small">X has no free plan for posting. In the <a href="https://console.x.com" target=_blank rel=noopener>X developer console</a>
create an app, set up <b>User authentication</b> with OAuth 2.0, type <b>Web App</b>, permissions <b>Read and write</b>, callback
<code class=copy>{xx}</code>. Add some credit and a spending limit, then put the OAuth 2.0 Client ID and Secret in Render as
<code>X_CLIENT_ID</code> and <code>X_CLIENT_SECRET</code>.</p></details>"""


def _settings_card(s: dict, admin: bool) -> str:
    e = html.escape
    if not admin:
        return ""
    chk = lambda k: " checked" if s[k] else ""  # noqa: E731
    per = "".join(f"<option value={i}{' selected' if s['per_week'] == i else ''}>{i}</option>" for i in range(1, 8))
    hours = "".join(f"<option value={h}{' selected' if s['post_hour'] == h else ''}>{h:02d}:00</option>" for h in range(24))
    x_links = (f"<label class=switch><input type=checkbox name=links_x{chk('links_x')}> Cleo may add the website link to X posts "
               "(costs about 20 cents a post instead of 1.5 cents)</label>") if configured("x") else ""
    auto_state = ("<span class='pill done'>On</span>" if s["auto_post"] else "<span class='pill plain'>Off</span>")
    return f"""<details class=card id=settings><summary>Settings <span class=muted>Auto-post {'on' if s['auto_post'] else 'off'}</span></summary>
<form method=post action='/marketing/act'><input type=hidden name=action value=settings>
<label class=switch><input type=checkbox name=weekly{chk('weekly')}> Every Monday morning, Cleo writes the week's posts
for each connected account and emails you</label>
<div class=grid2><div><label for=per>Posts per week, per account</label><select id=per name=per_week>{per}</select></div>
<div><label for=hr>Scheduled posts go out at</label><select id=hr name=post_hour>{hours}</select></div></div>
<label for=tz>Your time zone</label><input id=tz name=tz value="{e(s['tz'])}" placeholder="Europe/London" autocomplete=off>
<p class="muted small">Like Europe/London, America/New_York, Asia/Kolkata, Australia/Sydney. Times on this page use it.</p>
<label class=switch><input type=checkbox name=links_linkedin{chk('links_linkedin')}> Cleo may add the website link to LinkedIn posts</label>
{x_links}
<label for=notes>Standing notes for Cleo</label>
<textarea id=notes name=notes placeholder="For example: we have a launch offer this month; talk to compliance training teams; never mention competitors.">{e(s['notes'])}</textarea>
<h3 style="margin-top:18px">Auto-post {auto_state}</h3>
<p class="muted small">When on, Cleo's Monday posts are scheduled by themselves at the times above, without you approving each one.
You still get an email, and you can edit or cancel any post until it goes out. Posts appear publicly under your name.</p>
<label class=switch><input type=checkbox name=auto_post{chk('auto_post')}> Auto-post Cleo's weekly posts</label>
<label class=switch><input type=checkbox name=confirm_auto> I understand these posts go out under my name without my review</label>
<div class=row style="margin-top:12px"><button>Save settings</button></div></form></details>"""


async def page(request: Request):
    A = _A()
    if blocked := A.gate(request, "designer"):
        return blocked
    user = auth.current_user(request)
    admin = auth.can(user, "admin")
    s = await asyncio.to_thread(settings)
    tz = zone(s)
    drafts = await asyncio.to_thread(posts, ("draft", "failed", "posting"), "created DESC")
    sched = await asyncio.to_thread(posts, ("scheduled",), "scheduled_at ASC")
    done = await asyncio.to_thread(posts, ("posted",), "posted_at DESC", 20)
    err = await asyncio.to_thread(db.get_meta, "marketing_write_error")
    msg = ""
    if m := MESSAGES.get(request.query_params.get("m", "")):
        msg = A.note(m[0], m[1])
    if err and not writing():
        msg += A.note("bad", f"Cleo couldn't write the last drafts: {err}")
    if writing():
        msg += A.note("", "Cleo is writing drafts now. This page refreshes by itself.")
    plats = available()
    plat_boxes = "".join(f"<label class=switch><input type=checkbox name=platform value={p} checked> {PLATFORMS[p]}</label>"
                         for p in plats)
    ask = f"""<div class=card><h2>Ask Cleo for posts</h2>
<form method=post action='/marketing/act'><input type=hidden name=action value=write>
<label for=note>What should these posts be about? <span class=muted>(optional)</span></label>
<textarea id=note name=note style="min-height:80px" placeholder="For example: how we check courses for accessibility, or a tip about writing quiz questions."></textarea>
<div class=row style="margin-top:8px">{plat_boxes}<label for=n style="margin:0 0 0 8px;font-weight:500">How many</label>
<select id=n name=count>{''.join(f'<option{" selected" if i == 3 else ""}>{i}</option>' for i in range(1, 6))}</select>
<button{' disabled' if writing() else ''}>Write drafts</button></div>
<p class="muted small">Takes about a minute and costs a few cents of Claude usage. Nothing is posted until {'you approve it' if admin else 'an Admin approves it'}.</p>
</form></div>"""
    draft_html = "".join(_post_card(p, admin, tz, s) for p in drafts) or "<p class=muted>No drafts waiting.</p>"
    sched_html = "".join(_post_card(p, admin, tz, s) for p in sched) or "<p class=muted>Nothing scheduled.</p>"
    done_html = "".join(_post_card(p, admin, tz, s) for p in done) or "<p class=muted>Nothing posted yet.</p>"
    who_note = "" if admin else A.note("", "You can ask Cleo for drafts and edit them. Only an Admin can post, schedule or connect accounts.")
    body = f"""<style>{PAGE_CSS}</style>
<div class=head><div class=cleo><div class=face aria-hidden=true>C</div><div><h1>Marketing</h1>
<p class=lede>Cleo, our marketing agent, writes posts about Modulo for {' and '.join(PLATFORMS[p] for p in plats)}.
{'You' if admin else 'An Admin'} approve{'' if admin else 's'} each one before it goes out{', unless auto-post is on' if s['auto_post'] else ''}.</p></div></div></div>
{msg}{who_note}
{_accounts_card(admin)}
{ask}
<div class=group><h2>Drafts to check ({len(drafts)})</h2>{draft_html}</div>
<div class=group><h2>Scheduled ({len(sched)})</h2>{sched_html}</div>
<div class=group><h2>Posted</h2>{done_html}</div>
{_settings_card(s, admin)}
{_setup_help() if admin else ''}
{COUNTER_JS}"""
    return A.page("Marketing", body, refresh=writing(), request=request)


async def act(request: Request):
    A = _A()
    if blocked := A.gate(request, "designer"):
        return blocked
    user = auth.current_user(request)
    admin = auth.can(user, "admin")
    form = await request.form()
    action = form.get("action", "")
    pid = form.get("id", "")
    go = lambda m, anchor="": RedirectResponse(f"/marketing?m={m}{anchor}", 303)  # noqa: E731
    if action in ("post", "schedule", "unschedule", "settings", "disconnect") and not admin:
        return A.forbidden(request, "Only an Admin can post, schedule, connect accounts or change marketing settings.")
    s = await asyncio.to_thread(settings)

    if action == "write":
        plats = [p for p in form.getlist("platform") if p in available()] or available()
        try:
            count = max(1, min(5, int(form.get("count", "3"))))
        except ValueError:
            count = 3
        ok = start_writing(plats, count, str(form.get("note", ""))[:1000], user["name"])
        return go("writing" if ok else "busy")
    if action == "settings":
        new = dict(s)
        new["weekly"] = "weekly" in form
        new["links_linkedin"] = "links_linkedin" in form
        new["links_x"] = "links_x" in form
        new["notes"] = str(form.get("notes", ""))[:2000]
        try:
            new["per_week"] = max(1, min(7, int(form.get("per_week", 3))))
            new["post_hour"] = max(0, min(23, int(form.get("post_hour", 9))))
        except ValueError:
            pass
        tz = str(form.get("tz", "UTC")).strip() or "UTC"
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            return go("notz", "#settings")
        new["tz"] = tz
        want_auto = "auto_post" in form
        if want_auto and not s["auto_post"] and "confirm_auto" not in form:
            return go("confirm", "#settings")
        new["auto_post"] = want_auto
        await asyncio.to_thread(save_settings, new)
        if want_auto != s["auto_post"]:
            log.info("Auto-post switched %s by %s", "on" if want_auto else "off", user["email"])
        return go("settings")
    if action == "disconnect":
        plat = form.get("platform", "")
        if plat in PLATFORMS:
            await asyncio.to_thread(_save_token, plat, None)
        return go("disconnected")

    p = await asyncio.to_thread(get_post, pid) if pid else None
    if not p:
        return go("nopost")
    if action == "edit":
        if p["status"] not in ("draft", "failed", "scheduled"):
            return go("nopost")
        text = str(form.get("text", "")).replace("\r\n", "\n").strip()
        if length(p["platform"], text) > LIMITS[p["platform"]] or not text:
            return go("toolong", f"#p{pid}")
        await asyncio.to_thread(update_post, pid, text=text)
        return go("saved", f"#p{pid}")
    if action == "discard":
        if p["status"] in ("draft", "failed"):
            await asyncio.to_thread(update_post, pid, status="discarded")
        return go("discarded")
    if action == "unschedule":
        await asyncio.to_thread(db.execute, "UPDATE posts SET status = 'draft', scheduled_at = NULL, auto = 0, updated = ? "
                                "WHERE id = ? AND status = 'scheduled'", (time.time(), pid))
        return go("unscheduled", f"#p{pid}")
    if action == "schedule":
        try:
            when = dt.datetime.fromisoformat(str(form.get("when", ""))).replace(tzinfo=zone(s)).timestamp()
        except ValueError:
            return go("past", f"#p{pid}")
        if when < time.time() + 60:
            return go("past", f"#p{pid}")
        n = await asyncio.to_thread(db.execute, "UPDATE posts SET status = 'scheduled', scheduled_at = ?, approved_by = ?, "
                                    "error = NULL, updated = ? WHERE id = ? AND status IN ('draft', 'failed', 'scheduled')",
                                    (when, user["name"], time.time(), pid))
        return go("scheduled" if n else "nopost", f"#p{pid}")
    if action == "post":
        ok, _msg = await publish(pid, who=user["name"])
        return go("posted" if ok else "postfail", f"#p{pid}")
    return go("")


ROUTES = [
    Route("/marketing", page),
    Route("/marketing/act", act, methods=["POST"]),
    Route("/marketing/connect/{platform}", connect),
    Route("/marketing/callback/{platform}", callback),
]
