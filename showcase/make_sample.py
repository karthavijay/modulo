"""Builds the public sample course that anyone can try at /sample on the website.

It starts from the forklift sample storyboard (a fictional client), makes the course the same way the
course builder does, then tidies it for the public: it fills or removes every "[CLIENT TO CONFIRM]" note,
drops developer notes and pages that only make sense in Storyline, and draws the pictures.

Run it again after changing the sample storyboard:   python showcase/make_sample.py
It writes showcase/sample/course.json and showcase/sample/assets/*.svg, which are committed to the repo.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "pipeline")]

from authoring import course as C  # noqa: E402
from authoring import pictures as P  # noqa: E402

OUT = Path(__file__).resolve().parent / "sample"
FAKE = ROOT / "pipeline" / "samples" / "fake"

# What each "[CLIENT TO CONFIRM: ...]" note becomes in the public sample ("" removes it).
FILL = {
    "real figures, if approved": "",
    "checklist wording": "",
    "from the reach truck checklist": "",
    "example, for instance a torn seat cover": "a torn seat cover",
    "supervisor or maintenance contact": "your shift supervisor",
    "contact": "your shift supervisor",
    "tag and key steps": "(red tag on the steering wheel, key to your supervisor)",
    "how minor defects are reported, for example noted on the checklist and passed to maintenance":
        "on your checklist",
    "check time is built into shift start.": "",
    "pedestrians have the right of way": "pedestrians have the right of way",
    "restraint or chocks, dock plate in place": "with the restraint or chocks on and the dock plate in place",
    "for example walkways and mirrors": "marked walkways and mirrors",
    "travel height": "(about 15 cm, 6 in, off the floor)",
    "travel position": "mast retracted, load at travel height",
    "mast retracted and load at travel height": "the mast retracted and the load at travel height",
    "plus both safety-critical questions": "",
    "'and both safety-critical questions right'": "",
    "beacon or strobe": "beacon",
    "": "",
}
CONFIRM = re.compile(r"\s*\[(?:CLIENT|DANA) TO CONFIRM(?::\s*([^\]]*))?\]")
SOP_REF = re.compile(r"\s*SOP ref: \[CLIENT TO CONFIRM: section\]\.?")
SITE_SOP = re.compile(r"\.\s*Site SOP \[CLIENT TO CONFIRM: section\]")

TIP_TEXT = ("If it starts to tip:\n\nSit-down counterbalance:\n- Don't jump\n- Stay in the seat, belt on\n"
            "- Grip the wheel, brace your feet\n- Lean away from the fall\n\n"
            "Reach truck: follow the tip-over response in your SOP and your truck's operator's manual.")
TIP_SAY_END = ("On a reach truck, the right response depends on the truck's design, so follow the response in your "
               "SOP and your truck's operator's manual. Your supervisor will go through it with you.")

# The final check keeps one or two questions per learning objective (ten in all), as the design asked.
FINAL_KEEP = {1, 4, 5, 8, 9, 10, 12, 15, 16, 19}


def _fill(m: re.Match) -> str:
    inner = (m.group(1) or "").strip()
    if inner not in FILL:
        raise SystemExit(f"No public wording for: [CLIENT TO CONFIRM: {inner}]. Add it to FILL in {__file__}.")
    value = FILL[inner]
    return (" " + value) if value else ""


def clean(text: str) -> str:
    text = text.replace("Our site controls: [CLIENT TO CONFIRM]", "Our site controls: marked walkways and mirrors.")
    text = SOP_REF.sub("", text)
    text = SITE_SOP.sub(".", text)
    text = CONFIRM.sub(_fill, text)
    text = re.sub(r"[ \t]+([.,:])", r"\1", text)
    text = re.sub(r"\.\.+", ".", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def walk(o):
    if isinstance(o, dict):
        return {k: (v if k in ("id", "description", "src") else walk(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [walk(v) for v in o]
    return clean(o) if isinstance(o, str) else o


def text_block(uid: str, text: str) -> dict:
    return {"id": uid, "type": "text", "text": text}


def question(uid: str, prompt: str, options: list[tuple[str, bool]], right: str, wrong: str) -> dict:
    return {"id": uid, "type": "question", "prompt": prompt, "multiple": True, "scored": False, "retry": True,
            "options": [{"text": t, "correct": ok, "feedback": ""} for t, ok in options],
            "feedback_correct": right, "feedback_incorrect": wrong}


def tidy(course: dict) -> dict:
    s0, s1, s2, s3, s4 = course["sections"]
    for s in course["sections"]:
        s["title"] = re.sub(r"^M\d+:\s*", "", s["title"])

    course["description"] = (
        "A 24-minute, scenario-based refresher for about 220 forklift operators across three distribution centers "
        "(a fictional client). It targets pedestrian conflicts at intersections and dock doors, tip-overs from carrying "
        "raised loads through turns, and pre-shift inspections that get signed without being done. Operators make the "
        "calls themselves: they inspect a truck, choose how to approach a blind intersection or a busy dock door, and "
        "decide how to carry a load through a turn.")

    # Route page: this player has no captions button.
    blk = s0["pages"][2]["blocks"][2]
    blk["text"] = blk["text"].replace(" Captions are on because the floor is loud, and the CC button turns them off.", "")

    # Inspection pages were click-to-inspect screens in Storyline. Here they become "which would you flag?" questions.
    p = s1["pages"][4]
    p["blocks"][1:] = [question(
        p["blocks"][1]["id"], "Truck 07, walk-around and tests. Which of these would you flag as a defect?",
        [("Left fork heel worn, with a hairline crack starting", True),
         ("Lift chains with surface grime", False),
         ("Fluid weeping at the lift cylinder hose fitting, wet patch under the mast", True),
         ("Light scuffing on the tire sidewalls", False),
         ("Seatbelt buckle won't latch", True),
         ("Dust on the LPG cylinder", False),
         ("Reverse alarm silent when you back up", True)],
        "Right: four real defects. The fork crack, the leak, the belt and the alarm each take away a margin you rely on. "
        "Grime, scuffs and dust are cosmetic.",
        "Not quite. The four defects are the fork heel crack, the leak at the hose fitting, the seatbelt buckle and the "
        "silent reverse alarm. Grime, scuffs and dust are cosmetic. Try again.")]
    p = s1["pages"][5]
    p["blocks"][1:] = [question(
        p["blocks"][1]["id"], "Reach truck R12. Which of these would you flag as a defect?",
        [("Scuffed paint on the forks", False),
         ("A kinked, rust-stiffened link near the top of the left lift chain", True),
         ("Reach mechanism and hoses clean and dry", False),
         ("Cracked battery connector housing, copper showing on one cable", True),
         ("Data plate with edge scratches, every value readable", False),
         ("Horn silent when pressed", True),
         ("Truck creeps on the travel control with the presence pedal released", True)],
        "Right: the chain, the battery connector, the horn and the creeping travel control are all defects.",
        "Not quite. The defects are the kinked chain link, the cracked battery connector, the silent horn and the "
        "creeping travel control. Paint scuffs and edge scratches are cosmetic. Try again.")]

    # The stability simulator was a Storyline interaction. Here it becomes click-to-reveal panels.
    p = s3["pages"][2]
    p["title"] = "What Tips a Truck"
    p["blocks"][1:] = [
        text_block(p["blocks"][2]["id"], "Three things move the center of gravity toward the edge of the stability "
                   "triangle. Open each one to see what it does."),
        {"id": p["blocks"][1]["id"], "type": "reveal", "text": "", "items": [
            {"title": "Load height", "text": "The higher the load, the higher the combined center of gravity, and the "
             "less sideways force it takes to push it past the edge. Travel at floor height."},
            {"title": "Speed", "text": "Speed in a turn pushes the center of gravity sideways. Braking hard in the "
             "turn throws it forward at the same time. Get your speed off on the straight."},
            {"title": "Turn", "text": "A tight turn pushes harder than a wide, smooth one. Put all three together, a "
             "raised load, speed held and a tight turn, and the center of gravity is outside the triangle."},
        ]},
    ]

    # Tip-over: use the wording the storyboard gave for when the reach truck response isn't confirmed.
    p = s3["pages"][7]
    p["blocks"][1]["text"] = TIP_TEXT
    say = p["blocks"][2]["text"]
    say = say[:say.index("On a reach truck")] + TIP_SAY_END
    p["blocks"][2]["text"] = say
    del s3["pages"][9]  # reach truck quick check: its answer depends on the client's SOP

    # Final check: ten questions, plain titles, one results page, a closing page.
    pages = s4["pages"]
    intro = pages[0]
    intro["blocks"][1]["text"] = ("Final check:\n- 10 situations from the floor\n- Pass: 8 of 10 (80%)\n"
                                  "- Reasons shown after you submit\n- No timer")
    intro["blocks"][2]["text"] = ("Last part: ten situations, and you make the call. Eight of ten to pass. "
                                  "Reasons come after you submit, and there's no timer.")
    kept = []
    for n, page in enumerate(pages):
        if n in FINAL_KEEP:
            page["title"] = f"Question {len(kept) + 1}: " + re.sub(r"^Bank LO\d+, Item \d+(?: \(Fallback\))?:\s*", "", page["title"])
            kept.append(page)
    results = next(pg for pg in pages if any(b["type"] == "results" for b in pg["blocks"]))
    s4["pages"] = [intro, *kept, results, pages[-1]]
    s4["title"] = "Final Check: Your Shift, Your Calls"
    return course


def leftovers(course: dict) -> list[str]:
    """Anything that still looks like a production note, so the build fails loudly instead of going public."""
    bad = re.compile(r"TO CONFIRM|FALLBACK|layer:|%[A-Za-z]|\bDana\b|Storyline|\[[A-Z][^\]]*\]|Hidden Tracking|Bank LO")
    found = []
    for s in course["sections"]:
        for p in s["pages"]:
            for b in p["blocks"]:
                t = json.dumps({k: v for k, v in b.items() if k not in ("id", "description", "src")}, ensure_ascii=False)
                found += [f"{p['title']}: {m.group(0)}" for m in bad.finditer(t)]
    return found


def main() -> None:
    design = json.loads((FAKE / "1_design.json").read_text(encoding="utf-8"))
    storyboard = json.loads((FAKE / "2_storyboard_draft3.json").read_text(encoding="utf-8"))
    course = C.from_storyboard(storyboard, design, "sample")
    course = walk(tidy(course))
    course["settings"]["accent"] = "#0E7A6C"  # the website's green
    course = C.normalize(course)
    course = C.ensure_results_page(course)
    if bad := leftovers(course):
        raise SystemExit("Still has production notes:\n  " + "\n  ".join(bad))
    if OUT.exists():
        shutil.rmtree(OUT)
    assets = OUT / "assets"
    drawn = P.draw_free(course, assets)
    (OUT / "course.json").write_text(json.dumps(course, ensure_ascii=False, indent=1), encoding="utf-8")
    pages = sum(len(s["pages"]) for s in course["sections"])
    print(f"Sample course: {pages} pages, {drawn} pictures drawn -> {OUT}")


if __name__ == "__main__":
    main()
