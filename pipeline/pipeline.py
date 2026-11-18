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
import tempfile
from pathlib import Path

import anyio
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from render import render_design_md, render_docx, render_qa_md, render_storyboard_md

HERE = Path(__file__).resolve().parent
MODEL = os.environ.get("MODULO_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("MODULO_EFFORT", "high")

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
        self.path.write_text(json.dumps({"model": MODEL, "effort": EFFORT, "steps": self.steps,
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


async def run_agent(name: str, task: str, schema: dict, ledger: Ledger, label: str) -> dict:
    """Run one agent with no tools and return its structured JSON answer."""
    print(f"  ... {label}", flush=True)
    if FAKE:
        await anyio.sleep(float(os.environ.get("MODULO_FAKE_SECONDS", "2")))
        data = json.loads((HERE / "samples" / "fake" / FAKE_ANSWERS[name]).read_text(encoding="utf-8"))
        ledger.add({"step": label + " (test mode)", "cost_usd": 0.0, "seconds": 0})
        return data
    workdir = tempfile.mkdtemp(prefix="modulo-")  # empty folder: agents read nothing local
    options = ClaudeAgentOptions(
        model=MODEL,
        effort=EFFORT,
        system_prompt=agent_prompt(name),
        tools=[],                 # pure writing agents, no file or web access
        setting_sources=[],       # ignore any local Claude settings
        output_format={"type": "json_schema", "schema": schema},
        max_turns=6,
        cwd=workdir,
    )
    result: ResultMessage | None = None
    async for message in query(prompt=task, options=options):
        if isinstance(message, ResultMessage):
            result = message
    if result is None or result.is_error:
        detail = (result.errors or result.result) if result else "no result"
        raise RuntimeError(f"{label} failed: {detail}")
    data = result.structured_output
    if data is None:  # fall back to parsing the text answer
        text = (result.result or "").strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
        data = json.loads(text)
    cost = float(result.total_cost_usd or 0)
    ledger.add({"step": label, "cost_usd": round(cost, 4), "seconds": round(result.duration_ms / 1000)})
    print(f"      done in {result.duration_ms / 1000:.0f}s, ${cost:.2f}", flush=True)
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
    context = block("client_brief", brief) + "\n\n" + block("approved_design", design)

    storyboard = _load(out / "2_storyboard_draft1.json")
    if storyboard is None:
        print("Step 3  Storyboard Writer drafts the storyboard", flush=True)
        storyboard = await run_agent(
            "storyboard_writer",
            context + "\n\nWrite the complete storyboard for this approved design.",
            STORYBOARD_SCHEMA, ledger, "Theo writes the storyboard (draft 1)",
        )
        save(out / "2_storyboard_draft1.json", storyboard)

    qa_rounds: list[dict] = []
    for round_no in range(1, max_rounds + 2):
        print(f"Step 4  QA review, round {round_no}", flush=True)
        qa = _load(out / f"3_qa_round{round_no}.json") or await run_agent(
            "qa_reviewer",
            context + "\n\n" + block("storyboard", storyboard) + "\n\nReview this storyboard.",
            QA_SCHEMA, ledger, f"Quinn reviews draft {round_no}",
        )
        qa_rounds.append(qa)
        save(out / f"3_qa_round{round_no}.json", qa)
        counts = {s: sum(f["severity"] == s for f in qa["findings"]) for s in ("critical", "major", "minor")}
        print(f"      QA: {'PASS' if qa['pass'] else 'needs fixes'} | {counts}", flush=True)
        if qa["pass"] or round_no > max_rounds:
            break
        storyboard = _load(out / f"2_storyboard_draft{round_no + 1}.json") or await run_agent(
            "storyboard_writer",
            context + "\n\n" + block("current_storyboard", storyboard)
            + "\n\n" + block("qa_findings", qa)
            + "\n\nFix every QA finding and return the complete revised storyboard.",
            STORYBOARD_SCHEMA, ledger, f"Theo fixes QA findings (draft {round_no + 1})",
        )
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
