"""Modulo course format (course.json): load, clean up, check, and make a first draft from a storyboard.

A course is sections -> pages -> blocks. Block types:
  heading  {text}
  text     {text}                       simple Markdown: paragraphs, - lists, 1. lists, **bold**, *italic*, ### headings
  callout  {style: info|tip|warning, title, text}
  image    {src, alt, caption, description}   no src = a placeholder showing the description
  reveal   {text, items: [{title, text}]}     click-to-reveal panels
  question {prompt, multiple, scored, retry, options: [{text, correct, feedback}], feedback_correct, feedback_incorrect}
  results  {}                           score, pass or fail, and retake (for scored questions)
"""

from __future__ import annotations

import copy
import json
import re
import time
from pathlib import Path

FORMAT = "modulo-course/1"
BLOCK_TYPES = ("heading", "text", "callout", "image", "reveal", "question", "results")
DEFAULT_SETTINGS = {
    "pass_mark": 80,            # percent of scored questions
    "navigation": "free",       # "free" or "sequential"
    "quiz_feedback": True,      # show right/wrong feedback on scored questions
    "max_attempts": 0,          # 0 = unlimited retakes
    "accent": "#2f3e9e",
    "pass_message": "",
    "fail_message": "",
}
TO_CONFIRM = re.compile(r"\[(?:CLIENT TO CONFIRM|TO CONFIRM|TBC|TBD)[^\]]*\]", re.I)


# ---------------------------------------------------------------- files

def course_dir(out: Path) -> Path:
    return out / "course"


def load(out: Path) -> dict | None:
    f = course_dir(out) / "course.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def save(out: Path, course: dict, backup: bool = True) -> dict:
    """Clean up and save. The previous version goes to course/versions/ (last 20 kept)."""
    d = course_dir(out)
    (d / "assets").mkdir(parents=True, exist_ok=True)
    f = d / "course.json"
    if backup and f.exists():
        v = d / "versions"
        v.mkdir(exist_ok=True)
        (v / f"course-{time.strftime('%Y%m%d-%H%M%S')}.json").write_bytes(f.read_bytes())
        for old in sorted(v.glob("course-*.json"))[:-20]:
            old.unlink()
    course = normalize(course)
    course["revision"] = int(course.get("revision") or 0) + 1
    course["saved_at"] = time.time()
    tmp = d / "course.json.tmp"
    tmp.write_text(json.dumps(course, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(f)
    return course


def versions(out: Path) -> list[Path]:
    return sorted((course_dir(out) / "versions").glob("course-*.json"), reverse=True)


# ---------------------------------------------------------------- clean up

def _s(v, limit: int = 20000) -> str:
    return str(v if v is not None else "").strip()[:limit]


def _b(v) -> bool:
    return v is True or str(v).lower() in {"1", "true", "yes", "on"}


def normalize(course: dict) -> dict:
    """Return a tidy copy: known fields only, every id present and unique, settings filled in."""
    c = copy.deepcopy(course or {})
    seen: set[str] = set()

    def uid(prefix: str, given) -> str:
        given = re.sub(r"[^A-Za-z0-9_-]", "", str(given or ""))[:24]
        if given and given not in seen:
            seen.add(given)
            return given
        n = 1
        while f"{prefix}{n}" in seen:
            n += 1
        seen.add(f"{prefix}{n}")
        return f"{prefix}{n}"

    settings = dict(DEFAULT_SETTINGS)
    settings.update({k: v for k, v in (c.get("settings") or {}).items() if k in DEFAULT_SETTINGS})
    try:
        settings["pass_mark"] = max(0, min(100, int(float(settings["pass_mark"]))))
    except (TypeError, ValueError):
        settings["pass_mark"] = 80
    try:
        settings["max_attempts"] = max(0, min(20, int(settings["max_attempts"])))
    except (TypeError, ValueError):
        settings["max_attempts"] = 0
    settings["navigation"] = "sequential" if settings["navigation"] == "sequential" else "free"
    settings["quiz_feedback"] = _b(settings["quiz_feedback"])
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(settings["accent"])):
        settings["accent"] = DEFAULT_SETTINGS["accent"]
    settings["pass_message"] = _s(settings["pass_message"], 500)
    settings["fail_message"] = _s(settings["fail_message"], 500)

    sections = []
    for sec in c.get("sections") or []:
        pages = []
        for p in sec.get("pages") or []:
            blocks = [b for b in (_block(b, uid) for b in p.get("blocks") or []) if b]
            pages.append({"id": uid("p", p.get("id")), "title": _s(p.get("title"), 300) or "Untitled page",
                          "source": [_s(x, 40) for x in (p.get("source") or []) if _s(x)], "blocks": blocks})
        sections.append({"id": uid("s", sec.get("id")), "title": _s(sec.get("title"), 300), "pages": pages})

    return {
        "format": FORMAT,
        "id": re.sub(r"[^a-z0-9-]", "", _s(c.get("id")).lower())[:60] or "course",
        "title": _s(c.get("title"), 300) or "Untitled course",
        "description": _s(c.get("description"), 2000),
        "language": re.sub(r"[^A-Za-z-]", "", _s(c.get("language")))[:12] or "en",
        "revision": c.get("revision") or 0,
        "settings": settings,
        "sections": sections,
        **({"built_by": _s(c.get("built_by"), 100)} if c.get("built_by") else {}),
    }


def _block(b: dict, uid) -> dict | None:
    if not isinstance(b, dict) or b.get("type") not in BLOCK_TYPES:
        return None
    t = b["type"]
    out = {"id": uid("b", b.get("id")), "type": t}
    if t in ("heading", "text"):
        out["text"] = _s(b.get("text"))
    elif t == "callout":
        out["style"] = b.get("style") if b.get("style") in ("info", "tip", "warning") else "info"
        out["title"] = _s(b.get("title"), 200)
        out["text"] = _s(b.get("text"))
    elif t == "image":
        src = _s(b.get("src"), 300)
        out["src"] = src if re.fullmatch(r"assets/[A-Za-z0-9._-]+", src) or src.startswith("https://") else ""
        out["alt"] = _s(b.get("alt"), 500)
        out["caption"] = _s(b.get("caption"), 500)
        out["description"] = _s(b.get("description"), 2000)
    elif t == "reveal":
        out["text"] = _s(b.get("text"))
        out["items"] = [{"title": _s(i.get("title"), 300), "text": _s(i.get("text"))}
                        for i in b.get("items") or [] if isinstance(i, dict) and (_s(i.get("title")) or _s(i.get("text")))]
    elif t == "question":
        out["prompt"] = _s(b.get("prompt"), 3000)
        out["multiple"] = _b(b.get("multiple"))
        out["scored"] = _b(b.get("scored"))
        out["retry"] = not out["scored"] and b.get("retry") is not False
        out["options"] = [{"text": _s(o.get("text"), 1000), "correct": _b(o.get("correct")), "feedback": _s(o.get("feedback"), 3000)}
                          for o in (b.get("options") or [])[:12] if isinstance(o, dict) and _s(o.get("text"))]
        out["feedback_correct"] = _s(b.get("feedback_correct"), 3000)
        out["feedback_incorrect"] = _s(b.get("feedback_incorrect"), 3000)
    return out


def ensure_results_page(course: dict) -> dict:
    """If there are scored questions and no results block, add a results page after the last scored question."""
    last = None
    for si, sec in enumerate(course.get("sections", [])):
        for pi, p in enumerate(sec.get("pages", [])):
            for b in p.get("blocks", []):
                if b.get("type") == "results":
                    return course
                if b.get("type") == "question" and b.get("scored"):
                    last = (si, pi)
    if last:
        si, pi = last
        course["sections"][si]["pages"].insert(pi + 1, {"title": "Your results", "blocks": [
            {"type": "text", "text": "Here's how you did on the assessment."}, {"type": "results"}]})
    return course


# ---------------------------------------------------------------- checks before export

def contrast_with_white(hex_color: str) -> float:
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    lum = 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
    return 1.05 / (lum + 0.05)


def check(course: dict) -> dict:
    """Plain-language problems ('fix') and reminders ('note') before export."""
    fix, note = [], []
    pages = [(sec, p) for sec in course["sections"] for p in sec["pages"]]
    if not pages:
        fix.append("The course has no pages.")
    scored = placeholders = 0
    confirm_pages = []
    has_results = False
    for sec, p in pages:
        where = f"“{p['title']}”"
        if not p["blocks"]:
            fix.append(f"{where} is empty.")
        if TO_CONFIRM.search(json.dumps(p, ensure_ascii=False)):
            confirm_pages.append(p["title"])
        for b in p["blocks"]:
            if b["type"] == "question":
                right = sum(o["correct"] for o in b["options"])
                if len(b["options"]) < 2:
                    fix.append(f"A question on {where} needs at least two answers.")
                if right == 0:
                    fix.append(f"A question on {where} has no correct answer marked.")
                elif right > 1 and not b["multiple"]:
                    fix.append(f"A question on {where} has {right} correct answers but only lets learners pick one. Tick “Select all that apply” or mark one answer.")
                if not b["prompt"]:
                    fix.append(f"A question on {where} has no question text.")
                scored += b["scored"]
            elif b["type"] == "image":
                if not b["src"]:
                    placeholders += 1
                elif not b["alt"]:
                    note.append(f"An image on {where} has no description for screen readers (alt text). Add one unless it is decoration only.")
            elif b["type"] == "results":
                has_results = True
    if scored and not has_results:
        fix.append("The course has scored questions but no results page, so learners can't see their score or retake. Add a “Results” block.")
    if has_results and not scored:
        note.append("There's a results block but no scored questions, so there's no score to report.")
    if placeholders:
        note.append(f"{placeholders} image{'s are' if placeholders != 1 else ' is'} still a placeholder (“Image to come”). Upload pictures in the editor before the course goes to learners.")
    if confirm_pages:
        sample = ", ".join(f"“{t}”" for t in confirm_pages[:5]) + (" and more" if len(confirm_pages) > 5 else "")
        note.append(f"{len(confirm_pages)} page{'s' if len(confirm_pages) != 1 else ''} still contain “CLIENT TO CONFIRM” notes, for example {sample}.")
    ratio = contrast_with_white(course["settings"]["accent"])
    if ratio < 4.5:
        fix.append(f"The main color is too light for white button text (contrast {ratio:.1f} to 1, needs 4.5). Pick a darker color.")
    # SCORM 1.2 keeps at most 4,096 characters of progress data.
    questions = sum(b["type"] == "question" for _, p in pages for b in p["blocks"])
    if 60 + questions * 16 + len(pages) // 2 > 3800:
        note.append("This course is very long for SCORM 1.2 progress saving. Prefer the SCORM 2004 package, or split the course.")
    return {"fix": fix, "note": note, "pages": len(pages), "scored": scored}


# ---------------------------------------------------------------- free first draft from a storyboard

OPTION = re.compile(r"^\s*(?:\[\s?\]\s*)?([A-H])[).]\s+(.+)$")
BUTTONS_ONLY = re.compile(r"^(\s*\[[^\]\n]{1,60}\]\s*)+$")


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) > 1 and s[0] in "'\"‘“" and s[-1] in "'\"’”":
        return s[1:-1].strip()
    return s


def _letters(s: str) -> list[int]:
    return [ord(x) - 65 for x in re.findall(r"\b([A-H])\b", s)]


def _clean_text(text: str) -> str:
    lines = []
    for line in (text or "").splitlines():
        if BUTTONS_ONLY.match(line) and not TO_CONFIRM.search(line):
            continue
        lines.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _question(screen: dict) -> dict | None:
    text = screen.get("on_screen_text") or ""
    lines = text.splitlines()
    opts, first = [], None
    for i, line in enumerate(lines):
        m = OPTION.match(line)
        if m and (not opts or ord(m.group(1)) - 65 == len(opts)):
            first = i if first is None else first
            opts.append(m.group(2).strip())
    if len(opts) < 2:
        return None
    inter, fb = screen.get("interaction") or "", screen.get("feedback") or ""
    stype = (screen.get("screen_type") or "").lower()
    both = f"{inter}\n{fb}"
    right: list[int] = []
    letters = r"([A-H]\b(?:\s*(?:,|and|&)\s*[A-H]\b)*)"
    m = re.search(r"(?i:correct answers?)(?:\s+(?i:is|are))?\s*:\s*" + letters, both)
    m = m or re.search(r"(?i:correct answers?)\s*:[^\n]*?\(" + letters, both)
    if m:
        right = _letters(m.group(1))
    else:
        right = [ord(x) - 65 for x in re.findall(r"(?m)^\s*([A-H])\s*\(correct\)", fb)]
        m = re.search(r"(?m)^\s*Correct\s*\(([A-H ,orand]+)\)", fb)
        if not right and m:
            right = _letters(m.group(1))
    multiple = "[ ]" in text or bool(re.search(r"multiple[- ]response|select (all|every)", f"{text}\n{inter}", re.I)) or len(right) > 1
    scored = "assessment" in stype or bool(re.search(r"\bscored\b", inter, re.I) and not re.search(r"\bunscored|not scored", inter, re.I))
    per_option = {ord(m.group(1)) - 65: _unquote(m.group(2))
                  for m in re.finditer(r"(?m)^\s*([A-H])\s*\((?:in)?correct\)\s*:\s*(.+)$", fb)}
    fc = re.search(r"(?m)^\s*Correct(?:\s*\([^)]*\))?\s*:\s*(.+)$", fb)
    fi = re.search(r"(?m)^\s*Incorrect(?:\s*\([^)]*\))?\s*:\s*(.+)$", fb)
    general = ""
    if not per_option and not fc and not fi and fb.strip().lower().rstrip(".") not in {"", "none", "n/a"}:
        general = _unquote(re.sub(r"^[A-Z][A-Z ]+(\([^)]*\))?\s*:\s*", "", fb.strip()))
    prompt = _clean_text("\n".join(lines[:first]))
    return {
        "type": "question",
        "prompt": prompt or screen.get("title", ""),
        "multiple": multiple,
        "scored": scored,
        "retry": not scored,
        "options": [{"text": o, "correct": i in right, "feedback": per_option.get(i, "")} for i, o in enumerate(opts)],
        "feedback_correct": _unquote(fc.group(1)) if fc else general,
        "feedback_incorrect": _unquote(fi.group(1)) if fi else general,
    }


def _reveal(screen: dict) -> dict | None:
    inter = screen.get("interaction") or ""
    if "LAYER TEXT" not in inter.upper():
        return None
    body = re.split(r"LAYER TEXT\s*:?", inter, flags=re.I, maxsplit=1)[1]
    items = []
    for m in re.finditer(r"(?m)^\s*([^:\n]{1,80}):\s*(['\"‘“].+)$", body):
        items.append({"title": m.group(1).strip(), "text": _unquote(m.group(2))})
    return {"type": "reveal", "text": "", "items": items} if items else None


def from_storyboard(storyboard: dict, design: dict | None = None, job_id: str = "") -> dict:
    """A free, rule-based first draft: one page per storyboard screen. Dev's build is smarter."""
    design = design or {}
    sections: list[dict] = []
    for sc in storyboard.get("screens", []):
        module = sc.get("module") or "Course"
        if not sections or sections[-1]["title"] != module:
            sections.append({"title": module, "pages": []})
        blocks: list[dict] = []
        q = _question(sc)
        r = None if q else _reveal(sc)
        text = _clean_text(sc.get("on_screen_text") or "")
        visuals = (sc.get("visuals") or "").strip()
        if visuals and visuals.lower().rstrip(".") not in {"none", "n/a"}:
            blocks.append({"type": "image", "description": visuals[:600] + ("..." if len(visuals) > 600 else ""), "alt": ""})
        if q:
            blocks.append(q)
        else:
            if text:
                blocks.append({"type": "text", "text": text})
            narration = (sc.get("narration") or "").strip()
            if narration and narration.lower().rstrip(".") not in {"none", "n/a"}:
                blocks.append({"type": "text", "text": narration})
            if r:
                blocks.append(r)
        sections[-1]["pages"].append({"title": sc.get("title") or sc.get("id", ""), "source": [sc.get("id", "")], "blocks": blocks})
    pm = re.search(r"(\d{1,3})\s*%", str((design.get("assessment") or {}).get("pass_mark", "")))
    course = {
        "id": job_id,
        "title": storyboard.get("course_title") or design.get("title") or "Course",
        "description": design.get("summary", ""),
        "settings": {"pass_mark": int(pm.group(1)) if pm else 80},
        "sections": sections,
        "built_by": "Quick draft (rule-based, no AI)",
    }
    return normalize(ensure_results_page(course))
