"""Dev builds the course: turns the approved storyboard into course.json, one module at a time.

Runs inside the job worker (jobs.py). Each module's result is saved under output/course/build/,
so an interrupted build resumes where it stopped and nothing is paid for twice.
"""

from __future__ import annotations

import json
import re
import os
from pathlib import Path

import pipeline  # pipeline/pipeline.py (jobs.py puts it on the path)
from pipeline import STR, STRS, Ledger, _obj, block, run_agent

from authoring import course as C
from authoring import pictures

BUILD_ESTIMATE = os.environ.get("MODULO_BUILD_ESTIMATE", "about $2 to $4")
BOOL = {"type": "boolean"}

BLOCK_SCHEMA = _obj({
    "type": {"type": "string", "enum": ["text", "heading", "callout", "image", "reveal", "question"]},
    "text": STR,
    "callout_style": {"type": "string", "enum": ["info", "tip", "warning"]},
    "image_description": STR,
    "alt": STR,
    "items": {"type": "array", "items": _obj({"title": STR, "text": STR})},
    "prompt": STR,
    "multiple": BOOL,
    "scored": BOOL,
    "options": {"type": "array", "items": _obj({"text": STR, "correct": BOOL, "feedback": STR})},
    "feedback_correct": STR,
    "feedback_incorrect": STR,
})

MODULE_SCHEMA = _obj({
    "section_title": STR,
    "pages": {"type": "array", "items": _obj({"title": STR, "source_screen_ids": STRS, "blocks": {"type": "array", "items": BLOCK_SCHEMA}})},
    "build_notes": STRS,
})


def modules(storyboard: dict) -> list[tuple[str, list[dict]]]:
    """Storyboard screens grouped by module, in order."""
    groups: list[tuple[str, list[dict]]] = []
    for sc in storyboard.get("screens", []):
        m = sc.get("module") or "Course"
        if not groups or groups[-1][0] != m:
            groups.append((m, []))
        groups[-1][1].append(sc)
    return groups


def _to_section(data: dict) -> dict:
    pages = []
    for p in data.get("pages", []):
        blocks = []
        for b in p.get("blocks", []):
            t = b.get("type")
            if t == "image":
                blocks.append({"type": "image", "alt": b.get("alt", ""), "description": b.get("image_description", "")})
            elif t == "callout":
                blocks.append({"type": "callout", "style": b.get("callout_style", "info"), "text": b.get("text", "")})
            else:
                blocks.append({k: v for k, v in b.items() if k not in ("callout_style", "image_description", "alt")})
        pages.append({"title": p.get("title", ""), "source": p.get("source_screen_ids", []), "blocks": blocks})
    return {"title": data.get("section_title", ""), "pages": pages}


async def make_course(out: Path, brief: str, job_id: str, progress=None) -> dict:
    """Dev builds course/course.json from storyboard.json. Returns a short summary."""
    design, storyboard = pipeline._load(out / "1_design.json"), pipeline._load(out / "storyboard.json")
    if not design or not storyboard:
        raise RuntimeError("Dev needs a finished storyboard to build the course.")
    ledger = Ledger(out / "run_log.json")
    start = ledger.total
    build = C.course_dir(out) / "build"
    build.mkdir(parents=True, exist_ok=True)

    if pipeline.FAKE:  # test mode: the free rule-based draft, no API calls
        if progress:
            progress("Dev is building the course (test mode)")
        course = C.from_storyboard(storyboard, design, job_id)
        course["built_by"] = "Dev (test mode: rule-based draft)"
    else:
        groups = modules(storyboard)
        shared = [block("client_brief", brief) + "\n\n" + block("approved_design", design)]  # cached across modules
        sections, notes = [], []
        for n, (name, screens) in enumerate(groups, 1):
            f = build / f"module_{n:02d}.json"
            if f.exists():
                data = json.loads(f.read_text(encoding="utf-8"))
            else:
                if progress:
                    progress(f"Dev is building module {n} of {len(groups)}: {name}")
                data = await run_agent(
                    "course_builder",
                    block("storyboard_module", {"module": name, "screens": screens})
                    + f"\n\nBuild this module ({n} of {len(groups)}) as one course section. Every screen above must be covered.",
                    MODULE_SCHEMA, ledger, f"Dev builds module {n} of {len(groups)}", shared=shared,
                )
                pipeline.save(f, data)
            sections.append(_to_section(data))
            notes += data.get("build_notes", [])
        pm = re.search(r"(\d{1,3})\s*%", str((design.get("assessment") or {}).get("pass_mark", "")))
        course = {
            "id": job_id,
            "title": storyboard.get("course_title") or design.get("title"),
            "description": design.get("summary", ""),
            "settings": {"pass_mark": int(pm.group(1)) if pm else 80},
            "sections": sections,
            "built_by": "Dev (eLearning Developer)",
        }
        course = C.ensure_results_page(course)
        pipeline.save(build / "build_notes.md", "# Dev's build notes\n\n" + "".join(f"- {x}\n" for x in notes or ["No substitutions needed."]))
    # Vera turns each picture placeholder into a drawn illustration (a few cents; free in test mode)
    course = C.normalize(course)
    drawn = await pictures.draw_with_vera(course, C.course_dir(out) / "assets", ledger, design, progress)
    saved = C.save(out, course)
    report = C.check(saved)
    return {"pages": report["pages"], "scored": report["scored"], "pictures": drawn, "fix": report["fix"], "note": report["note"],
            "cost_usd": round(ledger.total - start, 4), "total_cost_usd": ledger.total}
