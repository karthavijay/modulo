"""Modulo on Render: one service for the website, the chat and course production.

  /                 the website (public/index.html)
  /api/chat         the agent team chat on the website
  /api/brief        the website's "Send brief" button; starts production automatically
  /review/<id>      a course's page: read the design, approve or request changes, download results
  /files/<id>/...   downloads for a course
  /dashboard        every course and its status (link is in each email)
  /health           for Render's health check
"""

from __future__ import annotations

import asyncio
import hmac
import html
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import chat
import emailer
import jobs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("modulo")
HERE = Path(__file__).resolve().parent
MAX_BRIEFS_PER_DAY = int(os.environ.get("MAX_BRIEFS_PER_DAY", "10"))
DOWNLOADS = {"storyboard.docx", "qa_report.md", "storyboard.md", "1_design.md", "run_log.json"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def dashboard_key() -> str:
    return os.environ.get("DASHBOARD_KEY", "")


def dashboard_url() -> str:
    return f"{jobs.public_url()}/dashboard?key={dashboard_key()}"


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
    return JSONResponse({"ok": True})


async def health(_: Request):
    return PlainTextResponse("ok")


# ---------------------------------------------------------------- pages

CSS = """
:root{--bg:#f7f7fb;--fg:#1c1f2e;--muted:#5d6275;--line:#e1e3ee;--card:#fff;--accent:#2f3e9e;--soft:#eef0fb;--ok:#1d7a46;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#12131a;--fg:#ecedf3;--muted:#a3a7b8;--line:#2b2d3a;--card:#1b1c25;--accent:#9aa7ff;--soft:#252845;--ok:#62d394;--bad:#ff8a80}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:860px;margin:0 auto;padding:28px 16px 60px}h1{font-size:26px;margin:0 0 4px}h2{font-size:20px;margin:28px 0 8px}
.muted{color:var(--muted)}.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 20px;margin:16px 0}
.pill{display:inline-block;font-size:13px;font-weight:600;padding:3px 10px;border-radius:999px;background:var(--soft);color:var(--accent)}
.pill.done{color:var(--ok)}.pill.failed{color:var(--bad)}
button,.btn{display:inline-block;font:600 15px/1 inherit;font-family:inherit;padding:12px 20px;border-radius:999px;border:1px solid var(--accent);background:var(--accent);color:#fff;cursor:pointer;text-decoration:none}
.btn.ghost,button.ghost{background:transparent;color:var(--accent)}
textarea,input{width:100%;font:15px/1.4 inherit;font-family:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--fg)}
textarea{min-height:120px}.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:top}
.design h1{font-size:22px}.design h2{font-size:18px}.design h3{font-size:16px}.design{overflow-wrap:anywhere}
:target{outline:3px solid var(--accent);outline-offset:4px;border-radius:14px}
"""

STATUS_TEXT = {
    "designing": "Iris is writing the design",
    "revising": "Iris is revising the design",
    "awaiting_approval": "Waiting for your approval",
    "storyboarding": "Storyboard in progress",
    "done": "Finished",
    "failed": "Stopped with a problem",
}


def page(title: str, body: str, refresh: bool = False) -> HTMLResponse:
    meta = '<meta http-equiv="refresh" content="30">' if refresh else ""
    return HTMLResponse(
        f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<meta name=robots content=noindex>{meta}<title>{html.escape(title)}</title><style>{CSS}</style></head>"
        f"<body><main>{body}</main></body></html>",
        headers={"cache-control": "no-store", "referrer-policy": "no-referrer"},
    )


def pill(status: str) -> str:
    cls = "done" if status == "done" else "failed" if status == "failed" else ""
    return f'<span class="pill {cls}">{html.escape(STATUS_TEXT.get(status, status))}</span>'


def get_job(request: Request) -> dict | None:
    try:
        job = jobs.load(request.path_params["job_id"])
    except KeyError:
        return None
    token = request.query_params.get("t", "")
    return job if hmac.compare_digest(token, job["token"]) else None


def not_found() -> HTMLResponse:
    r = page("Not found", "<h1>Link not found</h1><p class=muted>This link is wrong or has expired. Use the link in your latest email.</p>")
    r.status_code = 404
    return r


async def review(request: Request):
    job = get_job(request)
    if not job:
        return not_found()
    t = html.escape(job["token"])
    jid = job["id"]
    st = job["status"]
    out = jobs.job_dir(jid) / "output"
    parts = [
        f"<p class=muted><a href='{html.escape(dashboard_url())}'>All courses</a></p>",
        f"<h1>{html.escape(job.get('title') or 'New course brief')}</h1>",
        f"<p class=muted>Client: {html.escape(job['client_name'])} ({html.escape(job['client_email'])})</p>",
        f"<p>{pill(st)} <span class=muted>{html.escape(job.get('step', ''))}</span></p>",
    ]
    if st == "awaiting_approval":
        parts.append(f"""
<div class=card id=approve><h2 style="margin-top:0">Happy with this design?</h2>
<p>Approving starts the full storyboard. It takes about an hour and costs roughly $7 in Claude usage. You'll get an email with the Word storyboard when it's done.</p>
<form method=post action="/review/{jid}/approve?t={t}"><button>Approve and start the storyboard</button></form></div>
<div class=card id=changes><h2 style="margin-top:0">Request changes</h2>
<p class=muted>Write what you'd like Iris to change, in your own words. You'll get a new email with the revised design in a few minutes.</p>
<form method=post action="/review/{jid}/changes?t={t}"><textarea name=feedback required placeholder="For example: make it 15 minutes and add a module on reporting near misses"></textarea>
<p><button class=ghost>Send changes to Iris</button></p></form></div>""")
    elif st in jobs.WORKING:
        parts.append("<div class=card><p style='margin:0'>The team is working on it. This page refreshes by itself, "
                     "and you'll get an email when there's something to look at. You can close this page.</p></div>")
    elif st == "failed":
        parts.append(f"""<div class=card><h2 style="margin-top:0">Something went wrong</h2>
<pre style="white-space:pre-wrap">{html.escape(job.get('error', ''))}</pre>
<p>Common causes: the Claude account ran out of credit or hit its spending limit, or the API key is wrong. Fix that, then try again. Finished steps are kept.</p>
<form method=post action="/review/{jid}/retry?t={t}"><button>Try again</button></form></div>""")
    if st == "done":
        s = job.get("summary", {})
        parts.append(f"""<div class=card><h2 style="margin-top:0">Downloads</h2>
<p>{s.get('screens', '?')} screens. Final QA {'passed' if s.get('passed') else 'has open points, see the QA report'}. Cost ${job.get('cost_usd', 0):.2f}.</p>
<p class=row><a class=btn href="{html.escape(jobs.file_url(job, 'storyboard.docx'))}">Storyboard (Word)</a>
<a class="btn ghost" href="{html.escape(jobs.file_url(job, 'qa_report.md'))}">QA report</a></p></div>""")
    if (out / "1_design.md").exists():
        parts.append(f"<div class='card design'>{jobs.design_html(job)}</div>")
    parts.append("<h2>History</h2><table>" + "".join(
        f"<tr><td class=muted style='white-space:nowrap'>{time.strftime('%d %b %H:%M UTC', time.gmtime(h['at']))}</td><td>{html.escape(h['text'])}</td></tr>"
        for h in reversed(job.get("history", []))) + "</table>")
    return page(job.get("title") or "Course", "".join(parts), refresh=st in jobs.WORKING)


async def review_action(request: Request):
    job = get_job(request)
    if not job:
        return not_found()
    action = request.path_params["action"]
    if action == "approve":
        jobs.approve(job)
    elif action == "changes":
        form = await request.form()
        jobs.request_changes(job, str(form.get("feedback") or ""))
    elif action == "retry":
        jobs.retry(job)
    return RedirectResponse(f"/review/{job['id']}?t={job['token']}", 303)


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


def dashboard_ok(request: Request) -> bool:
    key = dashboard_key()
    return bool(key) and hmac.compare_digest(request.query_params.get("key", ""), key)


async def dashboard(request: Request):
    if not dashboard_ok(request):
        return not_found()
    k = html.escape(dashboard_key())
    rows = "".join(
        f"<tr><td><a href='{html.escape(jobs.links(j)['review'])}'>{html.escape(j.get('title') or 'New brief')}</a>"
        f"<div class=muted>{html.escape(j['client_name'])}</div></td><td>{pill(j['status'])}</td>"
        f"<td class=muted>{time.strftime('%d %b %H:%M', time.gmtime(j['created']))}</td><td>${j.get('cost_usd', 0):.2f}</td></tr>"
        for j in jobs.all_jobs()) or "<tr><td colspan=4 class=muted>No courses yet. Briefs sent from the website appear here.</td></tr>"
    email_note = "" if os.environ.get("RESEND_API_KEY") and os.environ.get("NOTIFY_EMAIL") else (
        "<div class=card style='border-color:var(--bad)'><b>Emails are switched off.</b> Add RESEND_API_KEY and NOTIFY_EMAIL in Render, "
        "under your service's Environment page, to get approval emails.</div>")
    fake_note = "<div class=card><b>Test mode is on</b> (MODULO_FAKE). Courses use the sample forklift answers and cost nothing.</div>" if os.environ.get("MODULO_FAKE") else ""
    body = f"""<h1>Modulo courses</h1><p class=muted>Every brief and where it is. Bookmark this page.</p>{email_note}{fake_note}
<div class=card><table><tr><th>Course</th><th>Status</th><th>Received (UTC)</th><th>Cost</th></tr>{rows}</table></div>
<div class=card><h2 style="margin-top:0">Start a course from a brief you received elsewhere</h2>
<p class=muted>Paste a brief from an email or a document. It goes through the same steps as a website brief.</p>
<form method=post action="/dashboard/new?key={k}"><div class=row><input name=name placeholder="Client name" required style="flex:1;min-width:180px">
<input name=email type=email placeholder="Client email" required style="flex:1;min-width:180px"></div>
<p><textarea name=transcript required placeholder="Paste the brief here"></textarea></p><button>Start</button></form></div>"""
    return page("Modulo courses", body, refresh=any(j["status"] in jobs.WORKING for j in jobs.all_jobs()))


async def dashboard_new(request: Request):
    if not dashboard_ok(request):
        return not_found()
    form = await request.form()
    name, email, transcript = (str(form.get(k) or "").strip() for k in ("name", "email", "transcript"))
    if name and email and transcript:
        job = jobs.create(name[:120], email[:200], transcript[:60000], source="pasted on the dashboard")
        return RedirectResponse(jobs.links(job)["review"].replace(jobs.public_url(), ""), 303)
    return RedirectResponse(f"/dashboard?key={dashboard_key()}", 303)


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
    log.info("Dashboard: %s/dashboard?key=<DASHBOARD_KEY>", jobs.public_url())
    welcome = jobs.DATA_DIR / "welcome_sent.txt"
    if dashboard_key() and emailer.notify_address() and os.environ.get("RESEND_API_KEY") and not welcome.exists():
        body = (f"<p>Your Modulo service is live at <a href='{jobs.public_url()}'>{jobs.public_url()}</a>.</p>"
                f"<p>Your private dashboard, with every course and its status, is here. Bookmark it:</p>"
                f"<p><a href='{dashboard_url()}'>Open the Modulo dashboard</a></p>"
                "<p>From now on, each brief a client sends from the website starts automatically, "
                "and you'll get an email when a course design is ready for your OK.</p>")
        if await asyncio.to_thread(emailer.send, "Your Modulo service is live", jobs._wrap(body)):
            jobs.DATA_DIR.mkdir(parents=True, exist_ok=True)
            welcome.write_text(time.ctime())
    task = asyncio.create_task(jobs.worker())
    yield
    task.cancel()


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
        Mount("/", StaticFiles(directory=HERE / "public", html=True)),
    ],
    lifespan=lifespan,
)
