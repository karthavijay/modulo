"""Modulo course production pipeline.

Brief -> Instructional Designer -> (human approves design) -> Storyboard Writer
-> QA Reviewer -> (Storyboard Writer fixes, QA re-checks) -> storyboard document.

Run:  python pipeline.py samples/forklift_brief.txt
See README.md for details.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import anthropic
import anyio

from render import render_design_md, render_docx, render_qa_md, render_storyboard_md

HERE = Path(__file__).resolve().parent
MODEL = os.environ.get("MODULO_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("MODULO_EFFORT", "high")
# Cheaper model for the simpler planning documents (Lars, Nia, Paige). Set it to
# claude-haiku-5-5 to save even more, or to the same as MODULO_MODEL to undo the change.
LIGHT_MODEL = os.environ.get("MODULO_LIGHT_MODEL", "claude-sonnet-5-5")
LIGHT_AGENTS = {"lms_administrator", "learning_analyst", "project_manager"}


def model_for(agent: str) -> str:
    """Which Claude model an agent (instructions file name) runs on."""
    return LIGHT_MODEL if agent in LIGHT_AGENTS else MODEL


# US dollars per million tokens: (input, output, cache read). Cache writes cost
# 1.25x input (5 minute cache) or 2x input (1 hour cache).
PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
}


def cost_usd(model: str, usage) -> float:
    """What one reply cost, from the token counts the API returns."""
    inp, out, read = PRICES.get(model, PRICES["claude-opus-5-5"])
    if model == "claude-haiku-5-5":  # Haiku has a higher rate card for prompts over 100K tokens
        prompt = (usage.input_tokens or 0) + (usage.cache_read_input_tokens or 0) + (usage.cache_creation_input_tokens or 0)
        if prompt > 100_000:
            inp, out, read = 0.50, 2.50, 0.05
    created = usage.cache_creation_input_tokens or 0
    c = getattr(usage, "cache_creation", None)
    long_writes = (getattr(c, "ephemeral_1h_input_tokens", 0) or 0) if c else 0
    short_writes = max(created - long_writes, 0)
    total = ((usage.input_tokens or 0) * inp + short_writes * inp * 1.25 + long_writes * inp * 2
             + (usage.cache_read_input_tokens or 0) * read + (usage.output_tokens or 0) * out)
    return total / 1_000_000

# ---------------------------------------------------------------------------
# Output shapes each agent must return (JSON Schema)
# ---------------------------------------------------------------------------

def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required if required is not None else list(props),
        "properties": props,
    }

STR = {"type": "string"}
STRS = {"type": "array", "items": STR}
NUM = {"type": "number"}

DESIGN_SCHEMA = _obj({
    "title": STR,
    "summary": STR,
    "audience": STR,
    "business_goal": STR,
    "seat_time_minutes": NUM,
    "approach": STR,
    "objectives": {"type": "array", "items": _obj({"id": STR, "text": STR, "bloom_level": STR})},
    "modules": {"type": "array", "items": _obj({
        "id": STR, "title": STR, "duration_minutes": NUM,
        "objective_ids": STRS, "topics": STRS, "interactions": STRS,
    })},
    "assessment": _obj({"description": STR, "pass_mark": STR, "item_count": NUM}),
    "delivery_notes": STRS,
    "assumptions": STRS,
    "open_questions": STRS,
})

STORYBOARD_SCHEMA = _obj({
    "course_title": STR,
    "screens": {"type": "array", "items": _obj({
        "id": STR, "module": STR, "title": STR, "screen_type": STR,
        "on_screen_text": STR, "narration": STR, "visuals": STR,
        "interaction": STR, "feedback": STR, "objective_ids": STRS, "notes": STR,
    })},
})

SCREEN_SCHEMA = STORYBOARD_SCHEMA["properties"]["screens"]["items"]

# Theo's QA fix rounds: only the screens that change, not the whole storyboard again.
STORYBOARD_FIX_SCHEMA = _obj({
    "replace_screens": {"type": "array", "items": SCREEN_SCHEMA},
    "insert_screens": {"type": "array", "items": _obj({"after_screen_id": STR, "screen": SCREEN_SCHEMA})},
    "delete_screen_ids": STRS,
})


def apply_screen_changes(storyboard: dict, changes: dict) -> dict:
    """Apply rewritten, new and removed screens to a storyboard. Returns a new storyboard."""
    gone = set(changes.get("delete_screen_ids") or [])
    by_id = {sc["id"]: sc for sc in changes.get("replace_screens") or []}
    screens = [by_id.get(sc["id"], sc) for sc in storyboard["screens"] if sc["id"] not in gone]
    for ins in changes.get("insert_screens") or []:
        idx = next((i for i, sc in enumerate(screens) if sc["id"] == ins["after_screen_id"]), len(screens) - 1)
        screens.insert(idx + 1, ins["screen"])
    return {**storyboard, "screens": screens}


QA_SCHEMA = _obj({
    "pass": {"type": "boolean"},
    "summary": STR,
    "estimated_seat_time_minutes": NUM,
    "findings": {"type": "array", "items": _obj({
        "severity": {"type": "string", "enum": ["critical", "major", "minor"]},
        "screen_id": STR, "issue": STR, "fix": STR,
    })},
})

# ---------------------------------------------------------------------------
# Running one agent
# ---------------------------------------------------------------------------

class Ledger:
    """Tracks the cost of each step in run_log.json, across resumed runs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.steps: list[dict] = old.get("steps", [])

    def add(self, step: dict) -> None:
        self.steps.append(step)
        self.path.write_text(json.dumps({"model": MODEL, "light_model": LIGHT_MODEL, "effort": EFFORT, "steps": self.steps,
                                         "total_cost_usd": self.total}, indent=2), encoding="utf-8")

    @property
    def total(self) -> float:
        return round(sum(s["cost_usd"] for s in self.steps), 4)


def agent_prompt(name: str) -> str:
    return (HERE / "agents" / f"{name}.md").read_text(encoding="utf-8")


FAKE = os.environ.get("MODULO_FAKE", "").lower() in {"1", "true", "yes"}
FAKE_ANSWERS = {  # test mode: canned answers from the forklift sample, no API calls, no cost
    "instructional_designer": "1_design.json",
    "storyboard_writer": "2_storyboard_draft3.json",
    "qa_reviewer": "3_qa_round3.json",
}


_client: anthropic.AsyncAnthropic | None = None

# The shared course documents go first, in exactly the same words for every agent,
# so Claude can reuse them from its cache instead of charging full price each time.
SHARED_INTRO = "These are the course materials for the current Modulo job. Your role and task come after them."


async def run_agent(name: str, task: str, schema: dict, ledger: Ledger, label: str,
                    shared: list[str] | None = None, cache: str = "1h") -> dict:
    """Run one agent and return its structured JSON answer.

    shared: course documents several agents read in turn (brief, design, storyboard).
    They are cached, so the next agent that sends the same documents pays about a
    twentieth of the normal price for them. cache: how long they stay cached, "5m" or "1h".
    """
    global _client
    print(f"  ... {label}", flush=True)
    if FAKE:
        await anyio.sleep(float(os.environ.get("MODULO_FAKE_SECONDS", "2")))
        if schema is STORYBOARD_FIX_SCHEMA:
            data = {"replace_screens": [], "insert_screens": [], "delete_screen_ids": []}
        elif name in FAKE_ANSWERS:
            data = json.loads((HERE / "samples" / "fake" / FAKE_ANSWERS[name]).read_text(encoding="utf-8"))
        else:
            data = {"title": label, "summary": "Test mode document.",
                    "document": f"## About this document\n\nTest mode. In a real run, {label.split()[0]} writes this.\n\n"
                                "| Screen | Note |\n|---|---|\n| M1-S01 | **Example** row |\n\n- First point\n- Second point\n"}
        ledger.add({"step": label + " (test mode)", "cost_usd": 0.0, "seconds": 0})
        return data

    model = model_for(name)
    cache_control = {"type": "ephemeral", "ttl": cache} if cache == "1h" else {"type": "ephemeral"}
    system: list[dict] = []
    for i, text in enumerate(shared or []):
        system.append({"type": "text", "text": (SHARED_INTRO + "\n\n" if i == 0 else "") + text,
                       "cache_control": cache_control})
    system.append({"type": "text", "text": agent_prompt(name)})
    extra = {} if model.startswith("claude-haiku") else {  # if the model declines, the API retries on a fallback model
        "betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}

    _client = _client or anthropic.AsyncAnthropic(max_retries=4)
    started = time.monotonic()
    async with _client.beta.messages.stream(
        model=model,
        max_tokens=128000,
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": schema}},
        system=system,
        messages=[{"role": "user", "content": task}],
        **extra,
    ) as stream:
        msg = await stream.get_final_message()
    seconds = round(time.monotonic() - started)
    cost = cost_usd(msg.model, msg.usage)
    u = msg.usage
    ledger.add({"step": label, "cost_usd": round(cost, 4), "seconds": seconds, "model": msg.model,
                "tokens": {"input": u.input_tokens, "cache_write": u.cache_creation_input_tokens or 0,
                           "cache_read": u.cache_read_input_tokens or 0, "output": u.output_tokens}})
    if msg.stop_reason == "refusal":
        raise RuntimeError(f"{label} failed: Claude declined this request.")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError(f"{label} failed: the answer was too long to finish.")
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    data = json.loads(text)
    print(f"      done in {seconds}s, ${cost:.2f} ({msg.model}, {u.cache_read_input_tokens or 0} tokens from cache)", flush=True)
    return data


def block(tag: str, content: str | dict) -> str:
    if not isinstance(content, str):
        content = json.dumps(content, indent=2, ensure_ascii=False)
    return f"<{tag}>\n{content}\n</{tag}>"

# ---------------------------------------------------------------------------
# The pipeline, as two stages so a web server can run them with an approval
# step in between (see ../jobs.py). The command line below uses the same stages.
# ---------------------------------------------------------------------------

def save(path: Path, data: dict | str) -> None:
    text = data if isinstance(data, str) else json.dumps(data, indent=2, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")


def _load(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


async def make_design(out: Path, brief: str, feedback: str | None = None) -> dict:
    """Stage 1: Iris writes the course design, or revises it with feedback."""
    out.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(out / "run_log.json")
    current = _load(out / "1_design.json")
    if feedback and current:
        design = await run_agent(
            "instructional_designer",
            block("client_brief", brief) + "\n\n" + block("current_design", current)
            + "\n\n" + block("reviewer_feedback", feedback)
            + "\n\nRevise the design to address all of the reviewer feedback.",
            DESIGN_SCHEMA, ledger, "Iris revises the design",
        )
    elif current:
        design = current
    else:
        design = await run_agent(
            "instructional_designer",
            block("client_brief", brief) + "\n\nWrite the course design for this brief.",
            DESIGN_SCHEMA, ledger, "Iris writes the design",
        )
    save(out / "1_design.json", design)
    save(out / "1_design.md", render_design_md(design))
    return design


async def make_storyboard(out: Path, brief: str, max_rounds: int = 2) -> dict:
    """Stage 2 (after approval): Theo storyboards, Quinn checks, Theo fixes.

    Finished drafts and QA rounds are reused, so an interrupted run resumes
    where it stopped. Returns a short summary for the delivery email.
    """
    ledger = Ledger(out / "run_log.json")
    design = _load(out / "1_design.json")
    if design is None:
        raise RuntimeError("There is no course design to storyboard yet.")
    # Brief and design are read by every step below, so they are cached for an hour.
    shared = [block("client_brief", brief) + "\n\n" + block("approved_design", design)]

    storyboard = _load(out / "2_storyboard_draft1.json")
    if storyboard is None:
        print("Step 3  Storyboard Writer drafts the storyboard", flush=True)
        storyboard = await run_agent(
            "storyboard_writer",
            "Write the complete storyboard for this approved design.",
            STORYBOARD_SCHEMA, ledger, "Theo writes the storyboard (draft 1)", shared=shared,
        )
        save(out / "2_storyboard_draft1.json", storyboard)

    qa_rounds: list[dict] = []
    for round_no in range(1, max_rounds + 2):
        print(f"Step 4  QA review, round {round_no}", flush=True)
        qa = _load(out / f"3_qa_round{round_no}.json") or await run_agent(
            "qa_reviewer",
            block("storyboard", storyboard) + "\n\nReview this storyboard.",
            QA_SCHEMA, ledger, f"Quinn reviews draft {round_no}", shared=shared,
        )
        qa_rounds.append(qa)
        save(out / f"3_qa_round{round_no}.json", qa)
        counts = {s: sum(f["severity"] == s for f in qa["findings"]) for s in ("critical", "major", "minor")}
        print(f"      QA: {'PASS' if qa['pass'] else 'needs fixes'} | {counts}", flush=True)
        if qa["pass"] or round_no > max_rounds:
            break
        revised = _load(out / f"2_storyboard_draft{round_no + 1}.json")
        if revised is None:
            changes = await run_agent(
                "storyboard_writer",
                block("current_storyboard", storyboard) + "\n\n" + block("qa_findings", qa)
                + "\n\nFix every QA finding. Return only the screens that change: each rewritten screen, complete and "
                "with its existing id, in \"replace_screens\"; new screens in \"insert_screens\" with the id of the "
                "screen they follow; ids to remove in \"delete_screen_ids\". Screens you leave out stay exactly as they are, "
                "so include every screen a finding touches, including a course-wide finding.",
                STORYBOARD_FIX_SCHEMA, ledger, f"Theo fixes QA findings (draft {round_no + 1})", shared=shared,
            )
            save(out / f"2_storyboard_fixes{round_no + 1}.json", changes)
            revised = apply_screen_changes(storyboard, changes)
        storyboard = revised
        save(out / f"2_storyboard_draft{round_no + 1}.json", storyboard)

    save(out / "storyboard.json", storyboard)
    save(out / "storyboard.md", render_storyboard_md(design, storyboard))
    save(out / "qa_report.md", render_qa_md(qa_rounds))
    render_docx(design, storyboard, qa_rounds, out / "storyboard.docx")
    final = qa_rounds[-1]
    return {
        "title": design["title"],
        "screens": len(storyboard["screens"]),
        "qa_rounds": len(qa_rounds),
        "passed": bool(final["pass"]),
        "open_findings": {s: sum(f["severity"] == s for f in final["findings"]) for s in ("critical", "major", "minor")},
        "cost_usd": ledger.total,
    }


# ---------------------------------------------------------------------------
# Stage 3 (optional, after its own approval): the rest of the team
# ---------------------------------------------------------------------------

# (id, name, role, instructions file, document title), in the order they run.
PACK_AGENTS = [
    ("sam", "Sam", "SME Liaison", "sme_liaison", "SME review pack"),
    ("vera", "Vera", "Visual Designer", "visual_designer", "Visual style guide and asset list"),
    ("mila", "Mila", "Voiceover and Media Producer", "media_producer", "Voiceover and media production brief"),
    ("dev", "Dev", "eLearning Developer", "elearning_developer", "Build specification"),
    ("lars", "Lars", "LMS and SCORM Administrator", "lms_administrator", "LMS launch plan"),
    ("nia", "Nia", "Learning Analyst", "learning_analyst", "Evaluation and measurement plan"),
    ("paige", "Paige", "Project Manager", "project_manager", "Project plan and client handover"),
]

PACK_SCHEMA = _obj({"title": STR, "summary": STR, "document": STR})


def pack_docs_with_ids(out: Path) -> list[tuple[str, str, str]]:
    """The finished team documents as (id, heading, markdown), in team order."""
    docs = []
    for aid, name, role, _, title in PACK_AGENTS:
        f = out / "pack" / f"{aid}.md"
        if f.exists():
            docs.append((aid, f"{title} ({name}, {role})", f.read_text(encoding="utf-8")))
    return docs


def pack_docs(out: Path) -> list[tuple[str, str]]:
    """The finished team documents as (heading, markdown), in team order."""
    return [(h, t) for _, h, t in pack_docs_with_ids(out)]


def rebuild_pack_docx(out: Path) -> None:
    from render import render_pack_docx
    design = _load(out / "1_design.json") or {"title": "Course"}
    docs = pack_docs(out)
    if docs:
        render_pack_docx(design["title"], docs, out / "production_pack.docx")


# What each team member reads. "full" = the whole storyboard; "outline" = one line per
# screen (id, title, type, objectives, questions, items to confirm) without narration
# or visual notes, which these roles never use. "team" = earlier team documents.
PACK_INPUTS = {
    "sam":   {"storyboard": "full",    "qa": True,  "team": []},
    "vera":  {"storyboard": "full",    "qa": False, "team": []},
    "mila":  {"storyboard": "full",    "qa": False, "team": ["vera"]},
    "dev":   {"storyboard": "full",    "qa": True,  "team": ["vera", "mila"]},
    "lars":  {"storyboard": "outline", "qa": False, "team": ["dev"]},
    "nia":   {"storyboard": "outline", "qa": False, "team": ["lars"]},
    "paige": {"storyboard": "outline", "qa": True,  "team": "all"},
}

_CONFIRM = re.compile(r"\[CLIENT TO CONFIRM:[^\]]*\]")
_QUESTION_TYPES = ("check", "assessment", "quiz", "question", "interaction", "scenario")


def storyboard_outline(storyboard: dict) -> dict:
    """A short version of the storyboard for roles that plan around it rather than build from it."""
    screens = []
    for sc in storyboard.get("screens", []):
        row = {k: sc.get(k) for k in ("id", "module", "title", "screen_type", "objective_ids")}
        if any(t in str(sc.get("screen_type", "")).lower() for t in _QUESTION_TYPES):
            row["interaction"] = sc.get("interaction", "")
        confirm = sorted(set(_CONFIRM.findall(json.dumps(sc, ensure_ascii=False))))
        if confirm:
            row["client_to_confirm"] = confirm
        words = len(str(sc.get("narration", "")).split())
        if words:
            row["narration_words"] = words
        screens.append(row)
    return {"course_title": storyboard.get("course_title", ""), "screen_count": len(screens), "screens": screens}


async def make_pack(out: Path, brief: str, progress=None) -> dict:
    """Each remaining team member writes their document, one after another. Resumable."""
    ledger = Ledger(out / "run_log.json")
    design, storyboard = _load(out / "1_design.json"), _load(out / "storyboard.json")
    if not design or not storyboard:
        raise RuntimeError("The production pack needs a finished storyboard first.")
    qa = sorted(out.glob("3_qa_round*.json"), key=lambda p: int(p.stem.rsplit("round", 1)[1]))
    latest_qa = _load(qa[-1]) if qa else None
    base = block("client_brief", brief) + "\n\n" + block("approved_design", design)
    shared = {  # identical text for every agent in a group, so the group shares one cache entry
        "full": [base + "\n\n" + block("storyboard", storyboard)],
        "outline": [base + "\n\n" + block("storyboard_outline", storyboard_outline(storyboard))],
    }
    (out / "pack").mkdir(exist_ok=True)
    start = ledger.total
    for aid, name, role, prompt, title in PACK_AGENTS:
        md = out / "pack" / f"{aid}.md"
        if md.exists():
            continue
        if progress:
            progress(f"{name} ({role}) is writing the {title.lower()}")
        needs = PACK_INPUTS.get(aid, {"storyboard": "full", "qa": True, "team": "all"})
        wanted = needs["team"]
        team = [(h, t) for tid, h, t in pack_docs_with_ids(out) if wanted == "all" or tid in wanted]
        parts = []
        if needs["qa"] and latest_qa:
            parts.append(block("latest_qa_review", latest_qa))
        parts += [block("team_document", f"# {h}\n\n{t}") for h, t in team]
        data = await run_agent(
            prompt,
            "".join(x + "\n\n" for x in parts) + f"Write your document: {title}. Put the whole document, in Markdown, in "
            "\"document\". Use ## headings for its sections and Markdown tables where they help. "
            "Put a one or two sentence summary for the studio owner in \"summary\".",
            PACK_SCHEMA, ledger, f"{name} writes the {title.lower()}",
            shared=shared[needs["storyboard"]], cache="1h" if needs["storyboard"] == "full" else "5m",
        )
        save(out / "pack" / f"{aid}.json", data)
        save(md, data["document"])
    rebuild_pack_docx(out)
    return {"documents": len(pack_docs(out)), "cost_usd": round(ledger.total - start, 4), "total_cost_usd": ledger.total}


# ---------------------------------------------------------------------------
# Command line (for running by hand on a computer)
# ---------------------------------------------------------------------------

def ask_for_approval(design_md: Path) -> str:
    """Return 'yes', 'stop', or the reviewer's change requests."""
    print("\n" + "=" * 70)
    print(f"HUMAN CHECK: please read the course design:\n  {design_md}")
    print("Type  y  to approve, type your requested changes, or press Enter to stop for now.")
    print("=" * 70)
    answer = input("> ").strip()
    if answer.lower() in {"y", "yes", "approve", "approved"}:
        return "yes"
    return answer or "stop"


async def main_async(args: argparse.Namespace) -> int:
    brief_path = Path(args.brief)
    brief = brief_path.read_text(encoding="utf-8")
    out = Path(args.out or HERE / "output" / brief_path.stem)
    if args.fresh and out.exists():
        for f in out.glob("*"):
            f.unlink()
    print(f"Modulo pipeline | model {MODEL} | effort {EFFORT}\nBrief: {brief_path}\nOutput: {out}\n")
    print("Step 1  Course design")
    await make_design(out, brief)

    approval_file = out / "1_design_APPROVED.txt"
    while not approval_file.exists():
        if args.changes:
            answer, args.changes = args.changes, None
        elif args.approve:
            answer = "yes"
        elif not sys.stdin.isatty():
            print(f"\nStopped for human review. Read {out / '1_design.md'}, then run again with --approve,\n"
                  "or with --changes \"your feedback\" to have Iris revise it.")
            return 0
        else:
            answer = ask_for_approval(out / "1_design.md")
        if answer == "stop":
            print("Stopped. Run the same command again when you are ready to approve.")
            return 0
        if answer == "yes":
            save(approval_file, "Design approved for storyboarding.\n")
            break
        print("Step 1b Iris revises the design with your feedback")
        await make_design(out, brief, feedback=answer)

    summary = await make_storyboard(out, brief, args.max_rounds)
    print(f"\nDone. Storyboard: {out / 'storyboard.docx'}")
    print(f"Final QA: {'PASS' if summary['passed'] else 'still has open findings, see qa_report.md'}")
    print(f"Total cost for this course so far: ${summary['cost_usd']:.2f}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Turn a client course brief into a QA-checked storyboard.")
    p.add_argument("brief", help="Text file with the client brief or website chat transcript")
    p.add_argument("--out", help="Output folder (default: output/<brief name>)")
    p.add_argument("--approve", action="store_true", help="Approve the course design without asking")
    p.add_argument("--changes", help="Feedback for Iris to revise the design before approval")
    p.add_argument("--max-rounds", type=int, default=2, help="Max QA fix rounds (default 2)")
    p.add_argument("--fresh", action="store_true", help="Throw away earlier results and start over")
    return anyio.run(main_async, p.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
