"""Course jobs: one folder per client brief, worked on one at a time in the background.

Status flow:
  designing -> awaiting_approval -> (revising -> awaiting_approval) -> storyboarding -> done
  any working step can end in "failed"; "Try again" picks up from the last finished step.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import secrets
import sys
import time
import traceback
from pathlib import Path

import anyio
import markdown

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "pipeline"))
import pipeline  # noqa: E402  (pipeline/pipeline.py)

import emailer  # noqa: E402

log = logging.getLogger("modulo.jobs")
DATA_DIR = Path(os.environ.get("DATA_DIR", HERE / "data"))
JOBS_DIR = DATA_DIR / "jobs"
WORKING = {"designing", "revising", "storyboarding"}
MAX_ROUNDS = int(os.environ.get("MODULO_MAX_ROUNDS", "2"))

_queue: asyncio.Queue[str] | None = None


def public_url() -> str:
    # Render sets RENDER_EXTERNAL_URL automatically, e.g. https://modulo.onrender.com
    return (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "http://localhost:8000").rstrip("/")


# ---------------------------------------------------------------- storage

def job_dir(job_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9-]{6,80}", job_id):
        raise KeyError(job_id)
    return JOBS_DIR / job_id


def load(job_id: str) -> dict:
    f = job_dir(job_id) / "job.json"
    if not f.exists():
        raise KeyError(job_id)
    return json.loads(f.read_text(encoding="utf-8"))


def save(job: dict) -> None:
    job["updated"] = time.time()
    d = job_dir(job["id"])
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "job.json.tmp"
    tmp.write_text(json.dumps(job, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(d / "job.json")


def all_jobs() -> list[dict]:
    if not JOBS_DIR.exists():
        return []
    jobs = []
    for f in JOBS_DIR.glob("*/job.json"):
        try:
            jobs.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(jobs, key=lambda j: j["created"], reverse=True)


def briefs_today() -> int:
    day_ago = time.time() - 86400
    return sum(1 for j in all_jobs() if j["created"] > day_ago)


def create(name: str, email: str, transcript: str, source: str = "website") -> dict:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "client"
    job = {
        "id": f"{time.strftime('%Y%m%d-%H%M')}-{slug}-{secrets.token_hex(2)}",
        "token": secrets.token_urlsafe(24),
        "client_name": name,
        "client_email": email,
        "source": source,
        "status": "designing",
        "step": "Waiting to start",
        "created": time.time(),
        "history": [],
    }
    save(job)
    out = job_dir(job["id"]) / "output"
    out.mkdir(parents=True, exist_ok=True)
    brief = f"Client: {name} <{email}>\nSource: {source}\n\n{transcript.strip()}\n"
    (job_dir(job["id"]) / "brief.txt").write_text(brief, encoding="utf-8")
    note(job, "Brief received")
    enqueue(job["id"])
    return job


def note(job: dict, text: str) -> None:
    job["history"].append({"at": time.time(), "text": text})
    save(job)


def links(job: dict) -> dict:
    base = f"{public_url()}/review/{job['id']}?t={job['token']}"
    return {"review": base, "approve": base + "#approve", "changes": base + "#changes"}


def file_url(job: dict, name: str) -> str:
    return f"{public_url()}/files/{job['id']}/{name}?t={job['token']}"


# ---------------------------------------------------------------- actions from the review page

def approve(job: dict) -> None:
    if job["status"] != "awaiting_approval":
        return
    (job_dir(job["id"]) / "output" / "1_design_APPROVED.txt").write_text("Design approved for storyboarding.\n")
    job["status"], job["step"] = "storyboarding", "Waiting to start"
    note(job, "Design approved")
    enqueue(job["id"])


def request_changes(job: dict, feedback: str) -> None:
    if job["status"] != "awaiting_approval" or not feedback.strip():
        return
    job["status"], job["step"], job["feedback"] = "revising", "Waiting to start", feedback.strip()[:5000]
    note(job, "Changes requested: " + feedback.strip()[:300])
    enqueue(job["id"])


def retry(job: dict) -> None:
    if job["status"] != "failed":
        return
    job["status"] = job.get("failed_status", "designing")
    job["step"] = "Waiting to start"
    note(job, "Trying again")
    enqueue(job["id"])


# ---------------------------------------------------------------- background worker

def enqueue(job_id: str) -> None:
    if _queue is not None:
        _queue.put_nowait(job_id)


async def worker() -> None:
    """Runs forever inside the web server, one course at a time."""
    global _queue
    _queue = asyncio.Queue()
    # Pick up anything that was interrupted by a restart or redeploy.
    for job in sorted(all_jobs(), key=lambda j: j["created"]):
        if job["status"] in WORKING:
            note(job, "Resuming after a restart")
            _queue.put_nowait(job["id"])
    while True:
        job_id = await _queue.get()
        try:
            await run(job_id)
        except Exception:  # never let one bad job stop the worker
            log.exception("Worker crashed on %s", job_id)


async def run(job_id: str) -> None:
    job = load(job_id)
    status = job["status"]
    if status not in WORKING:
        return
    d = job_dir(job_id)
    out = d / "output"
    brief = (d / "brief.txt").read_text(encoding="utf-8")
    try:
        if status in {"designing", "revising"}:
            job["step"] = "Iris is writing the course design" if status == "designing" else "Iris is revising the design"
            save(job)
            design = await pipeline.make_design(out, brief, feedback=job.get("feedback") if status == "revising" else None)
            job = load(job_id)
            job.pop("feedback", None)
            job["title"] = design["title"]
            job["status"], job["step"] = "awaiting_approval", "Waiting for your approval"
            job["design_version"] = job.get("design_version", 0) + 1
            job["cost_usd"] = pipeline.Ledger(out / "run_log.json").total
            note(job, f"Design ready (version {job['design_version']})")
            await anyio.to_thread.run_sync(email_design, job)
        elif status == "storyboarding":
            job["step"] = "Theo is storyboarding and Quinn is checking (about an hour)"
            save(job)
            summary = await pipeline.make_storyboard(out, brief, MAX_ROUNDS)
            job = load(job_id)
            job["summary"] = summary
            job["cost_usd"] = summary["cost_usd"]
            job["status"], job["step"] = "done", "Storyboard delivered"
            note(job, "Storyboard finished")
            await anyio.to_thread.run_sync(email_done, job)
    except Exception as e:
        log.error("Job %s failed:\n%s", job_id, traceback.format_exc())
        job = load(job_id)
        job["failed_status"] = status
        job["status"], job["step"] = "failed", "Stopped with a problem"
        job["error"] = str(e)[:1000]
        note(job, "Problem: " + str(e)[:300])
        await anyio.to_thread.run_sync(email_failed, job)


# ---------------------------------------------------------------- emails

BTN = ("display:inline-block;padding:12px 22px;border-radius:999px;text-decoration:none;"
       "font:600 15px/1 Arial,sans-serif;margin:4px 8px 4px 0;")


def _wrap(body: str) -> str:
    return ("<div style=\"font:15px/1.55 Arial,Helvetica,sans-serif;color:#1c1f2e;max-width:720px\">"
            + body + "<p style=\"color:#777;font-size:13px;margin-top:28px\">Sent by your Modulo course production service.</p></div>")


def design_html(job: dict) -> str:
    md = (job_dir(job["id"]) / "output" / "1_design.md").read_text(encoding="utf-8")
    return markdown.markdown(md)


def email_design(job: dict) -> None:
    lk = links(job)
    again = " (revised)" if job.get("design_version", 1) > 1 else ""
    body = (
        f"<p><b>{html.escape(job['client_name'])}</b> ({html.escape(job['client_email'])}) sent a course brief. "
        f"Iris has written the course design{again}. Please read it below, then choose:</p>"
        f"<p><a href=\"{lk['approve']}\" style=\"{BTN}background:#2f3e9e;color:#fff\">Approve</a>"
        f"<a href=\"{lk['changes']}\" style=\"{BTN}background:#eef0fb;color:#2f3e9e\">Request changes</a></p>"
        "<p style=\"color:#555\">Approving starts the full storyboard. It takes about an hour and costs roughly $7 in Claude usage. "
        "Nothing is spent on it until you approve.</p><hr>"
        + design_html(job)
    )
    emailer.send(f"Please review: course design for {job.get('title', job['client_name'])}{again}", _wrap(body))


def email_done(job: dict) -> None:
    out = job_dir(job["id"]) / "output"
    s = job.get("summary", {})
    f = s.get("open_findings", {})
    qa = ("Quinn's final check passed." if s.get("passed") else
          f"Quinn's final check still lists {f.get('critical', 0)} critical, {f.get('major', 0)} major and "
          f"{f.get('minor', 0)} minor points. They are in the QA report for you or your SME to decide on.")
    body = (
        f"<p>The storyboard for <b>{html.escape(s.get('title', ''))}</b> is ready "
        f"(client: {html.escape(job['client_name'])}).</p>"
        f"<p>{s.get('screens', '?')} screens, {s.get('qa_rounds', '?')} rounds of QA. {qa}</p>"
        f"<p>The Word storyboard and the QA report are attached. Total Claude cost for this course: ${job.get('cost_usd', 0):.2f}.</p>"
        f"<p><a href=\"{links(job)['review']}\" style=\"{BTN}background:#2f3e9e;color:#fff\">Open the course page</a></p>"
        "<p style=\"color:#555\">This is a draft for human review. Please have your SME check the content before it goes to the client.</p>"
    )
    emailer.send(f"Storyboard ready: {s.get('title', job['client_name'])}", _wrap(body),
                 [out / "storyboard.docx", out / "qa_report.md"])


def email_failed(job: dict) -> None:
    body = (
        f"<p>The work on the course for <b>{html.escape(job['client_name'])}</b> stopped with a problem:</p>"
        f"<pre style=\"white-space:pre-wrap;background:#f5f5f7;padding:12px;border-radius:8px\">{html.escape(job.get('error', ''))}</pre>"
        "<p>Common causes: the Claude account ran out of credit or hit its spending limit, or the API key is wrong. "
        "Fix that, then press Try again. Finished steps are kept, so nothing is paid for twice.</p>"
        f"<p><a href=\"{links(job)['review']}\" style=\"{BTN}background:#2f3e9e;color:#fff\">Open the course page</a></p>"
    )
    emailer.send(f"Problem with the course for {job['client_name']}", _wrap(body))
