"""Web pages for the course builder.

  /builder/<id>                          the editor (Designer or Admin)
  /builder/<id>/build                    POST: start Dev's build (mode=dev) or make the free draft (mode=quick)
  /builder/<id>/export/<kind>            download SCORM 1.2, SCORM 2004 or a website zip (any team member)
  /api/builder/<id>/course               GET or POST the course (JSON)
  /api/builder/<id>/upload               POST an image
  /api/builder/<id>/illustrate           POST: Vera draws every picture that is still a placeholder
  /api/builder/<id>/restore              POST: go back to the previous save
  /play/<id>/<code>/index.html           the course, playable in any browser (shareable; see jobs.play_token)
"""

from __future__ import annotations

import asyncio
import hmac
import html
import json
import re
import secrets
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

import jobs
from authoring import course as C
from authoring import export as E

HERE = Path(__file__).resolve().parent
IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
MAX_IMAGE = 8 * 1024 * 1024


def _app():
    import app  # the running Starlette app module (shared helpers); imported late to avoid a cycle
    return app


def _job(request: Request, need: str = "designer") -> dict | None:
    import auth
    if not auth.can(auth.current_user(request), need):
        return None
    try:
        return jobs.load(request.path_params["job_id"])
    except KeyError:
        return None


def _out(job: dict) -> Path:
    return jobs.job_dir(job["id"]) / "output"


def _busy(job: dict) -> bool:
    return job["status"] == "building"


# ---------------------------------------------------------------- pages

async def builder_page(request: Request):
    A = _app()
    if blocked := A.gate(request, "designer"):
        return blocked
    job = _job(request)
    if not job:
        return A.not_found()
    k = A.qkey()
    jid = job["id"]
    out = _out(job)
    head = (f"<p class=crumb><a href='{html.escape(A.dashboard_url())}'>All courses</a> · "
            f"<a href='{html.escape(jobs.page_url(job))}'>Course page</a> · "
            f"<a href='/course-files/{jid}'>Course files</a></p>")
    rebuild = request.query_params.get("rebuild") == "1"
    if C.load(out) and not _busy(job) and not rebuild:
        page = (HERE / "editor" / "editor.html").read_text(encoding="utf-8")
        cfg = {"job": jid, "key": "", "preview": jobs.preview_url(job).replace(jobs.public_url(), ""),
               "review": jobs.page_url(job), "dashboard": A.dashboard_url(),
               "rebuild": f"/builder/{jid}?rebuild=1", "files": f"/course-files/{jid}"}
        page = page.replace("/*CONFIG*/", "window.BUILDER=" + json.dumps(cfg).replace("</", "<\\/") + ";")
        return Response(page, media_type="text/html", headers={"cache-control": "no-store", "referrer-policy": "same-origin"})
    if _busy(job):
        body = (head + f"<h1>{html.escape(job.get('title') or 'Course')}</h1>"
                f"<p>{A.pill(job['status'])} <span class=muted>{html.escape(job.get('step', ''))}</span></p>"
                "<div class=card><p style='margin:0'>Dev is building the course. It usually takes 10 to 20 minutes. "
                "This page refreshes by itself and you'll get an email when it's ready.</p></div>")
        return A.page("Building the course", body, refresh=True, request=request)
    has_sb = (out / "storyboard.json").exists() and job["status"] == "done"
    body = head + f"<h1>Build the course</h1><p class=muted>{html.escape(job.get('title') or '')}</p>"
    if not has_sb:
        body += "<div class=card><p style='margin:0'>The course can be built once the storyboard is finished.</p></div>"
    else:
        body += build_card(job, k, rebuild=bool(C.load(out)))
        if C.load(out):
            body += f"<p><a href='/builder/{jid}'>Back to the editor</a></p>"
    return A.page("Build the course", body, request=request)


def build_card(job: dict, k: str, rebuild: bool = False) -> str:
    from authoring.build import BUILD_ESTIMATE
    jid = job["id"]
    again = " again" if rebuild else ""
    warn = ("<p class=muted>Building again replaces the current course. Your current version is kept as a backup, "
            "and you can go back to it from the editor.</p>") if rebuild else ""
    return f"""<div class=card id=build><h2 style="margin-top:0">Turn the storyboard into a real course</h2>
<p>You get a course that plays in any browser, with pages, pictures (drawn by Vera in the course colors, or your own uploads), knowledge checks,
a scored assessment with results and retake, progress tracking, and downloads for your LMS as SCORM 1.2 or SCORM 2004.</p>
<p><b>Dev builds it</b>: Dev reads the storyboard module by module and writes clear learner-facing pages, turning narration into text
and interactions into working questions and click-to-reveal panels. It takes about 10 to 20 minutes and costs {html.escape(BUILD_ESTIMATE)}
in Claude usage. You'll get an email when it's ready.</p>
<form method=post action="/builder/{jid}/build"><input type=hidden name=mode value=dev><button>Have Dev build the course{again}</button></form>
<p style="margin-top:18px"><b>Quick draft</b>: copies the storyboard into the course as it is (on-screen text, narration and questions), instantly and free.
Good for a first look, but it needs more editing.</p>
<form method=post action="/builder/{jid}/build"><input type=hidden name=mode value=quick><button class=ghost>Make a free quick draft{again}</button></form>
{warn}</div>"""


async def builder_build(request: Request):
    A = _app()
    if blocked := A.gate(request, "designer"):
        return blocked
    job = _job(request)
    if not job:
        return A.not_found()
    form = await request.form()
    mode = str(form.get("mode") or "")
    out = _out(job)
    if mode == "quick" and job["status"] == "done":
        await asyncio.to_thread(jobs.quick_build, job, A.auth.who(request))
    elif mode == "dev" and job["status"] == "done":
        build = C.course_dir(out) / "build"
        for f in build.glob("module_*.json"):  # a fresh build: forget earlier module results
            f.unlink()
        jobs.start_build(job, A.auth.who(request))
    return RedirectResponse(f"/builder/{job['id']}", 303)


async def builder_export(request: Request):
    A = _app()
    if blocked := A.gate(request, "reviewer"):
        return blocked
    job = _job(request, "reviewer")
    kind = request.path_params["kind"]
    if not job or kind not in E.KINDS:
        return A.not_found()
    course = C.load(_out(job))
    if not course:
        return A.not_found()
    data = await asyncio.to_thread(E.build, course, C.course_dir(_out(job)) / "assets", kind)
    name = E.filename(course, kind)
    return Response(data, media_type="application/zip",
                    headers={"content-disposition": f'attachment; filename="{name}"', "cache-control": "no-store"})


# ---------------------------------------------------------------- API used by the editor

async def api_course(request: Request):
    if blocked := _app().gate(request, "designer", api=True):
        return blocked
    job = _job(request)
    if not job:
        return JSONResponse({"error": "This link has expired. Open the editor from your dashboard."}, 404)
    out = _out(job)
    if request.method == "POST":
        if _busy(job):
            return JSONResponse({"error": "Dev is rebuilding this course, so changes can't be saved right now."}, 409)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "That didn't arrive in one piece. Please try again."}, 400)
        current = C.load(out) or {}
        if body.get("revision") is not None and current.get("revision") and body["revision"] != current["revision"]:
            return JSONResponse({"error": "This course was changed somewhere else (another tab, or a new build). Reload the page to get the latest version.",
                                 "conflict": True}, 409)
        body["id"] = job["id"]
        body["revision"] = current.get("revision", 0)
        saved = await asyncio.to_thread(C.save, out, body)
        return JSONResponse({"course": saved, "check": C.check(saved), "versions": len(C.versions(out))})
    course = C.load(out)
    if not course:
        return JSONResponse({"error": "No course yet."}, 404)
    return JSONResponse({"course": course, "check": C.check(course), "versions": len(C.versions(out)),
                         "built_by": course.get("built_by", "")})


async def api_restore(request: Request):
    if blocked := _app().gate(request, "designer", api=True):
        return blocked
    job = _job(request)
    if not job or _busy(job):
        return JSONResponse({"error": "Not available right now."}, 409)
    out = _out(job)
    vs = C.versions(out)
    if not vs:
        return JSONResponse({"error": "There is no earlier version to go back to."}, 404)
    previous = json.loads(vs[0].read_text(encoding="utf-8"))
    vs[0].unlink()  # going back consumes that backup
    current = C.load(out) or {}
    previous["revision"] = current.get("revision", 0)
    saved = C.save(out, previous, backup=False)
    return JSONResponse({"course": saved, "check": C.check(saved), "versions": len(C.versions(out))})


async def api_upload(request: Request):
    if blocked := _app().gate(request, "designer", api=True):
        return blocked
    job = _job(request)
    if not job or _busy(job):
        return JSONResponse({"error": "Not available right now."}, 409)
    form = await request.form(max_part_size=MAX_IMAGE + 1024)
    f = form.get("file")
    if f is None or not hasattr(f, "read"):
        return JSONResponse({"error": "No picture was attached."}, 400)
    ext = IMAGE_TYPES.get((f.content_type or "").lower())
    if not ext:
        return JSONResponse({"error": "Please upload a PNG, JPG, GIF or WebP picture."}, 400)
    data = await f.read()
    if len(data) > MAX_IMAGE:
        return JSONResponse({"error": "That picture is over 8 MB. Please make it smaller and try again."}, 400)
    stem = re.sub(r"[^A-Za-z0-9]+", "-", Path(f.filename or "image").stem).strip("-").lower()[:40] or "image"
    name = f"{stem}-{secrets.token_hex(3)}{ext}"
    assets = C.course_dir(_out(job)) / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / name).write_bytes(data)
    return JSONResponse({"src": f"assets/{name}"})


async def builder_asset(request: Request):
    """Pictures for the editor (signed-in team members)."""
    job = _job(request, "reviewer")
    if not job:
        return Response(status_code=404)
    return _asset(job, request.path_params["name"])


def _asset(job: dict, name: str):
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return Response(status_code=404)
    f = C.course_dir(_out(job)) / "assets" / name
    headers = {"cache-control": "private, max-age=3600", "x-content-type-options": "nosniff"}
    if name.endswith(".svg"):  # drawn pictures: never let an SVG run anything if opened on its own
        headers["content-security-policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    return FileResponse(f, headers=headers) if f.is_file() else Response(status_code=404)


# ---------------------------------------------------------------- the course in a browser

PLAYER_FILES = {"player.js": "application/javascript", "player.css": "text/css"}


async def play(request: Request):
    try:
        job = jobs.load(request.path_params["job_id"])
    except KeyError:
        return Response("Not found", status_code=404)
    given = request.path_params["token"]
    if not (hmac.compare_digest(given, job["token"]) or (job.get("play_token") and hmac.compare_digest(given, job["play_token"]))):
        return Response("Not found", status_code=404)
    return serve_course(job, request.path_params["path"])


def serve_course(job: dict, path: str) -> Response:
    """One file of the playable course. Also used by the client tracking page (tracking.py)."""
    course = C.load(_out(job))
    if not course:
        return Response("This course hasn't been built yet.", status_code=404)
    headers = {"cache-control": "no-store", "referrer-policy": "same-origin", "x-robots-tag": "noindex"}
    if path in ("", "index.html"):
        return Response(E.index_html(course), media_type="text/html", headers=headers)
    if path == "course.js":
        return Response(E.course_js(course, "none"), media_type="application/javascript", headers=headers)
    if path in PLAYER_FILES:
        return Response((E.PLAYER / path).read_bytes(), media_type=PLAYER_FILES[path], headers=headers)
    if path.startswith("assets/"):
        return _asset(job, path[len("assets/"):])
    return Response("Not found", status_code=404)


_drawing: set[str] = set()


async def api_illustrate(request: Request):
    """Vera draws the pictures that are still placeholders (a few cents per course)."""
    if blocked := _app().gate(request, "designer", api=True):
        return blocked
    job = _job(request)
    if not job or _busy(job) or job["id"] in _drawing:
        return JSONResponse({"error": "Not available right now. If Vera is already drawing, wait a minute and reload."}, 409)
    import pipeline
    from authoring import pictures
    out = _out(job)
    course = C.load(out)
    if not course:
        return JSONResponse({"error": "No course yet."}, 404)
    _drawing.add(job["id"])
    try:
        ledger = pipeline.Ledger(out / "run_log.json")
        design = pipeline._load(out / "1_design.json")
        drawn = await pictures.draw_with_vera(course, C.course_dir(out) / "assets", ledger, design)
        saved = await asyncio.to_thread(C.save, out, course) if drawn else course
        if drawn:
            fresh = jobs.load(job["id"])
            fresh["cost_usd"] = ledger.total
            jobs.note(fresh, f"Vera drew {drawn} picture{'s' if drawn != 1 else ''}")
    finally:
        _drawing.discard(job["id"])
    return JSONResponse({"course": saved, "check": C.check(saved), "versions": len(C.versions(out)), "drawn": drawn})


ROUTES = [
    Route("/builder/{job_id}", builder_page),
    Route("/builder/{job_id}/build", builder_build, methods=["POST"]),
    Route("/builder/{job_id}/export/{kind}", builder_export),
    Route("/builder/{job_id}/assets/{name}", builder_asset),
    Route("/api/builder/{job_id}/course", api_course, methods=["GET", "POST"]),
    Route("/api/builder/{job_id}/restore", api_restore, methods=["POST"]),
    Route("/api/builder/{job_id}/upload", api_upload, methods=["POST"]),
    Route("/api/builder/{job_id}/illustrate", api_illustrate, methods=["POST"]),
    Route("/play/{job_id}/{token}/{path:path}", play),
]
