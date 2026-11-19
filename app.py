"""Modulo on Render: one service for the website, the chat and course production.

  /                 the website (public/index.html)
  /api/chat         the agent team chat on the website
  /api/brief        the website's "Send brief" button; starts production automatically
  /review/<id>      a course's page: read the design, approve or request changes, download results
  /files/<id>/...   downloads for a course
  /dashboard        every course and its status (sign-in; see auth.py and accounts.py)
  /builder/<id>     the course builder: edit the course, preview it, download SCORM (see authoring/routes.py)
  /play/<id>/<t>/   the built course, playable in a browser
  /track/<code>     the client's private tracking page (see tracking.py)
  /health           for Render's health check
"""

from __future__ import annotations

import asyncio
import hmac
import json
import html
import logging
import os
import re
from urllib.parse import quote
import time
from contextlib import asynccontextmanager
from pathlib import Path

import markdown
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import accounts
import agent_chat
import auth
import backup
import db
import chat
import emailer
import jobs
import tracking
from authoring import routes as builder_routes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("modulo")
HERE = Path(__file__).resolve().parent
MAX_BRIEFS_PER_DAY = int(os.environ.get("MAX_BRIEFS_PER_DAY", "10"))
DOWNLOADS = {"storyboard.docx", "qa_report.md", "storyboard.md", "1_design.md", "run_log.json", "production_pack.docx"}
VERSION = (os.environ.get("RENDER_GIT_COMMIT") or "local")[:7]
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def dashboard_key() -> str:
    return os.environ.get("DASHBOARD_KEY", "")


def qkey() -> str:
    """Kept for older code: links no longer carry the dashboard key (team members sign in instead)."""
    return ""


def dashboard_url() -> str:
    return f"{jobs.public_url()}/dashboard"


def key_ok(request: Request) -> bool:
    key = dashboard_key()
    given = request.query_params.get("key", "")
    # A "+" in an old, unencoded link arrives as a space; accept both.
    return bool(key and given) and (hmac.compare_digest(given, key) or hmac.compare_digest(given.replace(" ", "+"), key))


def gate(request: Request, need: str = "reviewer", api: bool = False):
    """None when the signed-in member may do this; otherwise the response to send instead."""
    user = auth.current_user(request)
    if user and not auth.same_origin(request):
        return JSONResponse({"error": "Not allowed."}, 403) if api else forbidden(request, "This form came from another website.")
    if auth.can(user, need):
        return None
    if user:
        if api:
            return JSONResponse({"error": f"Your role ({auth.ROLE_NAMES[user['role']]}) can't do this. Ask an Admin."}, 403)
        return forbidden(request)
    if api:
        return JSONResponse({"error": "Please sign in again (reload the page)."}, 401)
    if not auth.has_users():
        # Before the first account exists, the old dashboard link leads to the owner setup.
        return RedirectResponse("/setup" + (f"?key={quote(request.query_params.get('key', ''), safe='')}" if key_ok(request) else ""), 303)
    return RedirectResponse(auth.login_url(request), 303)


def dashboard_ok(request: Request) -> bool:
    """True for a signed-in member who may work on courses (Designer or Admin)."""
    return auth.can(auth.current_user(request), "designer")


def forbidden(request: Request, why: str = "") -> HTMLResponse:
    u = auth.current_user(request)
    role = auth.ROLE_NAMES.get(u["role"], "") if u else ""
    r = page("Not allowed", f"<h1>Not allowed</h1><p>{html.escape(why) or f'Your role ({role}) can’t do this.'} "
             "If you need it, ask an Admin to change your role on the Team members page.</p>"
             "<p><a class='btn ghost' href='/dashboard'>Back to all courses</a></p>", request=request)
    r.status_code = 403
    return r


# ---------------------------------------------------------------- API

async def api_chat(request: Request):
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "The request body must be JSON."}, 400)
    data, status = await chat.reply(body.get("history") if isinstance(body, dict) else None)
    return JSONResponse(data, status, headers={"cache-control": "no-store"})


async def api_brief(request: Request):
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "The request body must be JSON."}, 400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "The request body must be a JSON object."}, 400)
    if body.get("company_site"):  # hidden honeypot field: only bots fill it in
        return JSONResponse({"ok": True})
    name = str(body.get("name") or "").strip()[:120]
    email = str(body.get("email") or "").strip()[:200]
    transcript = str(body.get("transcript") or "").strip()[:60000]
    if not name or not EMAIL_RE.match(email) or len(transcript) < 40:
        return JSONResponse({"error": "Please give your name, a valid email and a few details about the course."}, 400)
    if jobs.briefs_today() >= MAX_BRIEFS_PER_DAY:
        log.warning("Daily brief limit reached; brief from %s not started", email)
        return JSONResponse({"error": "We're receiving a lot of briefs today. Please email us instead."}, 429)
    job = jobs.create(name, email, transcript)
    log.info("New brief %s from %s", job["id"], email)
    emailed = emailer.can_email_clients()
    if emailed:  # in the background, so the website answers straight away
        asyncio.get_running_loop().run_in_executor(None, tracking.email_link, job)
    return JSONResponse({"ok": True, "track_url": tracking.url(job), "emailed": emailed})


async def health(_: Request):
    if not await asyncio.to_thread(db.ping):
        return PlainTextResponse("database not reachable", 503)
    return PlainTextResponse("ok")


# ---------------------------------------------------------------- pages

CSS = """
:root{--bg:#F3F6F4;--fg:#13201B;--muted:#56655E;--line:#D3DDD7;--card:#fff;--surface-2:#E8EEEA;--accent:#0E7A6C;--accent-fg:#fff;
--soft:#E3F1EE;--mark:#F4C53D;--ok:#15803D;--ok-soft:#E3F4E8;--bad:#B3261E;--bad-soft:#FBE9E7;--warn:#9A5B00;--warn-soft:#FDF1D8;
--display:"Bricolage Grotesque","Avenir Next","Segoe UI",system-ui,sans-serif;--body:"Inter","Segoe UI",system-ui,-apple-system,sans-serif;
--shadow:0 1px 2px rgba(19,32,27,.05),0 8px 24px -16px rgba(19,32,27,.18)}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0D1412;--fg:#E4EDE8;--muted:#97A8A0;--line:#2A3A33;--card:#15201C;--surface-2:#1C2924;
--accent:#3CC2AE;--accent-fg:#0D1412;--soft:#16332D;--ok:#5FD38B;--ok-soft:#173022;--bad:#FF8A80;--bad-soft:#3A1D1B;--warn:#F2B55A;--warn-soft:#3A2C12;--shadow:none;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#0D1412;--fg:#E4EDE8;--muted:#97A8A0;--line:#2A3A33;--card:#15201C;--surface-2:#1C2924;
--accent:#3CC2AE;--accent-fg:#0D1412;--soft:#16332D;--ok:#5FD38B;--ok-soft:#173022;--bad:#FF8A80;--bad-soft:#3A1D1B;--warn:#F2B55A;--warn-soft:#3A2C12;--shadow:none;color-scheme:dark}
*{box-sizing:border-box}[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 var(--body);-webkit-font-smoothing:antialiased}
a{color:var(--accent)}
main{max-width:1040px;margin:0 auto;padding:28px max(16px,env(safe-area-inset-left,0px)) 60px}
h1,h2,h3{font-family:var(--display);letter-spacing:-.02em;line-height:1.15;text-wrap:balance}
h1{font-size:clamp(26px,3.4vw,34px);font-weight:800;margin:0 0 6px}h2{font-size:20px;font-weight:700;margin:28px 0 10px}h3{font-size:17px;margin:0 0 6px}
.muted{color:var(--muted)}.small{font-size:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px 22px;margin:16px 0;box-shadow:var(--shadow)}
.card>h2:first-child,.card>h3:first-child{margin-top:0}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:13px;font-weight:600;padding:4px 11px;border-radius:999px;background:var(--soft);color:var(--accent);white-space:nowrap;line-height:1.3}
.pill::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}
.pill.done{background:var(--ok-soft);color:var(--ok)}.pill.failed{background:var(--bad-soft);color:var(--bad)}.pill.wait{background:var(--warn-soft);color:var(--warn)}
.pill.plain::before{display:none}
button,.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;font:600 15px/1.2 var(--body);padding:11px 18px;border-radius:999px;border:1px solid var(--accent);background:var(--accent);color:var(--accent-fg);cursor:pointer;text-decoration:none;white-space:nowrap;transition:transform .12s,background .12s}
button:hover,.btn:hover{transform:translateY(-1px)}
.btn.ghost,button.ghost{background:var(--card);color:var(--fg);border-color:var(--line)}
.btn.ghost:hover,button.ghost:hover{border-color:var(--fg)}
.btn.sm,button.sm{padding:8px 14px;font-size:14px}
a:focus-visible,button:focus-visible,input:focus-visible,textarea:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid var(--mark);outline-offset:2px}
textarea,input{width:100%;font:15px/1.45 var(--body);padding:11px 13px;border:1px solid var(--line);border-radius:12px;background:var(--bg);color:var(--fg)}
textarea:focus,input:focus,select:focus{border-color:var(--accent);outline:none}
textarea{min-height:120px}.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:600}tr:last-child td{border-bottom:0}
.design h1{font-size:22px}.design h2{font-size:18px}.design h3{font-size:16px}.design{overflow-wrap:anywhere}
:target{outline:3px solid var(--accent);outline-offset:4px;border-radius:16px}
.err{color:var(--bad)}.ok{color:var(--ok)}label{font-weight:600;display:block;margin:10px 0 4px}
select{font:inherit;padding:10px;border-radius:12px;border:1px solid var(--line);background:var(--card);color:var(--fg);max-width:100%}
button.link{background:none;border:0;padding:0;color:var(--muted);font:inherit;font-weight:500;cursor:pointer}button.link:hover{color:var(--fg);transform:none}
/* top bar */
.topbar{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--bg) 90%,transparent);backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}
.topbar .in{max-width:1040px;margin:0 auto;padding:0 max(16px,env(safe-area-inset-left,0px));display:flex;align-items:center;gap:18px;min-height:62px;flex-wrap:wrap}
.logo{display:flex;align-items:center;gap:10px;font:800 19px/1 var(--display);text-decoration:none;color:var(--fg);letter-spacing:-.02em}
.logo-mark{width:26px;height:26px;display:grid;grid-template-columns:1fr 1fr;gap:3px}.logo-mark i{border-radius:3px;background:var(--fg)}
.logo-mark i:nth-child(2){background:var(--mark)}.logo-mark i:nth-child(3){background:var(--accent)}
.tabsnav{display:flex;gap:4px;flex-wrap:wrap}.tabsnav a{white-space:nowrap;padding:7px 12px;border-radius:999px;text-decoration:none;color:var(--muted);font-size:14px;font-weight:500}
.tabsnav a:hover{color:var(--fg);background:var(--surface-2)}.tabsnav a.on{color:var(--fg);background:var(--card);box-shadow:inset 0 0 0 1px var(--line)}
.me{margin-left:auto;display:flex;align-items:center;gap:12px;font-size:14px;color:var(--muted)}.me a{color:var(--muted);text-decoration:none;font-weight:500}.me a:hover{color:var(--fg)}
.me a.avatar{color:var(--accent-fg)}.avatar{width:30px;height:30px;border-radius:50%;background:var(--accent);color:var(--accent-fg);display:grid;place-items:center;font-weight:700;font-size:13px}
@media (max-width:760px){.topbar .in{gap:8px;padding-top:10px;padding-bottom:8px}.tabsnav{order:3;width:100%;overflow-x:auto;flex-wrap:nowrap}.me .who{display:none}}
/* page header */
.crumb{font-size:14px;margin:0 0 10px}.crumb a{color:var(--muted);text-decoration:none}.crumb a:hover{color:var(--fg)}
.head{display:flex;gap:16px;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;margin-bottom:8px}.head .row{flex-wrap:wrap}
.lede{color:var(--muted);margin:0}
/* notices */
.note{display:flex;gap:12px;align-items:flex-start;border-radius:14px;padding:14px 16px;margin:12px 0;border:1px solid var(--line);background:var(--card);font-size:15px}
.note .ic{flex:none;width:24px;height:24px;border-radius:50%;display:grid;place-items:center;font-weight:800;font-size:13px;background:var(--soft);color:var(--accent)}
.note.bad{background:var(--bad-soft);border-color:transparent}.note.bad .ic{background:var(--bad);color:#fff}
.note.warn{background:var(--warn-soft);border-color:transparent}.note.warn .ic{background:var(--warn);color:#fff}
.note.good{background:var(--ok-soft);border-color:transparent}.note.good .ic{background:var(--ok);color:#fff}
.note p{margin:0}
/* summary tiles */
.tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:22px 0 8px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px 18px;box-shadow:var(--shadow)}
.tile b{display:block;font:800 30px/1.1 var(--display)}.tile span{color:var(--muted);font-size:14px}
.tile.hot{border-color:var(--warn);background:var(--warn-soft)}.tile.hot b{color:var(--warn)}
@media (max-width:560px){.tiles{gap:8px}.tile{padding:12px}.tile b{font-size:24px}.tile span{font-size:12px}}
/* course list */
.group{margin-top:30px}.group>h2{font-size:15px;font-family:var(--body);letter-spacing:0;text-transform:uppercase;color:var(--muted);margin:0 0 10px;font-weight:600}
.course{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px 20px;margin:0 0 12px;box-shadow:var(--shadow);display:grid;grid-template-columns:1fr auto;gap:10px 20px}
.course.hot{border-left:5px solid var(--warn)}.course.bad{border-left:5px solid var(--bad)}
.course h3{margin:0;font-size:18px}.course h3 a{color:var(--fg);text-decoration:none}.course h3 a:hover{text-decoration:underline}
.course .meta{font-size:14px;color:var(--muted)}
.course .next{grid-column:1/-1;display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap;background:var(--bg);border-radius:12px;padding:10px 12px}
.course .next p{margin:0;font-size:15px}.course .next b{font-weight:600}
.course .more{grid-column:1/-1;display:flex;gap:16px;flex-wrap:wrap;font-size:14px}.course .more a{color:var(--muted);font-weight:500}.course .more a:hover{color:var(--fg)}
@media (max-width:560px){.course{grid-template-columns:1fr;padding:16px}.course .side{order:-1}}
/* progress steps */
.steps{display:flex;list-style:none;padding:0;margin:4px 0 0;gap:4px;grid-column:1/-1}
.steps li{flex:1;min-width:0;font-size:12px;color:var(--muted);padding-top:10px;position:relative;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.steps li::before{content:"";position:absolute;top:0;left:0;right:0;height:5px;border-radius:3px;background:var(--line)}
.steps li.done::before{background:var(--accent)}.steps li.now{color:var(--fg);font-weight:600}
.steps li.now::before{background:linear-gradient(90deg,var(--accent) 50%,var(--line) 50%)}.steps li.you::before{background:var(--warn)}.steps li.you{color:var(--warn)}
.steps li.stop::before{background:var(--bad)}.steps li.stop{color:var(--bad)}
@media (max-width:560px){.steps li{font-size:0}.steps li.now,.steps li.you,.steps li.stop{font-size:12px;overflow:visible}}
/* course page */
.next-card{border:2px solid var(--accent)}.next-card.hot{border-color:var(--warn)}.next-card.bad{border-color:var(--bad)}
.eyebrow+h2{margin-top:0}.eyebrow{font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-weight:600;margin:0 0 4px}
details.card>summary{cursor:pointer;font:700 18px/1.3 var(--display);list-style:none;display:flex;justify-content:space-between;gap:10px;align-items:center}
details.card>summary::-webkit-details-marker{display:none}
details.card>summary::after{content:"Show";font:600 14px var(--body);color:var(--accent)}details.card[open]>summary::after{content:"Hide"}
details.card[open]>summary{margin-bottom:12px}details.card>summary .muted{font:400 14px var(--body)}
.grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 16px}@media (max-width:760px){.grid2{grid-template-columns:1fr}}
.foot{margin-top:40px;font-size:12px;color:var(--muted)}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

FONTS = ("<link rel=preconnect href='https://fonts.googleapis.com'><link rel=stylesheet href='https://fonts.googleapis.com/css2?"
         "family=Bricolage+Grotesque:opsz,wght@12..96,700;12..96,800&family=Inter:wght@400;500;600&display=swap'>")

STATUS_TEXT = {
    "designing": "Iris is writing the design",
    "revising": "Iris is revising the design",
    "awaiting_approval": "Waiting for your approval",
    "storyboarding": "Storyboard in progress",
    "packing": "Production pack in progress",
    "building": "Dev is building the course",
    "done": "Finished",
    "failed": "Stopped with a problem",
}


def nav(request: Request | None) -> str:
    """The bar at the top: the Modulo logo, and for signed-in team members the main sections and their name."""
    u = auth.current_user(request) if request is not None else None
    logo = "<a class=logo href='/dashboard'><span class=logo-mark aria-hidden=true><i></i><i></i><i></i><i></i></span>Modulo</a>"
    if not u:
        return f"<header class=topbar><div class=in>{logo}</div></header>"
    path = request.url.path
    items = [("/dashboard", "Courses", ("/dashboard", "/review", "/course-files", "/builder"))]
    if auth.can(u, "designer"):
        items.append(("/team", "Talk to the team", ("/team",)))
    if auth.can(u, "admin"):
        items += [("/admin/team", "Team members", ("/admin/team",)), ("/admin/system", "System", ("/admin/system",))]
    links = "".join(f"<a href='{href}'{' class=on aria-current=page' if path.startswith(pre) else ''}>{label}</a>"
                    for href, label, pre in items)
    initial = html.escape((u["name"] or "?").strip()[:1].upper())
    return (f"<header class=topbar><div class=in>{logo}<nav class=tabsnav aria-label='Main'>{links}</nav>"
            f"<div class=me><span class=who>{html.escape(u['name'])} · {auth.ROLE_NAMES[u['role']]}</span>"
            f"<a href='/account' title='Your account' class=avatar aria-label='Your account'>{initial}</a>"
            "<form method=post action='/logout' style='display:inline'><button class=link>Sign out</button></form></div></div></header>")


def page(title: str, body: str, refresh: bool = False, request: Request | None = None) -> HTMLResponse:
    meta = '<meta http-equiv="refresh" content="30">' if refresh else ""
    return HTMLResponse(
        f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<meta name=robots content=noindex>{meta}<title>{html.escape(title)} · Modulo</title>{FONTS}<style>{CSS}</style></head>"
        f"<body>{nav(request)}<main>{body}<p class=foot>Modulo · version {VERSION}</p></main></body></html>",
        headers={"cache-control": "no-store", "referrer-policy": "same-origin"},
    )


def pill(status: str) -> str:
    cls = {"done": "done", "failed": "failed", "awaiting_approval": "wait"}.get(status, "")
    return f'<span class="pill {cls}">{html.escape(STATUS_TEXT.get(status, status))}</span>'


def note(kind: str, text: str) -> str:
    """A coloured notice box. kind: bad, warn, good or info."""
    icon = {"bad": "!", "warn": "!", "good": "✓"}.get(kind, "i")
    return f"<div class='note {kind}'><span class=ic aria-hidden=true>{icon}</span><div>{text}</div></div>"


STEP_NAMES = ["Design", "Your OK", "Storyboard", "Course built", "Shared with client"]


def progress(job: dict) -> tuple[int, str]:
    """Where a course is on the five big steps: (index of the current step, its state).
    State is now (the team is on it), you (waiting for a person), stop (stopped with a problem) or all_done."""
    out = jobs.job_dir(job["id"]) / "output"
    st = job["status"]
    sample = job.get("source") == "sample"
    if st in {"designing", "revising"}:
        return 0, "now"
    if st == "awaiting_approval":
        return 1, "you"
    if st == "storyboarding":
        return 2, "now"
    if st == "building":
        return 3, "now"
    if st == "failed":
        if (out / "storyboard.json").exists():
            return 3, "stop"
        if (out / "1_design_APPROVED.txt").exists():
            return 2, "stop"
        return (1 if (out / "1_design.json").exists() else 0), "stop"
    # done or packing: the storyboard is finished
    if not jobs.course_ready(job):
        return 3, "todo" if sample else "you"
    if sample:
        return 4, "all_done"
    if tracking.is_shared(job):
        return 5, "all_done"
    return 4, "you"


def steps_html(job: dict) -> str:
    i, state = progress(job)
    names = STEP_NAMES[:4] if job.get("source") == "sample" else STEP_NAMES
    items = []
    for n, name in enumerate(names):
        cls = "done" if n < i else (state if n == i and state != "all_done" else "")
        items.append(f"<li class='{cls}' title='{name}'>{name}</li>")
    return f"<ol class=steps aria-label='Progress'>{''.join(items)}</ol>"


def next_step(job: dict, work: bool) -> tuple[str, str, str, str]:
    """What should happen next, in plain words: (group, sentence, button label, button link).
    group is you (needs a person), team (the agents are on it) or done."""
    jid = job["id"]
    st = job["status"]
    page_ = jobs.page_url(job)
    if st == "awaiting_approval":
        if work:
            return "you", "<b>Your turn:</b> read Iris's design, then approve it or ask for changes.", "Review the design", f"{page_}#approve"
        return "you", "<b>Waiting for approval.</b> Read the design and send changes if needed.", "Read the design", f"{page_}#changes"
    if st == "failed":
        if work:
            return "you", "<b>Stopped with a problem.</b> Open it to see what happened and try again.", "See the problem", page_
        return "you", "<b>Stopped with a problem.</b> A Designer or Admin needs to fix it and try again.", "Open", page_
    if st in jobs.WORKING:
        what = {"designing": "Iris is writing the course design.", "revising": "Iris is updating the design with your changes.",
                "storyboarding": "Theo is writing the storyboard and Quinn is checking it.",
                "packing": "The team is writing the production pack.", "building": "Dev is building the course."}[st]
        return "team", f"<b>Nothing to do.</b> {what} You'll get an email when it's done.", "", ""
    if job.get("source") == "sample":
        return "done", "<b>Demo course</b> to explore. Try it, chat with the team about it, or open the files.", "Open", page_
    if not jobs.course_ready(job):
        if work:
            return "you", "<b>Storyboard ready.</b> Next: turn it into a real course.", "Build the course", f"{page_}#build"
        return "done", "<b>Storyboard ready</b> to download.", "Open", page_
    if not tracking.is_shared(job):
        if work:
            return "you", "<b>Course built.</b> Check it in the editor, then share it with the client.", "Share with client", f"{page_}#client"
        return "done", "<b>Course built.</b> Try it from the course page.", "Open", page_
    return "done", "<b>All done.</b> The client can see and try the course.", "Open", page_


def get_job(request: Request) -> dict | None:
    """A course opened with its private email link code, or by a signed-in team member."""
    try:
        job = jobs.load(request.path_params["job_id"])
    except KeyError:
        return None
    token = request.query_params.get("t", "")
    if token and hmac.compare_digest(token, job["token"]):
        request.state.via_link = True
        return job
    request.state.via_link = False
    return job if auth.can(auth.current_user(request), "reviewer") else None


def may(request: Request, need: str) -> bool:
    """Email links can do everything on the course page, as before; team members by their role."""
    return getattr(request.state, "via_link", False) or auth.can(auth.current_user(request), need)


def not_found() -> HTMLResponse:
    r = page("Not found", "<h1>Link not found</h1><p class=muted>This link is wrong or has expired. Use the link in your latest email.</p>")
    r.status_code = 404
    return r


async def review(request: Request):
    job = get_job(request)
    if not job:
        if not auth.current_user(request) and not request.query_params.get("t"):
            return gate(request, "reviewer")
        return not_found()
    t = html.escape(job["token"]) if request.state.via_link else ""
    work = may(request, "designer")

    def file_link(name: str) -> str:
        return jobs.file_url(job, name) if request.state.via_link else f"/files/{jid}/{name}"
    jid = job["id"]
    st = job["status"]
    out = jobs.job_dir(jid) / "output"
    signed_in = bool(auth.current_user(request))
    tools = []
    if work and signed_in:
        tools.append(f"<a class='btn ghost sm' href='/team?course={jid}'>Talk to the team</a>")
    if signed_in:
        tools.append(f"<a class='btn ghost sm' href='/course-files/{jid}'>All files</a>")
    parts = [
        f"<p class=crumb><a href='{html.escape(dashboard_url())}'>← All courses</a></p>" if signed_in else "",
        f"<div class=head><div><h1>{html.escape(job.get('title') or 'New course brief')}</h1>"
        f"<p class=lede>For {html.escape(job['client_name'])} ({html.escape(job['client_email'])}) · "
        f"received {time.strftime('%d %b %Y', time.gmtime(job['created']))}</p></div>"
        f"<div class=row>{''.join(tools)}</div></div>",
        f"<div class=card style='padding:16px 20px'><div class=row style='justify-content:space-between'>"
        f"<div class=row>{pill(st)} <span class='muted small'>{html.escape(job.get('step', '')) if job.get('step', '') != STATUS_TEXT.get(st) else ''}</span></div></div>"
        f"{steps_html(job)}</div>",
    ]
    if request.query_params.get("m"):
        parts.append(note("good", f"<p><b>{html.escape(request.query_params['m'][:300])}</b></p>"))
    track_card = tracking.admin_card(job, t) if work and job.get("source") != "sample" else ""
    changes_form = f"""<form method=post action="/review/{jid}/changes?t={t}"><label for=fb>What should Iris change?</label>
<textarea id=fb name=feedback required placeholder="For example: make it 15 minutes and add a module on reporting near misses"></textarea>
<p style="margin:12px 0 0"><button class=ghost>Send changes to Iris</button></p></form>"""
    if st == "awaiting_approval" and not work:
        parts.append(f"""<div class='card next-card hot' id=changes><p class=eyebrow>Next step</p><h2>Request changes</h2>
<p class=muted>Read the design below. If something should change, write it here in your own words. Approving the design is for Designers and Admins.</p>
{changes_form}</div>""")
    elif st == "awaiting_approval":
        parts.append(f"""<div class='card next-card hot'><p class=eyebrow>Your turn</p><h2>Happy with this design?</h2>
<p>Read Iris's design below. Then either approve it, or tell Iris what to change.</p>
<div class=grid2><div id=approve style="padding:4px 0"><h3>Approve it</h3>
<p class=muted>Starts the full storyboard. Takes about an hour and costs roughly $7 in Claude usage. You'll get an email with the Word storyboard when it's done.</p>
<form method=post action="/review/{jid}/approve?t={t}"><button>Approve and start the storyboard</button></form></div>
<div id=changes style="padding:4px 0"><h3>Or ask for changes</h3>
<p class=muted>You'll get a new email with the revised design in a few minutes.</p>{changes_form}</div></div></div>""")
    elif st in jobs.WORKING:
        parts.append(note("info", f"<p><b>The team is working on it.</b> {html.escape(STATUS_TEXT.get(st, ''))}. "
                          "This page refreshes by itself, and you'll get an email when there's something to look at. You can close this page.</p>"))
    elif st == "failed":
        parts.append(f"""<div class='card next-card bad'><p class=eyebrow>Needs attention</p><h2>Something went wrong</h2>
<p>Common causes: the Claude account ran out of credit or hit its spending limit, or the API key is wrong. Fix that, then try again. Finished steps are kept.</p>
{f'<form method=post action="/review/{jid}/retry?t={t}" style="margin-bottom:14px"><button>Try again</button></form>' if work else ''}
<details><summary class='muted small' style='cursor:pointer'>Technical details</summary>
<pre style="white-space:pre-wrap;font-size:13px">{html.escape(job.get('error', ''))}</pre></details></div>""")
    if st == "done":
        s = job.get("summary", {})
        ready = jobs.course_ready(job)
        if not ready and work:
            parts.append(builder_routes.build_card(job, qkey()).replace(
                "<div class=card id=build><h2 style=\"margin-top:0\">",
                "<div class='card next-card hot' id=build><p class=eyebrow>Next step</p><h2>", 1))
        if ready:
            if work and job.get("source") != "sample" and not tracking.is_shared(job):
                parts.append(note("warn", "<p><b>Next step:</b> try the course and fix anything in the editor. "
                                  "When you're happy, press <a href='#client'>Share with client</a> below.</p>"))
            parts.append(f"""<div class=card id=course><h2>The course</h2>
<p>Built and ready to edit. Try it like a learner, change anything without code, and download it for your LMS (SCORM 1.2 or 2004).</p>
<p class=row>{f'<a class=btn href="/builder/{jid}">Open the course editor</a>' if work else ''}
<a class="btn ghost" href="{html.escape(jobs.preview_url(job))}" target=_blank rel=noopener>Try the course</a></p></div>""")
        qa = "passed" if s.get("passed") else "has open points (see the QA report)"
        parts.append(f"""<div class=card><h2>Documents</h2>
<p class=muted>{s.get('screens', '?')} screens · final quality check {qa} · ${job.get('cost_usd', 0):.2f} spent</p>
<p class=row><a class=btn href="{html.escape(file_link('storyboard.docx'))}">Storyboard (Word)</a>
<a class="btn ghost" href="{html.escape(file_link('qa_report.md'))}">QA report</a>
{f'<a class="btn ghost" href="{html.escape(file_link("production_pack.docx"))}">Production pack (Word)</a>' if (out / "production_pack.docx").exists() else ""}</p></div>""")
    parts.append(track_card)
    if st == "done" and not job.get("pack_done") and work:
        team = "".join(f"<li><b>{n}</b>, {r}: {t_.lower()}</li>" for _, n, r, _, t_ in jobs.pipeline.PACK_AGENTS)
        parts.append(f"""<details class=card id=pack><summary><span>Optional: the production pack <span class=muted>· documents from the rest of the team</span></span></summary>
<p>The rest of the team writes their documents from this storyboard:</p><ul>{team}</ul>
<p>It takes about 20 to 30 minutes and costs {html.escape(jobs.PACK_ESTIMATE)} in Claude usage. You'll get an email with one Word file when it's ready. They write documents only: no voice recording, image generation or LMS upload happens.</p>
<form method=post action="/review/{jid}/pack?t={t}"><button>Start the production pack</button></form></details>""")
    if (out / "1_design.md").exists():
        parts.append(f"<details class='card design' id=design{' open' if st == 'awaiting_approval' else ''}>"
                     f"<summary><span>Course design <span class=muted>· by Iris</span></span></summary>{jobs.design_html(job)}</details>")
    parts.append("<details class=card id=history><summary><span>History <span class=muted>· every step, newest first</span></span></summary><table>" + "".join(
        f"<tr><td class=muted style='white-space:nowrap'>{time.strftime('%d %b %H:%M UTC', time.gmtime(h['at']))}</td>"
        f"<td>{html.escape(h['text'])}{' <span class=muted>(' + html.escape(h['by']) + ')</span>' if h.get('by') else ''}</td></tr>"
        for h in reversed(job.get("history", []))) + "</table></details>")
    # Links from emails (like #pack) can point inside a closed section: open it.
    parts.append("<script>function o(){var e=location.hash&&document.getElementById(location.hash.slice(1));"
                 "var d=e&&e.closest('details');if(d){d.open=true;e.scrollIntoView();}}o();addEventListener('hashchange',o);</script>")
    return page(job.get("title") or "Course", "".join(parts), refresh=st in jobs.WORKING, request=request)


async def review_action(request: Request):
    job = get_job(request)
    if not job:
        return not_found()
    action = request.path_params["action"]
    need = "reviewer" if action == "changes" else "designer"
    if not request.state.via_link and (blocked := gate(request, need)):
        return blocked
    if action in tracking.ACTIONS:
        if not request.state.via_link and (blocked := gate(request, "designer")):
            return blocked
    by = auth.who(request) or ("email link" if request.state.via_link else None)
    if action in tracking.ACTIONS:
        msg = await asyncio.to_thread(tracking.act, job, action, await request.form(), by)
        back = f"/review/{job['id']}?" + (f"t={job['token']}&" if request.state.via_link else "")
        return RedirectResponse(back + f"m={quote(msg)}#client", 303)
    if action == "approve":
        jobs.approve(job, by)
    elif action == "changes":
        form = await request.form()
        jobs.request_changes(job, str(form.get("feedback") or ""), by)
    elif action == "retry":
        jobs.retry(job, by)
    elif action == "pack":
        jobs.start_pack(job, by)
    back = f"/review/{job['id']}" + (f"?t={job['token']}" if request.state.via_link else "")
    return RedirectResponse(back, 303)


async def files(request: Request):
    job = get_job(request)
    name = request.path_params["name"]
    if not job or name not in DOWNLOADS:
        return not_found()
    f = jobs.job_dir(job["id"]) / "output" / name
    if not f.exists():
        return not_found()
    slug = re.sub(r"[^A-Za-z0-9]+", "_", job.get("title") or job["id"]).strip("_")[:60]
    return FileResponse(f, filename=f"{slug}_{name}" if name == "storyboard.docx" else name)


_key_check: dict = {}


async def claude_key_status() -> tuple[bool, str]:
    """Checks the Claude key at most every 5 minutes. Free: it only looks up a model."""
    if os.environ.get("MODULO_FAKE"):
        return True, ""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return False, "no key has been added"
    cached = _key_check.get(key)
    if cached and time.time() - cached[0] < 300:
        return cached[1], cached[2]
    import anthropic
    try:
        await anthropic.AsyncAnthropic(max_retries=1, timeout=15).models.retrieve("claude-opus-5-5")
        result = (True, "")
    except anthropic.AuthenticationError:
        result = (False, "the key is invalid (it may have been deleted, mistyped, or belong to another account)")
    except anthropic.PermissionDeniedError as e:
        result = (False, f"the key is not allowed to use this model ({e.message})")
    except anthropic.APIError as e:
        return True, f"could not check right now ({e.__class__.__name__})"
    _key_check[key] = (time.time(), *result)
    return result


def course_row(j: dict, work: bool) -> tuple[str, str]:
    """One course on the dashboard: its group and its card."""
    group, sentence, label, href = next_step(j, work)
    _, state = progress(j)
    cls = "bad" if j["status"] == "failed" else "hot" if group == "you" else ""
    jid = j["id"]
    more = []
    if j.get("course_built") and work:
        more.append(f"<a href='/builder/{jid}'>Course editor</a>")
    if work:
        more.append(f"<a href='/team?course={jid}'>Talk to the team</a>")
    more.append(f"<a href='/course-files/{jid}'>Files</a>")
    if work and j.get("source") != "sample":
        more.append(f"<a href='/review/{jid}#client'>Client tracking page</a>")
    button = (f"<a class='btn sm{'' if group == 'you' else ' ghost'}' href='{html.escape(href)}'>{label}</a>" if label else "")
    cost = j.get("cost_usd", 0)
    card = f"""<article class='course {cls}'>
<div><h3><a href='{html.escape(jobs.page_url(j))}'>{html.escape(j.get('title') or 'New brief')}</a></h3>
<div class=meta>{html.escape(j['client_name'])} · received {time.strftime('%d %b %Y', time.gmtime(j['created']))}{f' · ${cost:.2f} spent' if cost else ''}</div></div>
<div class=side>{pill(j['status'])}</div>
{steps_html(j)}
<div class=next><p>{sentence}</p>{button}</div>
<nav class=more aria-label='More for this course'>{''.join(more)}</nav></article>"""
    return group, card


async def dashboard(request: Request):
    if blocked := gate(request, "reviewer"):
        return blocked
    k = qkey()
    work = auth.can(auth.current_user(request), "designer")
    all_jobs = jobs.all_jobs()
    groups: dict[str, list[str]] = {"you": [], "team": [], "done": []}
    for j in all_jobs:
        g, card = course_row(j, work)
        groups[g].append(card)

    notes = []
    if not (os.environ.get("RESEND_API_KEY") and os.environ.get("NOTIFY_EMAIL")):
        notes.append(note("bad", "<p><b>Emails are switched off.</b> You won't get approval emails until you add "
                          "RESEND_API_KEY and NOTIFY_EMAIL in Render (your service, then Environment).</p>"))
    ok, why = await claude_key_status()
    if not ok:
        notes.append(note("bad", f"<p><b>The Claude key is not working</b>, so the team can't do any work. Claude said: {html.escape(why)}. "
                          "Put a new key in Render (your service, Environment, ANTHROPIC_API_KEY), save, wait until it says Live, "
                          "then reload this page.</p>"))
    if os.environ.get("MODULO_FAKE"):
        notes.append(note("info", "<p><b>Test mode is on.</b> Courses use the sample forklift answers and cost nothing.</p>"))
    q = jobs.queue_state()
    if q["waiting"]:
        notes.append(note("info", f"<p><b>{len(q['waiting'])} course{'s' if len(q['waiting']) != 1 else ''} waiting in line.</b> "
                          f"The team works on up to {q['parallel']} at a time; the next one starts by itself.</p>"))

    tiles = (f"<div class=tiles><div class='tile{' hot' if groups['you'] else ''}'><b>{len(groups['you'])}</b><span>Need you</span></div>"
             f"<div class=tile><b>{len(groups['team'])}</b><span>Team working on it</span></div>"
             f"<div class=tile><b>{len(groups['done'])}</b><span>Done</span></div></div>")

    def section(key: str, title: str, empty: str = "") -> str:
        if not groups[key]:
            return f"<section class=group><h2>{title}</h2><p class=muted>{empty}</p></section>" if empty else ""
        return f"<section class=group><h2>{title} · {len(groups[key])}</h2>{''.join(groups[key])}</section>"

    if all_jobs:
        lists = (section("you", "Needs you", "Nothing needs you right now.")
                 + section("team", "The team is working on it") + section("done", "Done"))
    else:
        lists = ("<div class=card style='text-align:center;padding:40px 22px'><h2 style='margin-top:0'>No courses yet</h2>"
                 "<p class=muted>Briefs that clients send from your website appear here by themselves.</p>"
                 + (f"<p class=row style='justify-content:center;margin-top:16px'>{sample_button(k)}"
                    "<a class=btn href='#new'>Paste a brief</a></p>" if work else "") + "</div>")

    actions = ""
    new_form = ""
    if work:
        actions = (f"<div class=row><a class='btn' href='#new'>+ New course</a>"
                   f"{sample_button(k, 'Add the free sample course') if all_jobs else ''}</div>")
        new_form = f"""<div class=card id=new><h2>Start a course from a brief</h2>
<p class=muted>Got a brief by email or in a document? Paste it here. It goes through the same steps as a brief sent from your website.</p>
<form method=post action="/dashboard/new"><div class=grid2><div><label for=nn>Client name</label><input id=nn name=name required></div>
<div><label for=ne>Client email</label><input id=ne name=email type=email required></div></div>
<label for=nt>The brief</label><textarea id=nt name=transcript required placeholder="Paste the brief here: who the course is for, what they should learn, how long it should be..."></textarea>
<p style="margin:12px 0 0"><button>Start the course</button></p></form></div>"""
    intro = ("Every course and what happens next. Courses that need you are at the top." if work else
             "Every course and where it is. Open a course to read it, download files or request changes.")
    body = f"""<div class=head><div><h1>Courses</h1><p class=lede>{intro}</p></div>{actions}</div>
{''.join(notes)}{tiles if all_jobs else ''}{lists}{new_form}"""
    return page("Courses", body, refresh=any(j["status"] in jobs.WORKING for j in all_jobs) or bool(q["waiting"]), request=request)


async def dashboard_new(request: Request):
    if blocked := gate(request, "designer"):
        return blocked
    form = await request.form()
    name, email, transcript = (str(form.get(k) or "").strip() for k in ("name", "email", "transcript"))
    if name and email and transcript:
        job = jobs.create(name[:120], email[:200], transcript[:60000], source="pasted on the dashboard", who=auth.who(request))
        return RedirectResponse(jobs.page_url(job), 303)
    return RedirectResponse("/dashboard", 303)


# ---------------------------------------------------------------- talk to one agent

TEAM_JS = r"""
const $=s=>document.querySelector(s), KEY=new URLSearchParams(location.search).get('key');
const AG={iris:['Iris','Instructional Designer','Ask about the design, or ask for changes to it.','For example: Iris, why did you choose a 24-minute course?'],
          theo:['Theo','Storyboard Writer','Ask about any screen, or ask Theo to rewrite, add or remove screens.','For example: Theo, make screen M1-S03 shorter and add a quiz question after it'],
          quinn:['Quinn','QA Reviewer','Ask why something was flagged, how serious it is, or to check a screen.','For example: Quinn, which of your findings matter most and why?'],
          sam:['Sam','SME Liaison','Ask about what the SME must check, open facts, or the interview guide.','For example: Sam, what are the five things the SME must confirm first?'],
          vera:['Vera','Visual Designer','Ask about the look, colors, characters, or image prompts.','For example: Vera, make the palette warmer and give me prompts for module 1'],
          mila:['Mila','Voiceover and Media Producer','Ask about the voice, recording specs, pronunciation or captions.','For example: Mila, what voice would you cast and why?'],
          dev:['Dev','eLearning Developer','Ask about the build in Storyline or Rise, variables, or build risks.','For example: Dev, which screens are hardest to build?'],
          lars:['Lars','LMS and SCORM Administrator','Ask about SCORM or xAPI settings, tracking and LMS testing.','For example: Lars, should we use SCORM 1.2 or 2004 here?'],
          nia:['Nia','Learning Analyst','Ask how to measure whether the course worked.','For example: Nia, what should we report to the client after 90 days?'],
          paige:['Paige','Project Manager','Ask about the plan, schedule, open items and risks.','For example: Paige, what are the next three steps with the client?']};
let agent=new URLSearchParams(location.search).get('agent')||'iris', busy=false;
const course=()=>$('#course').value, store=()=>`modulo-chat:${course()}:${agent}`;
function load(){try{return JSON.parse(localStorage.getItem(store())||'[]')}catch{return[]}}
function keep(h){try{localStorage.setItem(store(),JSON.stringify(h))}catch{}}
function bubble(m){const d=document.createElement('div');d.className='msg '+(m.from==='you'?'you':'them');
  const who=document.createElement('b');who.textContent=m.from==='you'?'You':AG[agent][0];d.append(who);
  const t=document.createElement('div');t.textContent=m.text;d.append(t);
  if(m.proposal){const c=document.createElement('div');c.className='prop';
    const s=document.createElement('div');s.textContent='Proposed change: '+m.proposal.summary;c.append(s);
    if(m.saved){const ok=document.createElement('div');ok.className='muted';ok.textContent=m.saved;c.append(ok);}
    else{const b=document.createElement('button');b.textContent='Save to course';b.onclick=()=>save(m,b);c.append(b);}
    d.append(c);}
  $('#log').append(d);}
function draw(){$('#log').innerHTML='';const h=load();
  if(!h.length){const p=document.createElement('p');p.className='muted';p.textContent=AG[agent][2];$('#log').append(p);}
  h.forEach(bubble);$('#log').scrollTop=1e9;
  document.querySelectorAll('.tabs button').forEach(b=>b.classList.toggle('on',b.dataset.a===agent));
  $('#who').textContent=AG[agent][0]+', '+AG[agent][1];
  const o=$('#course').selectedOptions[0], why=blocked(o);
  const note=stageNote(o);
  $('#msg').disabled=!!why; $('#send').disabled=!!why; $('#why').hidden=!note; $('#why').textContent=note;
  $('#msg').placeholder=why?'':AG[agent][3];}
function blocked(o){return '';}
function stageNote(o){
  if(['designing','revising','storyboarding'].includes(o.dataset.status)) return 'This course is in production right now. You can chat about it; any changes can be saved once the current step has finished.';
  if(agent==='theo' && o.dataset.sb!=='1') return 'The storyboard is not finished yet, so Theo can answer questions and share ideas, but not change screens yet.';
  if(!['iris','theo','quinn'].includes(agent) && !(o.dataset.pack||'').split(',').includes(agent)) return AG[agent][0]+"'s document for this course hasn't been written yet. You can still ask questions; it's written when you start the production pack on the course page.";
  return '';}
async function post(url,body){const r=await fetch(url+'?key='+encodeURIComponent(KEY),{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
  let j={};try{j=await r.json()}catch{} if(!r.ok) throw new Error(j.error||'Something went wrong.'); return j;}
async function send(e){e.preventDefault();const text=$('#msg').value.trim();if(!text||busy)return;
  const h=load();h.push({from:'you',text});keep(h);$('#msg').value='';draw();busy=true;$('#err').textContent='';
  $('#wait').hidden=false;$('#wait').textContent=AG[agent][0]+' is reading the course and thinking. Bigger changes can take a minute or two.';
  try{const j=await post('/api/team/chat',{job_id:course(),agent,history:h});h.push({from:agent,text:j.reply,proposal:j.proposal});keep(h);}
  catch(x){$('#err').textContent=x.message;}
  busy=false;$('#wait').hidden=true;draw();}
async function save(m,b){b.disabled=true;b.textContent='Saving...';
  try{const j=await post('/api/team/apply',{job_id:course(),proposal_id:m.proposal.id});
    const h=load();const t=h.find(x=>x.proposal&&x.proposal.id===m.proposal.id);if(t)t.saved=j.message;keep(h);draw();}
  catch(x){b.disabled=false;b.textContent='Save to course';$('#err').textContent=x.message;}}
$('#form').onsubmit=send;
$('#msg').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){send(e)}});
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>{agent=b.dataset.a;draw();});
$('#course').onchange=()=>{draw();$('#open').href=$('#course').selectedOptions[0].dataset.review;};
$('#clear').onclick=()=>{try{localStorage.removeItem(store())}catch{} draw();};
draw();
try{$('#msg').focus({preventScroll:false});$('#form').scrollIntoView({block:'end'});}catch{}
"""

TEAM_CSS = """
.tabs{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}.tabs button{background:transparent;color:var(--accent)}.tabs button.on{background:var(--accent);color:#fff}
#log{background:var(--card);border:1px solid var(--line);border-radius:14px 14px 0 0;padding:16px;height:min(38vh,380px);overflow:auto}
#form{background:var(--card);border:1px solid var(--line);border-top:0;border-radius:0 0 14px 14px;padding:12px 16px}
#form textarea{min-height:70px}#form p{margin:8px 0 0}
.msg{margin:0 0 14px;max-width:92%}.msg b{display:block;font-size:13px;color:var(--muted)}.msg div{white-space:pre-wrap}
.msg.you{margin-left:auto;text-align:right}.msg.you>div{background:var(--soft);display:inline-block;text-align:left;padding:8px 12px;border-radius:12px}
.prop{margin-top:8px;padding:12px;border:1px dashed var(--accent);border-radius:12px;display:grid;gap:8px;white-space:normal}
.prop button{justify-self:start}select{font:inherit;padding:9px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--fg);max-width:100%}
#err{color:var(--bad);min-height:1.4em}textarea:disabled{opacity:.5}button:disabled{opacity:.5;cursor:default}
"""


def _admin_job(job_id: str) -> dict | None:
    try:
        return jobs.load(str(job_id))
    except KeyError:
        return None


async def dashboard_sample(request: Request):
    if blocked := gate(request, "designer"):
        return blocked
    job = await asyncio.to_thread(jobs.create_sample)
    return RedirectResponse(f"/team?course={job['id']}&agent=theo", 303)


def sample_button(k: str, label: str = "Add the sample forklift course") -> str:
    return (f"<form method=post action='/dashboard/sample' style='display:inline'><button class=ghost>{label}</button></form>")


async def team_page(request: Request):
    if blocked := gate(request, "designer"):
        return blocked
    all_jobs = jobs.all_jobs()
    k = qkey()
    if not all_jobs:
        return page("Talk to the team", f"<p class=crumb><a href='/dashboard'>← All courses</a></p><h1>Talk to the team</h1>"
                    "<div class=card><p style='margin-top:0'><b>There's nothing to talk about yet.</b> The agents chat about a specific course, "
                    "and you don't have any courses yet.</p>"
                    "<p>To try it now, add the finished forklift sample course. It's free and takes a second. "
                    "Or send a brief from your website, or paste one on the dashboard; you can chat about it straight away.</p>"
                    f"<p>{sample_button(k)}</p></div>", request=request)
    want = request.query_params.get("course", "")
    opts = "".join(
        f"<option value='{j['id']}' data-review='{html.escape(jobs.page_url(j))}' data-status='{j['status']}' "
        f"data-sb='{1 if (jobs.job_dir(j['id']) / 'output' / 'storyboard.json').exists() else 0}' "
        f"data-pack='{','.join(p.stem for p in (jobs.job_dir(j['id']) / 'output' / 'pack').glob('*.md') if '_before_' not in p.stem)}'"
        f"{' selected' if j['id'] == want else ''}>"
        f"{html.escape((j.get('title') or 'New brief')[:70])} ({html.escape(j['client_name'][:30])})</option>" for j in all_jobs)
    sel = next((j for j in all_jobs if j["id"] == want), all_jobs[0])
    body = f"""<style>{TEAM_CSS}</style>
<p class=crumb><a href='/dashboard'>← All courses</a></p><h1>Talk to the team</h1>
<p class=muted>A private chat with one team member about one course. When Iris or Theo suggest a change, nothing is saved until you press <b>Save to course</b>. Saving keeps a backup and rebuilds the Word storyboard.</p>
<p class=row><select id=course aria-label="Course">{opts}</select> <a id=open class="btn ghost" href="{html.escape(jobs.page_url(sel))}">Open course page</a> {sample_button(k, "Add sample course")}</p>
<div class=tabs role=tablist><button data-a=iris>Iris · design</button><button data-a=theo>Theo · storyboard</button><button data-a=quinn>Quinn · QA</button>
<button data-a=sam>Sam · SME</button><button data-a=vera>Vera · visuals</button><button data-a=mila>Mila · voiceover</button><button data-a=dev>Dev · build</button>
<button data-a=lars>Lars · LMS</button><button data-a=nia>Nia · evaluation</button><button data-a=paige>Paige · project</button></div>
<p class=muted id=who></p><p id=why class=muted hidden></p><div id=log aria-live=polite></div>
<form id=form><label for=msg><b>Your message</b></label><textarea id=msg></textarea><p class=muted id=wait hidden></p><p id=err></p>
<p class=row><button id=send>Send</button> <button type=button class=ghost id=clear>Clear this chat</button></p></form>
<script>{TEAM_JS}</script>"""
    return page("Talk to the team", body, request=request)


async def team_chat(request: Request):
    if blocked := gate(request, "designer", api=True):
        return blocked
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "Bad request."}, 400)
    job = _admin_job(body.get("job_id"))
    agent = body.get("agent")
    if not job or agent not in agent_chat.AGENTS:
        return JSONResponse({"error": "Pick a course and an agent."}, 400)
    history = body.get("history") if isinstance(body.get("history"), list) else []
    data, status = await agent_chat.reply(job, agent, history)
    return JSONResponse(data, status)


async def team_apply(request: Request):
    if blocked := gate(request, "designer", api=True):
        return blocked
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "Bad request."}, 400)
    job = _admin_job(body.get("job_id"))
    if not job:
        return JSONResponse({"error": "Course not found."}, 404)
    if job["status"] in jobs.WORKING:
        return JSONResponse({"error": "The team is working on this course right now. Press Save again once the current step has finished (the course page shows progress)."}, 409)
    try:
        message = await asyncio.to_thread(agent_chat.apply, job, str(body.get("proposal_id") or ""))
    except KeyError:
        return JSONResponse({"error": "That change was already saved or has expired."}, 404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, 409)
    return JSONResponse({"message": message})


# ---------------------------------------------------------------- course files (everything in a course's folder)

FILE_HELP = {
    "brief.txt": "The client's brief, exactly as received",
    "job.json": "The course's status record and history",
    "1_design.md": "Iris's course design (readable)",
    "1_design.json": "Iris's course design (raw data)",
    "1_design_APPROVED.txt": "Marker that you approved the design",
    "storyboard.docx": "The Word storyboard to send for review",
    "storyboard.md": "The storyboard as plain text",
    "storyboard.json": "The storyboard (raw data)",
    "qa_report.md": "Quinn's QA report",
    "run_log.json": "Time and cost of each step",
    "production_pack.docx": "The production pack: all seven team documents in one Word file",
}


def _help(rel: str) -> str:
    name = rel.rsplit("/", 1)[-1]
    if name in FILE_HELP:
        return FILE_HELP[name]
    m = re.match(r"2_storyboard_draft(\d+)\.json$", name)
    if m:
        return f"Theo's storyboard, draft {m.group(1)}"
    m = re.match(r"3_qa_round(\d+)\.json$", name)
    if m:
        return f"Quinn's QA review, round {m.group(1)}"
    if rel.startswith("output/pack/"):
        for aid, n, r, _, t in jobs.pipeline.PACK_AGENTS:
            if name.startswith(aid):
                kind = "backup before a chat change" if "_before_" in name else ("readable" if name.endswith(".md") else "raw data")
                return f"{n}'s {t.lower()} ({kind})"
    if rel.startswith("output/course/"):
        if name == "course.json":
            return "The built course (edit it in the course editor)"
        if rel.startswith("output/course/versions/"):
            return "Earlier saved version of the course"
        if rel.startswith("output/course/assets/"):
            return "Picture uploaded to the course"
        if name == "build_notes.md":
            return "Dev's notes on how the storyboard was built"
        if name.startswith("module_"):
            return "Dev's build of one module (raw data)"
    if "_before_" in name:
        return "Backup made before you saved a change from the team chat"
    if rel.startswith("proposals/"):
        return "A change an agent suggested in the team chat" + (" (saved)" if name.endswith(".saved") else " (not saved)")
    return ""


def _course_and_file(request: Request) -> tuple[dict | None, Path | None]:
    if not auth.can(auth.current_user(request), "reviewer"):
        return None, None
    try:
        job = jobs.load(request.path_params["job_id"])
    except KeyError:
        return None, None
    root = jobs.job_dir(job["id"]).resolve()
    rel = request.query_params.get("f", "")
    if not rel:
        return job, None
    f = (root / rel).resolve()
    if root not in f.parents or not f.is_file():
        return job, None
    return job, f


def _rel(job: dict, f: Path) -> str:
    return f.resolve().relative_to(jobs.job_dir(job["id"]).resolve()).as_posix()


async def course_files_page(request: Request):
    if blocked := gate(request, "reviewer"):
        return blocked
    job, _ = _course_and_file(request)
    if not job:
        return not_found()
    root = jobs.job_dir(job["id"])
    files = sorted((p for p in root.rglob("*") if p.is_file() and not p.name.endswith(".tmp")),
                   key=lambda p: (p.parent != root, "proposals" in p.parts, p.parent.name, p.name))
    rows = []
    for p in files:
        rel = _rel(job, p)
        q = f"?f={html.escape(rel)}"
        size = p.stat().st_size
        size_txt = f"{size / 1024:.0f} KB" if size >= 1024 else f"{size} bytes"
        view = "" if p.suffix == ".docx" else f"<a href='/course-files/{job['id']}/view{q}'>Open</a> · "
        rows.append(
            f"<tr><td><b>{html.escape(rel)}</b><div class=muted>{html.escape(_help(rel))}</div></td>"
            f"<td class=muted style='white-space:nowrap'>{size_txt}<br>{time.strftime('%d %b %H:%M', time.gmtime(p.stat().st_mtime))} UTC</td>"
            f"<td style='white-space:nowrap'>{view}<a href='/course-files/{job['id']}/download{q}'>Download</a></td></tr>")
    body = (f"<p class=crumb><a href='/dashboard'>All courses</a> · <a href='{html.escape(jobs.page_url(job))}'>← Back to the course</a></p>"
            f"<h1>Course files</h1><p class=muted>{html.escape(job.get('title') or 'New brief')} · {html.escape(job['client_name'])}</p>"
            "<p>Everything saved for this course. <b>Open</b> shows a file here in the browser; <b>Download</b> saves it to your computer.</p>"
            f"<div class=card><table><tr><th>File</th><th>Size and date</th><th></th></tr>{''.join(rows)}</table></div>")
    return page("Course files", body, request=request)


async def course_file_view(request: Request):
    if blocked := gate(request, "reviewer"):
        return blocked
    job, f = _course_and_file(request)
    if not job or not f:
        return not_found()
    rel = _rel(job, f)
    text = f.read_text(encoding="utf-8", errors="replace")
    if f.suffix == ".md":
        shown = f"<div class='card design'>{markdown.markdown(text, extensions=['tables'])}</div>"
    elif f.suffix == ".json" or f.name.endswith(".saved"):
        try:
            text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except ValueError:
            pass
        if f.name == "job.json":
            text = text.replace(job["token"], "(private link code hidden)")
            if job.get("track_token"):
                text = text.replace(job["track_token"], "(client tracking code hidden)")
            if job.get("play_token"):
                text = text.replace(job["play_token"], "(course preview code hidden)")
        shown = f"<pre class=card style='white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px'>{html.escape(text)}</pre>"
    else:
        shown = f"<pre class=card style='white-space:pre-wrap;overflow-wrap:anywhere'>{html.escape(text)}</pre>"
    body = (f"<p class=crumb><a href='/course-files/{job['id']}'>← Back to course files</a></p>"
            f"<h1 style='overflow-wrap:anywhere'>{html.escape(rel)}</h1><p class=muted>{html.escape(_help(rel))}</p>"
            f"<p><a class='btn ghost' href='/course-files/{job['id']}/download?f={html.escape(rel)}'>Download</a></p>{shown}")
    return page(rel, body, request=request)


async def course_file_download(request: Request):
    if blocked := gate(request, "reviewer"):
        return blocked
    job, f = _course_and_file(request)
    if not job or not f:
        return not_found()
    if f.name == "job.json":  # never hand out the private email link codes
        text = f.read_text(encoding="utf-8").replace(job["token"], "(private link code hidden)")
        for k, label in (("track_token", "client tracking code"), ("play_token", "course preview code")):
            if job.get(k):
                text = text.replace(job[k], f"({label} hidden)")
        return Response(text, media_type="application/json", headers={"content-disposition": 'attachment; filename="job.json"'})
    return FileResponse(f, filename=f.name)


# ---------------------------------------------------------------- app

@asynccontextmanager
async def lifespan(_app):
    # Pasted keys sometimes carry spaces, line breaks or quotes; clean them once for everything.
    for k in ("ANTHROPIC_API_KEY", "RESEND_API_KEY", "NOTIFY_EMAIL"):
        if k in os.environ:
            os.environ[k] = os.environ[k].strip().strip('"').strip("'").strip()
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        log.info("Claude key loaded: starts %s..., %d characters", key[:10], len(key))
    missing = [k for k in ("ANTHROPIC_API_KEY", "RESEND_API_KEY", "NOTIFY_EMAIL", "DASHBOARD_KEY") if not os.environ.get(k)]
    if missing:
        log.warning("Not set yet: %s", ", ".join(missing))
    # The database: create tables, bring over logins from the SQLite file if Postgres was just added,
    # and add any course folders on the disk that the database doesn't know yet. Nothing is deleted.
    await asyncio.to_thread(db.init)
    await asyncio.to_thread(db.copy_sqlite_into_postgres)
    added = await asyncio.to_thread(jobs.import_from_disk)
    log.info("Database: %s. %d courses in it (%d just brought over from the disk).", db.kind(),
             len(await asyncio.to_thread(jobs.all_jobs)), added)
    log.info("Dashboard: %s/dashboard", jobs.public_url())
    mail_ok = bool(emailer.notify_address() and os.environ.get("RESEND_API_KEY"))
    welcome = jobs.DATA_DIR / "welcome_sent.txt"
    has_users = await asyncio.to_thread(auth.has_users)
    setup_link = f"{jobs.public_url()}/setup?key={quote(dashboard_key(), safe='')}"
    if dashboard_key() and mail_ok and not welcome.exists() and not has_users:
        body = (f"<p>Your Modulo service is live at <a href='{jobs.public_url()}'>{jobs.public_url()}</a>.</p>"
                "<p>First, create your owner login (it takes a minute). Keep this email private: the link works only once, "
                "for the first account.</p>"
                f"<p><a href='{setup_link}'>Create my Modulo login</a></p>"
                "<p>From now on, each brief a client sends from the website starts automatically, "
                "and you'll get an email when a course design is ready for your OK.</p>")
        if await asyncio.to_thread(emailer.send, "Your Modulo service is live", jobs._wrap(body)):
            jobs.DATA_DIR.mkdir(parents=True, exist_ok=True)
            welcome.write_text(time.ctime())
            await asyncio.to_thread(db.set_meta, "login_email_sent", str(time.time()))
    elif dashboard_key() and mail_ok and not has_users and not await asyncio.to_thread(db.get_meta, "login_email_sent"):
        body = ("<p>Modulo now has personal logins for you and your team, with roles (Admin, Designer, Reviewer). "
                "The old secret dashboard link is replaced by a sign-in page.</p>"
                "<p>Create your owner login now (it takes a minute). Keep this email private: the link works only once, "
                "for the first account.</p>"
                f"<p><a href='{setup_link}'>Create my Modulo login</a></p>"
                "<p>The Approve and Request changes buttons in your course emails keep working as before, without signing in.</p>")
        if await asyncio.to_thread(emailer.send, "Create your Modulo login", jobs._wrap(body)):
            await asyncio.to_thread(db.set_meta, "login_email_sent", str(time.time()))

    def backup_failed(result: dict) -> None:
        emailer.send("Modulo: last night's backup did not work", jobs._wrap(
            f"<p>The nightly backup did not work:</p><pre style='white-space:pre-wrap'>{html.escape(result.get('error', ''))}</pre>"
            f"<p>Your courses are safe; this only means there is no fresh backup copy. It tries again in a few hours. "
            f"See <a href='{jobs.public_url()}/admin/system'>the System page</a>.</p>"))

    tasks = [asyncio.create_task(jobs.worker()), asyncio.create_task(backup.nightly(backup_failed))]
    yield
    for t in tasks:
        t.cancel()
    await asyncio.to_thread(db.close)


app = Starlette(
    routes=[
        Route("/api/chat", api_chat, methods=["POST"]),
        Route("/api/brief", api_brief, methods=["POST"]),
        Route("/health", health),
        Route("/review/{job_id}", review),
        Route("/review/{job_id}/{action:str}", review_action, methods=["POST"]),
        Route("/files/{job_id}/{name}", files),
        Route("/dashboard", dashboard),
        Route("/dashboard/new", dashboard_new, methods=["POST"]),
        Route("/dashboard/sample", dashboard_sample, methods=["POST"]),
        Route("/team", team_page),
        Route("/course-files/{job_id}", course_files_page),
        Route("/course-files/{job_id}/view", course_file_view),
        Route("/course-files/{job_id}/download", course_file_download),
        Route("/api/team/chat", team_chat, methods=["POST"]),
        Route("/api/team/apply", team_apply, methods=["POST"]),
        *builder_routes.ROUTES,
        *accounts.ROUTES,
        *tracking.ROUTES,
        Mount("/", StaticFiles(directory=HERE / "public", html=True)),
    ],
    lifespan=lifespan,
)
