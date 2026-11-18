"""Course jobs: one folder per client brief, worked on one at a time in the background.

Status flow:
  designing -> awaiting_approval -> (revising -> awaiting_approval) -> storyboarding -> done
  done -> packing -> done      (optional production pack, started by the owner)
  done -> building -> done     (optional: Dev builds the playable course, started by the owner)
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

import db  # noqa: E402
import emailer  # noqa: E402

log = logging.getLogger("modulo.jobs")
DATA_DIR = Path(os.environ.get("DATA_DIR", HERE / "data"))
JOBS_DIR = DATA_DIR / "jobs"
WORKING = {"designing", "revising", "storyboarding", "packing", "building"}
PACK_ESTIMATE = os.environ.get("MODULO_PACK_ESTIMATE", "about $4 to $6")
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


def _mirror(job: dict) -> None:
    """Keep job.json in the course folder in step with the database (a self-describing folder, and a second copy)."""
    try:
        d = job_dir(job["id"])
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "job.json.tmp"
        tmp.write_text(json.dumps(job, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(d / "job.json")
    except OSError as e:
        log.warning("Could not write job.json for %s: %s", job.get("id"), e)


def load(job_id: str) -> dict:
    job_dir(job_id)  # checks the id
    row = db.one("SELECT data FROM jobs WHERE id = ?", (job_id,))
    if not row:
        raise KeyError(job_id)
    return json.loads(row["data"])


def save(job: dict) -> None:
    job["updated"] = time.time()
    job_dir(job["id"])
    data = json.dumps(job, ensure_ascii=False)
    db.execute(
        "INSERT INTO jobs (id, status, created, updated, data) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (id) DO UPDATE SET status = excluded.status, updated = excluded.updated, data = excluded.data",
        (job["id"], job["status"], job["created"], job["updated"], data))
    _mirror(job)


def all_jobs() -> list[dict]:
    return [json.loads(r["data"]) for r in db.query("SELECT data FROM jobs ORDER BY created DESC")]


def import_from_disk() -> int:
    """Brings courses that only exist as folders on the disk (from before the database) into the database.
    Safe to run at every start: courses already in the database are left alone, and no file is changed."""
    if not JOBS_DIR.exists():
        return 0
    known = {r["id"] for r in db.query("SELECT id FROM jobs")}
    added = 0
    for f in sorted(JOBS_DIR.glob("*/job.json")):
        try:
            job = json.loads(f.read_text(encoding="utf-8"))
            if job.get("id") != f.parent.name:
                log.warning("Skipping %s: its id does not match its folder", f)
                continue
            job_dir(job["id"])
            if job["id"] in known:
                continue
            job.setdefault("history", [])
            job.setdefault("created", f.stat().st_mtime)
            job.setdefault("updated", job["created"])
            job.setdefault("status", "done")
            db.execute("INSERT INTO jobs (id, status, created, updated, data) VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                       (job["id"], job["status"], job["created"], job["updated"], json.dumps(job, ensure_ascii=False)))
            added += 1
        except (OSError, ValueError, KeyError) as e:
            log.warning("Could not bring %s into the database: %s", f, e)
    if added:
        log.info("Brought %d existing courses from the disk into the database", added)
    return added


def briefs_today() -> int:
    day_ago = time.time() - 86400
    return db.one("SELECT COUNT(*) AS n FROM jobs WHERE created > ?", (day_ago,))["n"]


def create(name: str, email: str, transcript: str, source: str = "website", who: str | None = None) -> dict:
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
    note(job, "Brief received", who)
    enqueue(job["id"])
    return job


def create_sample() -> dict:
    """A finished demo course from the forklift sample, made instantly and for free."""
    from render import render_design_md, render_docx, render_qa_md, render_storyboard_md
    samples = HERE / "pipeline" / "samples"
    design = json.loads((samples / "fake" / "1_design.json").read_text(encoding="utf-8"))
    storyboard = json.loads((samples / "fake" / "2_storyboard_draft3.json").read_text(encoding="utf-8"))
    qa = json.loads((samples / "fake" / "3_qa_round3.json").read_text(encoding="utf-8"))
    job = {
        "id": f"{time.strftime('%Y%m%d-%H%M')}-sample-forklift-{secrets.token_hex(2)}",
        "token": secrets.token_urlsafe(24),
        "client_name": "Sample client (forklift demo)",
        "client_email": "sample@example.com",
        "source": "sample",
        "status": "done",
        "step": "Sample course, ready to explore",
        "title": design["title"],
        "created": time.time(),
        "cost_usd": 0.0,
        "history": [],
    }
    out = job_dir(job["id"]) / "output"
    out.mkdir(parents=True, exist_ok=True)
    (job_dir(job["id"]) / "brief.txt").write_text((samples / "forklift_brief.txt").read_text(encoding="utf-8"), encoding="utf-8")
    pipeline.save(out / "1_design.json", design)
    pipeline.save(out / "1_design.md", render_design_md(design))
    pipeline.save(out / "1_design_APPROVED.txt", "Sample course.\n")
    pipeline.save(out / "storyboard.json", storyboard)
    pipeline.save(out / "3_qa_round1.json", qa)
    pipeline.save(out / "storyboard.md", render_storyboard_md(design, storyboard))
    pipeline.save(out / "qa_report.md", render_qa_md([qa]))
    render_docx(design, storyboard, [qa], out / "storyboard.docx")
    counts = {k: sum(f["severity"] == k for f in qa["findings"]) for k in ("critical", "major", "minor")}
    job["summary"] = {"title": design["title"], "screens": len(storyboard["screens"]), "qa_rounds": 1,
                      "passed": bool(qa["pass"]), "open_findings": counts, "cost_usd": 0.0}
    save(job)
    note(job, "Sample course added")
    return job


def note(job: dict, text: str, who: str | None = None) -> None:
    entry = {"at": time.time(), "text": text}
    if who:
        entry["by"] = who
    job["history"].append(entry)
    save(job)


def page_url(job: dict) -> str:
    """The course page for signed-in team members (no private link code in it)."""
    return f"/review/{job['id']}"


def links(job: dict) -> dict:
    base = f"{public_url()}/review/{job['id']}?t={job['token']}"
    return {"review": base, "approve": base + "#approve", "changes": base + "#changes"}


def file_url(job: dict, name: str) -> str:
    return f"{public_url()}/files/{job['id']}/{name}?t={job['token']}"


# ---------------------------------------------------------------- actions from the review page

def approve(job: dict, who: str | None = None) -> None:
    if job["status"] != "awaiting_approval":
        return
    (job_dir(job["id"]) / "output" / "1_design_APPROVED.txt").write_text("Design approved for storyboarding.\n")
    job["status"], job["step"] = "storyboarding", "Waiting to start"
    note(job, "Design approved", who)
    enqueue(job["id"])


def request_changes(job: dict, feedback: str, who: str | None = None) -> None:
    if job["status"] != "awaiting_approval" or not feedback.strip():
        return
    job["status"], job["step"], job["feedback"] = "revising", "Waiting to start", feedback.strip()[:5000]
    note(job, "Changes requested: " + feedback.strip()[:300], who)
    enqueue(job["id"])


def start_pack(job: dict, who: str | None = None) -> None:
    if job["status"] != "done" or job.get("pack_done"):
        return
    job["status"], job["step"] = "packing", "Waiting to start"
    note(job, "Production pack approved", who)
    enqueue(job["id"])


def start_build(job: dict, who: str | None = None) -> None:
    """Owner pressed "Have Dev build the course" (costs money, so only from that button)."""
    if job["status"] != "done":
        return
    job["status"], job["step"] = "building", "Waiting to start"
    note(job, "Course build approved", who)
    enqueue(job["id"])


def quick_build(job: dict, who: str | None = None) -> dict:
    """The free rule-based draft, made instantly. Returns the check report."""
    from authoring import course as C
    out = job_dir(job["id"]) / "output"
    design, storyboard = pipeline._load(out / "1_design.json"), pipeline._load(out / "storyboard.json")
    if job["status"] in WORKING or not storyboard:
        raise RuntimeError("There is no finished storyboard to build from yet.")
    saved = C.save(out, C.from_storyboard(storyboard, design, job["id"]))
    job["course_built"] = True
    note(job, "Quick course draft made (free)", who)
    return C.check(saved)


def retry(job: dict, who: str | None = None) -> None:
    if job["status"] != "failed":
        return
    job["status"] = job.get("failed_status", "designing")
    job["step"] = "Waiting to start"
    note(job, "Trying again", who)
    enqueue(job["id"])


# ---------------------------------------------------------------- background work queue
#
# The queue lives in the database: a course is waiting for work when its status is one of WORKING
# and nobody has claimed it. The worker claims up to PARALLEL courses at a time, so several clients'
# courses move forward together, and releases each claim when that step ends. After a restart or a
# redeploy, every claim is released and the work picks up again from the last finished step.
# A course that keeps being interrupted (for example because the server runs out of memory) is
# stopped after MAX_ATTEMPTS tries and you get an email, so it can't loop and spend money forever.

PARALLEL = max(1, int(os.environ.get("MODULO_PARALLEL_JOBS", "1")))
MAX_ATTEMPTS = int(os.environ.get("MODULO_MAX_ATTEMPTS", "3"))
STUCK_HOURS = float(os.environ.get("MODULO_STUCK_HOURS", "3"))
WORKER_ID = f"{os.getpid()}-{secrets.token_hex(3)}"
_wake: asyncio.Event | None = None
_running: dict[str, asyncio.Task] = {}


def enqueue(job_id: str) -> None:
    """Nudges the worker. The work itself is already recorded by the job's status."""
    if _wake is not None:
        try:
            asyncio.get_running_loop()
            _wake.set()
        except RuntimeError:  # called from a worker thread
            if _loop is not None:
                _loop.call_soon_threadsafe(_wake.set)


_loop: asyncio.AbstractEventLoop | None = None


def queue_state() -> dict:
    rows = db.query("SELECT id, status, claimed_by, claimed_at, attempts FROM jobs WHERE status IN ({})".format(
        ", ".join("?" * len(WORKING))), sorted(WORKING))
    return {"running": [r for r in rows if r["claimed_by"]], "waiting": [r for r in rows if not r["claimed_by"]],
            "parallel": PARALLEL}


def release_all_claims() -> list[str]:
    """At start-up: anything claimed by the previous run of the service was interrupted."""
    rows = db.query("SELECT id FROM jobs WHERE claimed_by IS NOT NULL")
    db.execute("UPDATE jobs SET claimed_by = NULL, claimed_at = NULL WHERE claimed_by IS NOT NULL")
    return [r["id"] for r in rows]


def claim_next() -> str | None:
    """Takes the oldest waiting course, or returns None. Stops a course that has been tried too often."""
    while True:
        with db.tx() as c:
            row = c.one("SELECT id, attempts FROM jobs WHERE claimed_by IS NULL AND status IN ({}) ORDER BY updated LIMIT 1".format(
                ", ".join("?" * len(WORKING))), sorted(WORKING))
            if not row:
                return None
            if row["attempts"] >= MAX_ATTEMPTS:
                c.execute("UPDATE jobs SET attempts = 0 WHERE id = ?", (row["id"],))
                too_many = row["id"]
            else:
                n = c.execute("UPDATE jobs SET claimed_by = ?, claimed_at = ?, attempts = attempts + 1 "
                              "WHERE id = ? AND claimed_by IS NULL", (WORKER_ID, time.time(), row["id"]))
                if n:
                    return row["id"]
                continue
        _give_up(too_many)


def _give_up(job_id: str) -> None:
    job = load(job_id)
    status = job["status"]
    if status not in WORKING:
        return
    job["failed_status"] = status
    job["status"], job["step"] = "failed", "Stopped with a problem"
    job["error"] = (f"The service was interrupted {MAX_ATTEMPTS} times while working on this step, so it was stopped to "
                    "avoid paying for the same work again and again. This usually means the server ran out of memory "
                    "(see Render's Events page for your service). Press Try again to restart from the last finished step.")
    note(job, "Stopped after repeated interruptions")
    try:
        email_failed(job)
    except Exception:  # noqa: BLE001
        log.exception("Could not send the failure email for %s", job_id)


def release(job_id: str) -> None:
    job = load(job_id)
    if job["status"] in WORKING:
        db.execute("UPDATE jobs SET claimed_by = NULL, claimed_at = NULL WHERE id = ?", (job_id,))
    else:
        db.execute("UPDATE jobs SET claimed_by = NULL, claimed_at = NULL, attempts = 0 WHERE id = ?", (job_id,))


async def _run_claimed(job_id: str) -> None:
    try:
        await run(job_id)
    except asyncio.CancelledError:
        # The service is shutting down normally (a redeploy or restart): that doesn't count as a failed try.
        try:
            db.execute("UPDATE jobs SET claimed_by = NULL, claimed_at = NULL, "
                       "attempts = CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END WHERE id = ?", (job_id,))
        except Exception:  # noqa: BLE001
            pass
        raise
    except Exception:  # never let one bad course stop the others
        log.exception("Worker crashed on %s", job_id)
        try:
            job = load(job_id)
            if job["status"] in WORKING:
                job["failed_status"] = job["status"]
                job["status"], job["step"] = "failed", "Stopped with a problem"
                job["error"] = "An unexpected error stopped the work. Press Try again; if it happens again, tell your developer."
                note(job, "Problem: unexpected error")
                await anyio.to_thread.run_sync(email_failed, job)
        except Exception:  # noqa: BLE001
            log.exception("Could not record the failure of %s", job_id)
    try:
        await anyio.to_thread.run_sync(release, job_id)
    except Exception:  # noqa: BLE001
        log.exception("Could not release %s", job_id)
    if _wake is not None:
        _wake.set()  # a slot is free: start the next waiting course straight away


def check_stuck() -> list[str]:
    """Emails once about any course whose current step has run far longer than it should."""
    flagged = []
    limit = time.time() - STUCK_HOURS * 3600
    for r in db.query("SELECT id FROM jobs WHERE claimed_by IS NOT NULL AND claimed_at < ?", (limit,)):
        job = load(r["id"])
        if job.get("stuck_alerted_at", 0) > limit:
            continue
        job["stuck_alerted_at"] = time.time()
        save(job)
        flagged.append(job["id"])
        emailer.send(f"Taking unusually long: {job.get('title') or job['client_name']}", _wrap(
            f"<p>The step <b>{html.escape(job.get('step', ''))}</b> for the course for <b>{html.escape(job['client_name'])}</b> "
            f"has been running for more than {STUCK_HOURS:g} hours. Normally the longest step takes about an hour.</p>"
            "<p>It may still finish. If it hasn't moved by tomorrow, redeploy the service in Render (Manual Deploy, "
            "Restart service): the work restarts from the last finished step.</p>"
            f"<p><a href=\"{public_url()}/review/{job['id']}\" style=\"{BTN}background:#2f3e9e;color:#fff\">Open the course page</a></p>"))
    return flagged


_last_alert: dict[str, float] = {}


def alert_once_an_hour(what: str, subject: str, body: str) -> None:
    if time.time() - _last_alert.get(what, 0) < 3600:
        return
    _last_alert[what] = time.time()
    try:
        emailer.send(subject, _wrap(body))
    except Exception:  # noqa: BLE001
        log.exception("Could not send alert %s", what)


async def worker() -> None:
    """Runs forever inside the web server, PARALLEL courses at a time."""
    global _wake, _loop
    _wake, _loop = asyncio.Event(), asyncio.get_running_loop()
    for job_id in await anyio.to_thread.run_sync(release_all_claims):
        try:
            note(load(job_id), "Resuming after a restart")
        except KeyError:
            pass
    log.info("Worker %s started: up to %d courses at a time", WORKER_ID, PARALLEL)
    last_watch = 0.0
    while True:
        try:
            for jid in [j for j, t in _running.items() if t.done()]:
                _running.pop(jid)
            while len(_running) < PARALLEL:
                job_id = await anyio.to_thread.run_sync(claim_next)
                if not job_id:
                    break
                _running[job_id] = asyncio.create_task(_run_claimed(job_id), name=f"job-{job_id}")
            if time.time() - last_watch > 600:
                last_watch = time.time()
                await anyio.to_thread.run_sync(check_stuck)
        except Exception as e:  # e.g. the database was briefly unreachable
            log.exception("Worker loop problem")
            await anyio.to_thread.run_sync(alert_once_an_hour, "worker", "Modulo: the work queue has a problem",
                                           "<p>The background worker could not reach the database or hit an error:</p>"
                                           f"<pre style=\"white-space:pre-wrap\">{html.escape(str(e)[:800])}</pre>"
                                           "<p>It keeps retrying by itself. If you get this email again in an hour, "
                                           "check your service and database in Render.</p>")
        _wake.clear()
        try:
            await asyncio.wait_for(_wake.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass


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
        elif status == "packing":
            def progress(text: str) -> None:
                j = load(job_id)
                j["step"] = text
                save(j)
            progress("The team is starting the production pack")
            summary = await pipeline.make_pack(out, brief, progress)
            job = load(job_id)
            job["pack_done"] = True
            job["pack_cost_usd"] = summary["cost_usd"]
            job["cost_usd"] = summary["total_cost_usd"]
            job["status"], job["step"] = "done", "Storyboard and production pack delivered"
            note(job, f"Production pack finished ({summary['documents']} documents)")
            await anyio.to_thread.run_sync(email_pack_done, job)
        elif status == "building":
            from authoring.build import make_course

            def progress(text: str) -> None:
                j = load(job_id)
                j["step"] = text
                save(j)
            progress("Dev is starting the course build")
            summary = await make_course(out, brief, job_id, progress)
            job = load(job_id)
            job["course_built"] = True
            job["build_summary"] = summary
            job["build_cost_usd"] = round(job.get("build_cost_usd", 0) + summary["cost_usd"], 4)
            job["cost_usd"] = summary["total_cost_usd"]
            job["status"], job["step"] = "done", "Course built"
            note(job, f"Dev built the course ({summary['pages']} pages)")
            await anyio.to_thread.run_sync(email_course_built, job)
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
        "<hr><p><b>Optional next step: the production pack.</b> The rest of the team can now write their documents from this storyboard: "
        "Sam's SME review pack, Vera's visual style guide and image prompts, Mila's voiceover brief, Dev's build specification, "
        f"Lars's LMS launch plan, Nia's evaluation plan and Paige's project plan. It takes about 20 to 30 minutes and costs {PACK_ESTIMATE}. "
        "Nothing runs unless you ask for it.</p>"
        f"<p><a href=\"{links(job)['review']}#pack\" style=\"{BTN}background:#eef0fb;color:#2f3e9e\">Review and start the production pack</a></p>"
    )
    emailer.send(f"Storyboard ready: {s.get('title', job['client_name'])}", _wrap(body),
                 [out / "storyboard.docx", out / "qa_report.md"])


def email_pack_done(job: dict) -> None:
    out = job_dir(job["id"]) / "output"
    items = "".join(
        f"<li><b>{html.escape(title)}</b> ({name}, {role})</li>"
        for aid, name, role, _, title in pipeline.PACK_AGENTS if (out / "pack" / f"{aid}.md").exists())
    body = (
        f"<p>The production pack for <b>{html.escape(job.get('title', ''))}</b> is ready. It's attached as one Word file with these documents:</p>"
        f"<ul>{items}</ul>"
        f"<p>Cost of the pack: ${job.get('pack_cost_usd', 0):.2f}. Total Claude cost for this course: ${job.get('cost_usd', 0):.2f}.</p>"
        f"<p><a href=\"{links(job)['review']}\" style=\"{BTN}background:#2f3e9e;color:#fff\">Open the course page</a></p>"
        "<p style=\"color:#555\">These are drafts for your review. You can ask any team member to change their document in Talk to the team.</p>"
    )
    emailer.send(f"Production pack ready: {job.get('title', job['client_name'])}", _wrap(body), [out / "production_pack.docx"])


def course_ready(job: dict) -> bool:
    return (job_dir(job["id"]) / "output" / "course" / "course.json").exists() and job["status"] != "building"


def builder_url(job: dict) -> str:
    return f"{public_url()}/builder/{job['id']}"  # asks for a login


def play_token(job: dict) -> str:
    """The code in shareable "Try the course" links. Separate from the course's private email link code,
    so sharing the course with a client (or a Reviewer seeing the link) can't be used to approve or spend.
    Links shared before this change used the private code and still work."""
    if not job.get("play_token"):
        job["play_token"] = secrets.token_urlsafe(18)
        save(job)
    return job["play_token"]


def preview_url(job: dict) -> str:
    return f"{public_url()}/play/{job['id']}/{play_token(job)}/index.html"


def email_course_built(job: dict) -> None:
    s = job.get("build_summary", {})
    fixes = "".join(f"<li>{html.escape(x)}</li>" for x in s.get("fix", []))
    notes = "".join(f"<li>{html.escape(x)}</li>" for x in s.get("note", []))
    body = (
        f"<p>Dev has built <b>{html.escape(job.get('title', ''))}</b> as a real course: {s.get('pages', '?')} pages"
        + (f", with {s.get('scored')} scored questions and a results page" if s.get("scored") else "") + ".</p>"
        f"<p><a href=\"{preview_url(job)}\" style=\"{BTN}background:#2f3e9e;color:#fff\">Try the course</a>"
        f"<a href=\"{builder_url(job)}\" style=\"{BTN}background:#eef0fb;color:#2f3e9e\">Edit and download SCORM</a></p>"
        + (f"<p><b>Please fix before export:</b></p><ul>{fixes}</ul>" if fixes else "")
        + (f"<p><b>Before it goes to learners:</b></p><ul>{notes}</ul>" if notes else "")
        + f"<p>Cost of the build: ${s.get('cost_usd', 0):.2f}. Total Claude cost for this course: ${job.get('cost_usd', 0):.2f}.</p>"
        "<p style=\"color:#555\">In the editor you can change any text, question or setting, upload pictures, "
        "and download the course for your LMS as SCORM 1.2 or SCORM 2004. The Try the course link works for anyone you send it to, so you can share it with your client.</p>"
    )
    emailer.send(f"Course built: {job.get('title', job['client_name'])}", _wrap(body))


def email_failed(job: dict) -> None:
    body = (
        f"<p>The work on the course for <b>{html.escape(job['client_name'])}</b> stopped with a problem:</p>"
        f"<pre style=\"white-space:pre-wrap;background:#f5f5f7;padding:12px;border-radius:8px\">{html.escape(job.get('error', ''))}</pre>"
        "<p>Common causes: the Claude account ran out of credit or hit its spending limit, or the API key is wrong. "
        "Fix that, then press Try again. Finished steps are kept, so nothing is paid for twice.</p>"
        f"<p><a href=\"{links(job)['review']}\" style=\"{BTN}background:#2f3e9e;color:#fff\">Open the course page</a></p>"
    )
    emailer.send(f"Problem with the course for {job['client_name']}", _wrap(body))
