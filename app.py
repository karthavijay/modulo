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
import json
import html
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import markdown
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import agent_chat
import chat
import emailer
import jobs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("modulo")
HERE = Path(__file__).resolve().parent
MAX_BRIEFS_PER_DAY = int(os.environ.get("MAX_BRIEFS_PER_DAY", "10"))
DOWNLOADS = {"storyboard.docx", "qa_report.md", "storyboard.md", "1_design.md", "run_log.json", "production_pack.docx"}
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
    "packing": "Production pack in progress",
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
        f"<p class=muted><a href='{html.escape(dashboard_url())}'>All courses</a> · "
        f"<a href='/team?key={html.escape(dashboard_key())}&course={jid}'>Talk to the team about this course</a> · "
        f"<a href='/course-files/{jid}?key={html.escape(dashboard_key())}'>Course files</a></p>",
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
<a class="btn ghost" href="{html.escape(jobs.file_url(job, 'qa_report.md'))}">QA report</a>
{f'<a class="btn ghost" href="{html.escape(jobs.file_url(job, "production_pack.docx"))}">Production pack (Word)</a>' if (out / "production_pack.docx").exists() else ""}</p></div>""")
        if not job.get("pack_done"):
            team = "".join(f"<li><b>{n}</b>, {r}: {t.lower()}</li>" for _, n, r, _, t in jobs.pipeline.PACK_AGENTS)
            parts.append(f"""<div class=card id=pack><h2 style="margin-top:0">Optional: the production pack</h2>
<p>The rest of the team writes their documents from this storyboard:</p><ul>{team}</ul>
<p>It takes about 20 to 30 minutes and costs {html.escape(jobs.PACK_ESTIMATE)} in Claude usage. You'll get an email with one Word file when it's ready. They write documents only: no voice recording, image generation or LMS upload happens.</p>
<form method=post action="/review/{jid}/pack?t={t}"><button>Start the production pack</button></form></div>""")
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
    elif action == "pack":
        jobs.start_pack(job)
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
        f"<td class=muted>{time.strftime('%d %b %H:%M', time.gmtime(j['created']))}</td><td>${j.get('cost_usd', 0):.2f}</td>"
        f"<td><a href='/team?key={k}&course={j['id']}'>Talk to the team</a><br><a href='/course-files/{j['id']}?key={k}'>Course files</a></td></tr>"
        for j in jobs.all_jobs()) or "<tr><td colspan=5 class=muted>No courses yet. Briefs sent from the website appear here.</td></tr>"
    email_note = "" if os.environ.get("RESEND_API_KEY") and os.environ.get("NOTIFY_EMAIL") else (
        "<div class=card style='border-color:var(--bad)'><b>Emails are switched off.</b> Add RESEND_API_KEY and NOTIFY_EMAIL in Render, "
        "under your service's Environment page, to get approval emails.</div>")
    fake_note = "<div class=card><b>Test mode is on</b> (MODULO_FAKE). Courses use the sample forklift answers and cost nothing.</div>" if os.environ.get("MODULO_FAKE") else ""
    body = f"""<h1>Modulo courses</h1><p class=muted>Every brief and where it is. Bookmark this page.</p>
<p><a class=btn href="/team?key={k}">Talk to the team</a> <span class=muted>Chat one-on-one with Iris, Theo or Quinn about a course.</span></p>
<p>{sample_button(k)} <span class=muted>A finished demo course to explore, free.</span></p>{email_note}{fake_note}
<div class=card><table><tr><th>Course</th><th>Status</th><th>Received (UTC)</th><th>Cost</th><th></th></tr>{rows}</table></div>
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
"""

TEAM_CSS = """
.tabs{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}.tabs button{background:transparent;color:var(--accent)}.tabs button.on{background:var(--accent);color:#fff}
#log{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;height:min(60vh,560px);overflow:auto}
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
    if not dashboard_ok(request):
        return not_found()
    job = await asyncio.to_thread(jobs.create_sample)
    return RedirectResponse(f"/team?key={dashboard_key()}&course={job['id']}&agent=theo", 303)


def sample_button(k: str, label: str = "Add the sample forklift course") -> str:
    return (f"<form method=post action='/dashboard/sample?key={k}' style='display:inline'><button class=ghost>{label}</button></form>")


async def team_page(request: Request):
    if not dashboard_ok(request):
        return not_found()
    all_jobs = jobs.all_jobs()
    k = html.escape(dashboard_key())
    if not all_jobs:
        return page("Talk to the team", f"<p class=muted><a href='/dashboard?key={k}'>All courses</a></p><h1>Talk to the team</h1>"
                    "<div class=card><p style='margin-top:0'><b>There's nothing to talk about yet.</b> The agents chat about a specific course, "
                    "and you don't have any courses yet.</p>"
                    "<p>To try it now, add the finished forklift sample course. It's free and takes a second. "
                    "Or send a brief from your website, or paste one on the dashboard; you can chat about it straight away.</p>"
                    f"<p>{sample_button(k)}</p></div>")
    want = request.query_params.get("course", "")
    opts = "".join(
        f"<option value='{j['id']}' data-review='{html.escape(jobs.links(j)['review'])}' data-status='{j['status']}' "
        f"data-sb='{1 if (jobs.job_dir(j['id']) / 'output' / 'storyboard.json').exists() else 0}' "
        f"data-pack='{','.join(p.stem for p in (jobs.job_dir(j['id']) / 'output' / 'pack').glob('*.md') if '_before_' not in p.stem)}'"
        f"{' selected' if j['id'] == want else ''}>"
        f"{html.escape((j.get('title') or 'New brief')[:70])} ({html.escape(j['client_name'][:30])})</option>" for j in all_jobs)
    sel = next((j for j in all_jobs if j["id"] == want), all_jobs[0])
    body = f"""<style>{TEAM_CSS}</style>
<p class=muted><a href='/dashboard?key={k}'>All courses</a></p><h1>Talk to the team</h1>
<p class=muted>A private chat with one team member about one course. When Iris or Theo suggest a change, nothing is saved until you press <b>Save to course</b>. Saving keeps a backup and rebuilds the Word storyboard.</p>
<p class=row><select id=course aria-label="Course">{opts}</select> <a id=open class="btn ghost" href="{html.escape(jobs.links(sel)['review'])}">Open course page</a> {sample_button(k, "Add sample course")}</p>
<div class=tabs role=tablist><button data-a=iris>Iris · design</button><button data-a=theo>Theo · storyboard</button><button data-a=quinn>Quinn · QA</button>
<button data-a=sam>Sam · SME</button><button data-a=vera>Vera · visuals</button><button data-a=mila>Mila · voiceover</button><button data-a=dev>Dev · build</button>
<button data-a=lars>Lars · LMS</button><button data-a=nia>Nia · evaluation</button><button data-a=paige>Paige · project</button></div>
<p class=muted id=who></p><div id=log aria-live=polite></div><p class=muted id=wait hidden></p><p id=err></p>
<p id=why class=muted hidden></p>
<form id=form><textarea id=msg aria-label="Your message"></textarea>
<p class=row><button id=send>Send</button> <button type=button class=ghost id=clear>Clear this chat</button></p></form>
<script>{TEAM_JS}</script>"""
    return page("Talk to the team", body)


async def team_chat(request: Request):
    if not dashboard_ok(request):
        return JSONResponse({"error": "Not allowed."}, 403)
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
    if not dashboard_ok(request):
        return JSONResponse({"error": "Not allowed."}, 403)
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
    if "_before_" in name:
        return "Backup made before you saved a change from the team chat"
    if rel.startswith("proposals/"):
        return "A change an agent suggested in the team chat" + (" (saved)" if name.endswith(".saved") else " (not saved)")
    return ""


def _course_and_file(request: Request) -> tuple[dict | None, Path | None]:
    if not dashboard_ok(request):
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
    job, _ = _course_and_file(request)
    if not job:
        return not_found()
    k = html.escape(dashboard_key())
    root = jobs.job_dir(job["id"])
    files = sorted((p for p in root.rglob("*") if p.is_file() and not p.name.endswith(".tmp")),
                   key=lambda p: (p.parent != root, "proposals" in p.parts, p.parent.name, p.name))
    rows = []
    for p in files:
        rel = _rel(job, p)
        q = f"?key={k}&f={html.escape(rel)}"
        size = p.stat().st_size
        size_txt = f"{size / 1024:.0f} KB" if size >= 1024 else f"{size} bytes"
        view = "" if p.suffix == ".docx" else f"<a href='/course-files/{job['id']}/view{q}'>Open</a> · "
        rows.append(
            f"<tr><td><b>{html.escape(rel)}</b><div class=muted>{html.escape(_help(rel))}</div></td>"
            f"<td class=muted style='white-space:nowrap'>{size_txt}<br>{time.strftime('%d %b %H:%M', time.gmtime(p.stat().st_mtime))} UTC</td>"
            f"<td style='white-space:nowrap'>{view}<a href='/course-files/{job['id']}/download{q}'>Download</a></td></tr>")
    body = (f"<p class=muted><a href='/dashboard?key={k}'>All courses</a> · <a href='{html.escape(jobs.links(job)['review'])}'>Course page</a></p>"
            f"<h1>Course files</h1><p class=muted>{html.escape(job.get('title') or 'New brief')} · {html.escape(job['client_name'])}</p>"
            "<p>Everything saved for this course. <b>Open</b> shows a file here in the browser; <b>Download</b> saves it to your computer.</p>"
            f"<div class=card><table><tr><th>File</th><th>Size and date</th><th></th></tr>{''.join(rows)}</table></div>")
    return page("Course files", body)


async def course_file_view(request: Request):
    job, f = _course_and_file(request)
    if not job or not f:
        return not_found()
    k = html.escape(dashboard_key())
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
        shown = f"<pre class=card style='white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px'>{html.escape(text)}</pre>"
    else:
        shown = f"<pre class=card style='white-space:pre-wrap;overflow-wrap:anywhere'>{html.escape(text)}</pre>"
    body = (f"<p class=muted><a href='/course-files/{job['id']}?key={k}'>Back to course files</a></p>"
            f"<h1 style='overflow-wrap:anywhere'>{html.escape(rel)}</h1><p class=muted>{html.escape(_help(rel))}</p>"
            f"<p><a class='btn ghost' href='/course-files/{job['id']}/download?key={k}&f={html.escape(rel)}'>Download</a></p>{shown}")
    return page(rel, body)


async def course_file_download(request: Request):
    job, f = _course_and_file(request)
    if not job or not f:
        return not_found()
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
        Route("/dashboard/sample", dashboard_sample, methods=["POST"]),
        Route("/team", team_page),
        Route("/course-files/{job_id}", course_files_page),
        Route("/course-files/{job_id}/view", course_file_view),
        Route("/course-files/{job_id}/download", course_file_download),
        Route("/api/team/chat", team_chat, methods=["POST"]),
        Route("/api/team/apply", team_apply, methods=["POST"]),
        Mount("/", StaticFiles(directory=HERE / "public", html=True)),
    ],
    lifespan=lifespan,
)
