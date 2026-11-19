"""The client tracking page: a private, parcel-tracker style page for each course.

  /track/<code>                    the page the client opens (no sign-in; the long random code is the key)
  /track/<code>/status             the same tracker as JSON, so the open page updates by itself
  /track/<code>/course/...         the finished course, once the studio has pressed "Share with client"
  /track/<code>/download/<kind>    the SCORM zip, only when the studio allowed downloads when sharing

Each course gets its own code (job["track_token"]), separate from the studio's private email link code
and from the "Try the course" code, so the client's link can never approve, spend or see internal files.
The client only ever sees friendly stage names and dates: no costs, QA notes, files or errors.

The studio controls it from the course page (see admin_card): copy or email the link, Share with client
(which unlocks the last stop, "Ready"), stop sharing, and turn the link off or back on.
"""

from __future__ import annotations

import asyncio
import hmac
import html
import json
import re
import secrets
import time

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

import db
import emailer
import jobs

TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{20,64}")

# (title the client sees, who does it)
STAGES = [
    ("Brief received", ""),
    ("Course design", "Iris, instructional designer"),
    ("Design approved", ""),
    ("Storyboard written", "Theo, storyboard writer"),
    ("Quality check", "Quinn, quality reviewer"),
    ("Course being built", "Dev, eLearning developer"),
    ("Final check by our team", ""),
    ("Ready", ""),
]
# How the headline says the stop that's in progress
NOW = ["Brief received", "Designing your course", "Design review", "Writing the storyboard", "Quality check",
       "Building your course", "Final check by our team", "Ready"]


# ---------------------------------------------------------------- links

def token(job: dict) -> str:
    """The course's tracking code, made the first time it's needed (courses from before this feature)."""
    if not job.get("track_token"):
        job["track_token"] = secrets.token_urlsafe(24)
        jobs.save(job)
    return job["track_token"]


def url(job: dict) -> str:
    return f"{jobs.public_url()}/track/{token(job)}"


def find(code: str) -> dict | None:
    """The course with this tracking code, or None. Turned-off links count as not found."""
    if not TOKEN_RE.fullmatch(code or ""):
        return None
    # Narrow down in the database, then compare exactly (LIKE is case-insensitive in SQLite and "_" is a wildcard).
    for row in db.query("SELECT data FROM jobs WHERE data LIKE ?", (f'%"track_token": "{code}"%',)):
        job = json.loads(row["data"])
        if job.get("track_token") and hmac.compare_digest(job["track_token"], code):
            return job
    return None


# ---------------------------------------------------------------- where the course is

def _when(job: dict, *prefixes: str, last: bool = True) -> float | None:
    hits = [h["at"] for h in job.get("history", []) if any(h.get("text", "").startswith(p) for p in prefixes)]
    if not hits:
        return None
    return hits[-1] if last else hits[0]


def _mtime(path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def is_shared(job: dict) -> bool:
    return bool(job.get("shared_at")) and jobs.course_ready(job)


def stages(job: dict) -> dict:
    """The eight stops: each done (with a date), current (with a short note) or still to come."""
    out = jobs.job_dir(job["id"]) / "output"
    st = job["status"]
    design = (out / "1_design.json").exists() and st not in {"designing", "revising"}
    approved = (out / "1_design_APPROVED.txt").exists()
    draft1 = out / "2_storyboard_draft1.json"
    storyboard_done = (out / "storyboard.json").exists() and st != "storyboarding"
    written = draft1.exists() or storyboard_done
    built = jobs.course_ready(job)
    shared = is_shared(job)
    done = [True, design, approved, written, storyboard_done, built, shared, shared]
    dates = [
        job.get("created"),
        _when(job, "Design ready"),
        _when(job, "Design approved") or _mtime(out / "1_design_APPROVED.txt"),
        _mtime(draft1) or _when(job, "Storyboard finished"),
        _when(job, "Storyboard finished"),
        _when(job, "Dev built the course", "Quick course draft made"),
        job.get("shared_at"),
        job.get("shared_at"),
    ]
    for i in range(1, len(done)):  # a stop only counts as done when the ones before it are
        done[i] = done[i] and done[i - 1]
    current = next((i for i, d in enumerate(done) if not d), None)

    note = ""
    if current is not None:
        if st == "failed":
            note = "A short pause while our team sorts out a detail. Nothing is needed from you."
        elif current == 1:
            note = ("Updating the design with your feedback" if st == "revising"
                    else "Iris is shaping the course: goals, structure and activities")
        elif current == 2:
            note = "Our team is reviewing the course design"
        elif current == 3:
            note = "Theo is writing the course screen by screen"
        elif current == 4:
            note = "Quinn is checking every screen for accuracy, clarity and accessibility"
        elif current == 5:
            note = {"building": "Dev is building your interactive course",
                    "packing": "Our team is planning the media and production"}.get(st, "Getting ready to build your course")
        elif current == 6:
            note = "Our team is trying the course and giving it a final polish"
    return {
        "stops": [{"title": t, "who": w, "done": d, "at": (a if d else None)}
                  for (t, w), d, a in zip(STAGES, done, dates)],
        "current": current,
        "note": note,
        "shared": shared,
        "scorm": shared and bool(job.get("share_scorm")),
        "updated": job.get("updated") or job.get("created"),
    }


# ---------------------------------------------------------------- the client's page

CSS = """
:root{--bg:#f6f7fb;--fg:#181b2b;--muted:#5d6275;--line:#dfe2ee;--card:#fff;--accent:#2f3e9e;--soft:#eef0fb;--ok:#1d7a46;--okbg:#e5f4ea}
@media (prefers-color-scheme:dark){:root{--bg:#11121a;--fg:#ecedf3;--muted:#a3a7b8;--line:#2c2e3c;--card:#1a1b25;--accent:#9aa7ff;--soft:#252845;--ok:#62d394;--okbg:#173225}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:640px;margin:0 auto;padding:28px 16px 60px}
.brand{font-weight:700;letter-spacing:.02em;color:var(--accent);margin:0 0 22px}
h1{font-size:24px;line-height:1.25;margin:0 0 6px;overflow-wrap:anywhere}.muted{color:var(--muted)}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px;margin:18px 0}
.head .now{font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0}
.head .big{font-size:22px;font-weight:700;margin:2px 0 4px}
.bar{height:10px;background:var(--soft);border-radius:999px;overflow:hidden;margin:14px 0 6px}
.bar span{display:block;height:100%;background:var(--accent);border-radius:999px;transition:width .6s}
ol.stops{list-style:none;margin:0;padding:0}
.stop{display:grid;grid-template-columns:34px 1fr;gap:0 14px;position:relative;padding:0 0 22px}
.stop:last-child{padding-bottom:0}
.stop:not(:last-child)::before{content:"";position:absolute;left:16px;top:30px;bottom:-2px;width:2px;background:var(--line)}
.stop.done:not(:last-child)::before{background:var(--ok)}
.dot{width:34px;height:34px;border-radius:50%;display:grid;place-items:center;border:2px solid var(--line);background:var(--card);color:var(--muted);font-size:14px;font-weight:700;z-index:1}
.done .dot{background:var(--ok);border-color:var(--ok);color:#fff}
.current .dot{border-color:var(--accent);color:var(--accent);box-shadow:0 0 0 5px var(--soft)}
.current .dot::after{content:"";width:12px;height:12px;border-radius:50%;background:var(--accent);animation:pulse 1.6s ease-in-out infinite}
@keyframes pulse{50%{opacity:.35}}@media (prefers-reduced-motion:reduce){.current .dot::after{animation:none}.bar span{transition:none}}
.stop .t{font-weight:600;padding-top:5px}.todo .t{color:var(--muted);font-weight:500}
.stop .s{font-size:14px;color:var(--muted)}.current .s.note{color:var(--fg);background:var(--soft);border-radius:10px;padding:8px 12px;margin-top:6px}
.ready{border-color:var(--ok);background:var(--okbg)}.ready h2{margin:0 0 6px;font-size:20px}
.btn{display:inline-block;font:600 15px/1 system-ui,sans-serif;padding:13px 22px;border-radius:999px;border:1px solid var(--accent);background:var(--accent);color:#fff;text-decoration:none;margin:6px 8px 0 0}
.btn.ghost{background:transparent;color:var(--accent)}
.foot{font-size:13px;color:var(--muted);margin-top:28px}
"""

JS = r"""
const fmt=t=>new Date(t*1000).toLocaleString([], {day:'numeric',month:'short',year:'numeric',hour:'numeric',minute:'2-digit'});
function dates(){document.querySelectorAll('[data-ts]').forEach(e=>{e.textContent=fmt(+e.dataset.ts)})}
dates();
async function refresh(){
  try{const r=await fetch(location.pathname.replace(/\/$/,'')+'/status',{cache:'no-store'});
    if(r.status===404){location.reload();return}
    const j=await r.json(); if(j.html!==document.getElementById('tracker').innerHTML){document.getElementById('tracker').innerHTML=j.html;dates();}
    if(j.shared) return;
  }catch(e){}
  setTimeout(refresh,30000);
}
setTimeout(refresh,30000);
"""


def _date(ts: float | None) -> str:
    if not ts:
        return ""
    return f"<span data-ts='{ts:.0f}'>{time.strftime('%d %b %Y, %H:%M UTC', time.gmtime(ts))}</span>"


def tracker_html(job: dict) -> str:
    s = stages(job)
    code = html.escape(job["track_token"])
    n_done = sum(st["done"] for st in s["stops"])
    total = len(s["stops"])
    if s["shared"]:
        head = "<p class=now>Delivered</p><p class=big>Your course is ready</p>"
    else:
        cur = NOW[s["current"]]
        head = f"<p class=now>Now</p><p class=big>{html.escape(cur)}</p>"
    pct = 100 * n_done / total
    parts = [f"<div class='card head'>{head}<div class=bar role=progressbar aria-valuemin=0 aria-valuemax={total} "
             f"aria-valuenow={n_done} aria-label='Progress'><span style='width:{pct:.0f}%'></span></div>"
             f"<div class=muted style='font-size:14px'>Step {min(n_done + (0 if s['shared'] else 1), total)} of {total} · "
             f"Last update {_date(s['updated'])}</div></div>"]
    if s["shared"]:
        dl = ""
        if s["scorm"]:
            dl = (f"<a class='btn ghost' href='/track/{code}/download/scorm12'>Download SCORM 1.2</a>"
                  f"<a class='btn ghost' href='/track/{code}/download/scorm2004'>Download SCORM 2004</a>"
                  "<p class=muted style='font-size:14px;margin:10px 0 0'>The SCORM files are for uploading to your learning "
                  "management system (LMS). If you're not sure which one, ask your LMS administrator.</p>")
        parts.append(f"<div class='card ready'><h2>Your course is ready</h2>"
                     "<p style='margin:0 0 6px'>Try it the way your learners will. It works on a computer, tablet or phone.</p>"
                     f"<a class=btn href='/track/{code}/course/index.html' target=_blank rel=noopener>Try your course</a>{dl}</div>")
    items = []
    for i, st in enumerate(s["stops"]):
        state = "done" if st["done"] else "current" if i == s["current"] else "todo"
        mark = "&#10003;" if st["done"] else ("" if state == "current" else str(i + 1))
        sub = []
        if st["who"] and state != "todo":
            sub.append(f"<div class=s>{html.escape(st['who'])}</div>")
        if st["done"] and st["at"]:
            sub.append(f"<div class=s>{_date(st['at'])}</div>")
        elif st["done"]:
            sub.append("<div class=s>Done</div>")
        if state == "current" and s["note"]:
            sub.append(f"<div class='s note'>{html.escape(s['note'])}</div>")
        label = {"done": "done", "current": "in progress", "todo": "to come"}[state]
        items.append(f"<li class='stop {state}'{' aria-current=step' if state == 'current' else ''}>"
                     f"<span class=dot aria-hidden=true>{mark}</span><div><div class=t>{html.escape(st['title'])}"
                     f"<span style='position:absolute;left:-9999px'> ({label})</span></div>{''.join(sub)}</div></li>")
    parts.append(f"<div class=card><ol class=stops>{''.join(items)}</ol></div>")
    return "".join(parts)


def _page(title: str, body: str, status: int = 200, script: bool = False) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<meta name=robots content=noindex><title>{html.escape(title)}</title><style>{CSS}</style></head>"
        f"<body><main><p class=brand>Modulo</p>{body}</main>{'<script>' + JS + '</script>' if script else ''}</body></html>",
        status_code=status,
        headers={"cache-control": "no-store", "referrer-policy": "no-referrer", "x-robots-tag": "noindex"})


def _missing() -> HTMLResponse:
    return _page("Link not available", "<h1>This tracking link isn't available</h1>"
                 "<p class=muted>The link may be mistyped, or it has been turned off. Please contact the Modulo team "
                 "and we'll send you a fresh one.</p>", 404)


def _live(request: Request) -> dict | None:
    job = find(request.path_params["code"])
    if not job or job.get("track_off"):
        return None
    return job


async def page(request: Request):
    job = await asyncio.to_thread(_live, request)
    if not job:
        return _missing()
    first = (job.get("client_name") or "").split(" ")[0]
    title = job.get("title") or "Your new course"
    body = (f"<h1>{html.escape(title)}</h1>"
            f"<p class=muted style='margin:0'>{('Hi ' + html.escape(first) + '. ') if first else ''}"
            "Here's where your course is. This page updates by itself, so you can keep it open or come back any time.</p>"
            f"<div id=tracker aria-live=polite>{tracker_html(job)}</div>"
            "<p class=foot>Keep this link to yourself: anyone with it can see this page. Questions? Just get in touch with the Modulo team.</p>")
    return _page(title, body, script=True)


async def status(request: Request):
    job = await asyncio.to_thread(_live, request)
    if not job:
        return JSONResponse({"error": "not found"}, 404, headers={"cache-control": "no-store"})
    return JSONResponse({"html": tracker_html(job), "shared": is_shared(job)}, headers={"cache-control": "no-store"})


async def course(request: Request):
    job = await asyncio.to_thread(_live, request)
    if not job or not is_shared(job):
        return _missing()
    from authoring import routes as builder_routes
    return builder_routes.serve_course(job, request.path_params["path"])


async def download(request: Request):
    from authoring import course as C, export as E
    job = await asyncio.to_thread(_live, request)
    kind = request.path_params["kind"]
    if not job or not is_shared(job) or not job.get("share_scorm") or kind not in ("scorm12", "scorm2004"):
        return _missing()
    out = jobs.job_dir(job["id"]) / "output"
    c = C.load(out)
    if not c:
        return _missing()
    data = await asyncio.to_thread(E.build, c, C.course_dir(out) / "assets", kind)
    return Response(data, media_type="application/zip", headers={
        "content-disposition": f'attachment; filename="{E.filename(c, kind)}"', "cache-control": "no-store"})


ROUTES = [
    Route("/track/{code}", page),
    Route("/track/{code}/status", status),
    Route("/track/{code}/course/{path:path}", course),
    Route("/track/{code}/download/{kind}", download),
]


# ---------------------------------------------------------------- studio actions (from the course page)

def _client_email(job: dict, subject: str, body: str) -> bool:
    if not emailer.can_email_clients() or job.get("source") == "sample":
        return False
    return emailer.send(subject, jobs._wrap(body), to=job["client_email"])


def email_link(job: dict) -> bool:
    """Sends the client their tracking link. Returns True if the email was accepted."""
    first = html.escape((job.get("client_name") or "").split(" ")[0] or "there")
    body = (f"<p>Hi {first},</p><p>Thanks for your course brief. You can follow your course from brief to finished course "
            "on your private tracking page:</p>"
            f"<p><a href=\"{url(job)}\" style=\"{jobs.BTN}background:#2f3e9e;color:#fff\">Track my course</a></p>"
            "<p style=\"color:#555\">The page updates by itself as our team works. Please keep the link to yourself.</p>")
    return _client_email(job, "Track your course with Modulo", body)


def email_ready(job: dict) -> bool:
    first = html.escape((job.get("client_name") or "").split(" ")[0] or "there")
    title = html.escape(job.get("title") or "your course")
    body = (f"<p>Hi {first},</p><p>Good news: <b>{title}</b> is ready.</p>"
            f"<p><a href=\"{url(job)}\" style=\"{jobs.BTN}background:#2f3e9e;color:#fff\">Open my course page</a></p>"
            "<p style=\"color:#555\">You can try the course there"
            + (" and download it for your learning management system" if job.get("share_scorm") else "") + ".</p>")
    return _client_email(job, f"Your course is ready: {job.get('title') or 'Modulo'}", body)


def act(job: dict, action: str, form, who: str | None) -> str:
    """One of the studio's buttons on the course page. Returns a short message for the page."""
    if action == "share":
        if not jobs.course_ready(job):
            return "The course has to be built before you can share it."
        token(job)
        job["shared_at"] = time.time()
        job["share_scorm"] = str(form.get("scorm") or "") == "1"
        jobs.note(job, "Shared with the client" + (" (with SCORM download)" if job["share_scorm"] else ""), who)
        sent = email_ready(job)
        return "Shared. The client's page now shows Ready" + (" and we emailed them." if sent else ".")
    if action == "unshare":
        job.pop("shared_at", None)
        job["share_scorm"] = False
        jobs.note(job, "Stopped sharing the course with the client", who)
        return "Stopped sharing. The client's page is back at Final check."
    if action == "track_off":
        job["track_off"] = True
        jobs.note(job, "Client tracking link turned off", who)
        return "The client's tracking link is off."
    if action == "track_on":
        job["track_off"] = False
        token(job)
        jobs.note(job, "Client tracking link turned back on", who)
        return "The client's tracking link works again."
    if action == "track_email":
        if email_link(job):
            jobs.note(job, "Tracking link emailed to the client", who)
            return f"Emailed the tracking link to {job['client_email']}."
        return "The email could not be sent. Copy the link and send it yourself."
    if action == "track_new":
        job["track_token"] = secrets.token_urlsafe(24)
        job["track_off"] = False
        jobs.note(job, "Client tracking link replaced with a new one", who)
        return "Made a new link. The old one no longer works."
    return ""


ACTIONS = {"share", "unshare", "track_off", "track_on", "track_email", "track_new"}


def admin_card(job: dict, t: str) -> str:
    """The "Client tracking page" box on the course page, for Designers and Admins."""
    link = url(job)
    jid = job["id"]
    off = bool(job.get("track_off"))

    def button(action: str, label: str, ghost: bool = True, extra: str = "") -> str:
        return (f"<form method=post action='/review/{jid}/{action}?t={t}' style='display:inline'>{extra}"
                f"<button{' class=ghost' if ghost else ''}>{label}</button></form>")

    s = stages(job)
    where = "Ready (shared)" if s["shared"] else s["stops"][s["current"]]["title"]
    if is_shared(job):
        share = (f"<p><b>Shared with the client</b> on {time.strftime('%d %b %Y', time.gmtime(job['shared_at']))}. "
                 f"SCORM download: {'allowed' if job.get('share_scorm') else 'not allowed'}.</p>"
                 + button("unshare", "Stop sharing"))
    elif jobs.course_ready(job):
        share = ("<p>When you're happy with the course, share it. The client's page then shows <b>Ready</b> "
                 "with a <b>Try your course</b> button.</p>"
                 + button("share", "Share with client", ghost=False,
                          extra="<label style='font-weight:400;margin:0 0 10px'><input type=checkbox name=scorm value=1 "
                                "style='width:auto;margin-right:8px'>Also let them download the SCORM files</label>"))
    else:
        share = ("<p class=muted>The last stop, <b>Ready</b>, unlocks when you press <b>Share with client</b>. "
                 "That button appears here once the course is built.</p>")
    mail = (button("track_email", "Email the link to the client") if emailer.can_email_clients() else
            "<p class=muted style='font-size:14px'>To email clients directly, verify your domain in Resend and set "
            "EMAIL_FROM in Render. Until then, copy the link and send it yourself.</p>")
    state = (f"<p class=err><b>The link is turned off.</b> The client sees a \"link not available\" page.</p>"
             f"<p class=row>{button('track_on', 'Turn the link back on', ghost=False)} {button('track_new', 'Make a new link instead')}</p>"
             if off else
             f"<p class=row>{button('track_off', 'Turn link off')} {button('track_new', 'Make a new link')}</p>")
    return f"""<div class=card id=client><h2 style="margin-top:0">Client tracking page</h2>
<p class=muted>A private page where {html.escape(job['client_name'])} follows the course like a parcel. It shows only friendly stage names and dates:
no files, costs or QA notes. Right now it shows: <b>{html.escape(where)}</b>.</p>
<div class=row><input id=tracklink readonly value="{html.escape(link)}" style="flex:1;min-width:220px" onclick="this.select()">
<button type=button class=ghost onclick="navigator.clipboard.writeText(document.getElementById('tracklink').value).then(()=>{{this.textContent='Copied'}})">Copy</button>
<a class="btn ghost" href="{html.escape(link)}" target=_blank rel=noopener>Open as the client</a></div>
{mail}{share}{state}</div>"""
