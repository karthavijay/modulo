"""Private one-on-one chat with Iris, Theo or Quinn about one course.

Each agent sees the brief, the current design, the current storyboard and the
latest QA review. Iris and Theo can propose changes; nothing is saved until
you press "Save to course", which also rebuilds the Word storyboard.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time

import anthropic

import jobs
import pipeline
from render import render_design_md, render_docx, render_qa_md, render_storyboard_md

log = logging.getLogger("modulo.agent_chat")
MODEL = os.environ.get("MODULO_MODEL", "claude-opus-5-5")
_client: anthropic.AsyncAnthropic | None = None

AGENTS = {
    "iris": {"name": "Iris", "role": "Instructional Designer", "prompt": "instructional_designer"},
    "theo": {"name": "Theo", "role": "Storyboard Writer", "prompt": "storyboard_writer"},
    "quinn": {"name": "Quinn", "role": "QA Reviewer", "prompt": "qa_reviewer"},
}

SCREEN = pipeline.STORYBOARD_SCHEMA["properties"]["screens"]["items"]
STRS = {"type": "array", "items": {"type": "string"}}

SCHEMAS = {
    "iris": pipeline._obj({
        "reply": {"type": "string"},
        "revised_design": {"anyOf": [pipeline.DESIGN_SCHEMA, {"type": "null"}]},
    }),
    "theo": pipeline._obj({
        "reply": {"type": "string"},
        "replace_screens": {"type": "array", "items": SCREEN},
        "insert_screens": {"type": "array", "items": pipeline._obj({"after_screen_id": {"type": "string"}, "screen": SCREEN})},
        "delete_screen_ids": STRS,
    }),
    "quinn": pipeline._obj({"reply": {"type": "string"}}),
}

CHAT_RULES = {
    "iris": """You are now in a private one-on-one chat with the studio owner about this course. They are your manager, not the client.
Answer questions about the design plainly and briefly. Plain text in "reply", no markdown headings, no em dashes.
Only when they ask you to change the design, put the complete revised design in "revised_design" and say in "reply" what you changed. Otherwise set "revised_design" to null.
Nothing is saved until the owner presses Save. If a storyboard already exists, remind them that a design change does not rewrite the storyboard by itself; Theo can update affected screens.""",
    "theo": """You are now in a private one-on-one chat with the studio owner about this course. They are your manager, not the client.
Answer questions about the storyboard plainly and briefly. Plain text in "reply", no markdown headings, no em dashes.
Only when they ask you to change screens: put each rewritten screen, complete and with its existing id, in "replace_screens"; new screens in "insert_screens" with the id of the screen they follow; ids to remove in "delete_screen_ids". Leave the lists empty otherwise. Say in "reply" which screens you changed and why.
Change only what was asked, keep the house style and objective mapping, and never invent client facts (use [CLIENT TO CONFIRM: ...]). Nothing is saved until the owner presses Save.""",
    "quinn": """You are now in a private one-on-one chat with the studio owner about this course. They are your manager, not the client.
Explain your findings, why you flagged something, how serious it is and how to fix it. You can also check specific screens on request. Plain text, short paragraphs, no markdown headings, no em dashes.
You do not edit content yourself; if a change is needed, say what Theo or Iris should do so the owner can ask them.""",
}


def _read(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def course_files(job_id: str) -> dict:
    out = jobs.job_dir(job_id) / "output"
    qa = sorted(out.glob("3_qa_round*.json"), key=lambda p: int(p.stem.rsplit("round", 1)[1]))
    return {
        "out": out,
        "brief": (jobs.job_dir(job_id) / "brief.txt").read_text(encoding="utf-8"),
        "design": _read(out / "1_design.json"),
        "storyboard": _read(out / "storyboard.json"),
        "qa_rounds": [_read(p) for p in qa],
    }


def context_text(f: dict) -> str:
    parts = [pipeline.block("client_brief", f["brief"])]
    if f["design"]:
        parts.append(pipeline.block("current_design", f["design"]))
    if f["storyboard"]:
        parts.append(pipeline.block("current_storyboard", f["storyboard"]))
    elif f["design"]:
        parts.append("<current_storyboard>Not written yet.</current_storyboard>")
    if f["qa_rounds"]:
        parts.append(pipeline.block("latest_qa_review", f["qa_rounds"][-1]))
    return "\n\n".join(parts)


def available(job: dict, agent: str) -> str | None:
    """Return None if this agent can chat about this course, else why not."""
    f = course_files(job["id"])
    if not f["design"]:
        return "Iris hasn't written the design yet. Try again in a few minutes."
    if agent in {"theo", "quinn"} and not f["storyboard"]:
        return f"{AGENTS[agent]['name']} can chat once the storyboard is finished."
    return None


async def reply(job: dict, agent: str, history: list) -> tuple[dict, int]:
    global _client
    why_not = available(job, agent)
    if why_not:
        return {"error": why_not}, 409
    f = course_files(job["id"])
    turns = []
    for m in history[-40:]:
        if not isinstance(m, dict) or not str(m.get("text") or "").strip():
            continue
        role = "user" if m.get("from") == "you" else "assistant"
        text = str(m["text"])[:8000]
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"][-1]["text"] += "\n\n" + text
        else:
            turns.append({"role": role, "content": [{"type": "text", "text": text}]})
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    if not turns or turns[-1]["role"] != "user":
        return {"error": "Type a message first."}, 400
    # The course material goes first and is cached, so follow-up messages are cheap.
    turns[0]["content"].insert(0, {"type": "text", "text": context_text(f), "cache_control": {"type": "ephemeral"}})

    _client = _client or anthropic.AsyncAnthropic()
    try:
        async with _client.beta.messages.stream(
            model=MODEL,
            max_tokens=48000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMAS[agent]}},
            system=pipeline.agent_prompt(AGENTS[agent]["prompt"]) + "\n\n" + CHAT_RULES[agent],
            messages=turns,
        ) as stream:
            msg = await stream.get_final_message()
    except anthropic.AuthenticationError as e:
        log.error("Claude rejected ANTHROPIC_API_KEY (401): %s", e.message)
        return {"error": "The Claude key is not working. Check ANTHROPIC_API_KEY in Render."}, 500
    except anthropic.RateLimitError:
        return {"error": "Claude is busy. Try again in a minute."}, 429
    except anthropic.APIStatusError as e:
        log.error("Anthropic API error %s: %s", e.status_code, e.message)
        return {"error": f"Claude returned an error ({e.status_code}). Try again, or check the Logs in Render."}, 502
    except anthropic.APIConnectionError as e:
        log.error("Could not reach Claude: %s", e)
        return {"error": "Could not reach Claude. Try again."}, 502

    if msg.stop_reason == "refusal":
        return {"reply": "Sorry, I can't help with that one.", "proposal": None}, 200
    if msg.stop_reason == "max_tokens":
        return {"error": "That answer was too long to finish. Try asking for a smaller change."}, 502
    data = json.loads("".join(b.text for b in msg.content if b.type == "text"))
    log.info("Agent chat %s/%s: %s in, %s out, %s cached", job["id"], agent, msg.usage.input_tokens,
             msg.usage.output_tokens, msg.usage.cache_read_input_tokens)
    return {"reply": data["reply"], "proposal": _save_proposal(job, agent, data)}, 200


def _save_proposal(job: dict, agent: str, data: dict) -> dict | None:
    change = None
    if agent == "iris" and data.get("revised_design"):
        change = {"design": data["revised_design"]}
        summary = "Revised course design"
    elif agent == "theo" and (data["replace_screens"] or data["insert_screens"] or data["delete_screen_ids"]):
        change = {k: data[k] for k in ("replace_screens", "insert_screens", "delete_screen_ids")}
        bits = []
        if data["replace_screens"]:
            bits.append("rewrite " + ", ".join(s["id"] for s in data["replace_screens"]))
        if data["insert_screens"]:
            bits.append("add " + ", ".join(s["screen"]["id"] for s in data["insert_screens"]))
        if data["delete_screen_ids"]:
            bits.append("remove " + ", ".join(data["delete_screen_ids"]))
        summary = "Storyboard: " + "; ".join(bits)
    if not change:
        return None
    pid = f"{int(time.time())}-{secrets.token_hex(3)}"
    d = jobs.job_dir(job["id"]) / "proposals"
    d.mkdir(exist_ok=True)
    (d / f"{pid}.json").write_text(json.dumps({"agent": agent, "summary": summary, **change}, ensure_ascii=False), encoding="utf-8")
    return {"id": pid, "summary": summary}


def apply(job: dict, proposal_id: str) -> str:
    """Save a proposed change into the course and rebuild the documents. Returns a message."""
    if not proposal_id.replace("-", "").isalnum():
        raise KeyError(proposal_id)
    pf = jobs.job_dir(job["id"]) / "proposals" / f"{proposal_id}.json"
    if not pf.exists():
        raise KeyError(proposal_id)
    p = json.loads(pf.read_text(encoding="utf-8"))
    f = course_files(job["id"])
    out, stamp = f["out"], time.strftime("%Y%m%d-%H%M%S")
    design, storyboard = f["design"], f["storyboard"]

    if "design" in p:
        pipeline.save(out / f"1_design_before_{stamp}.json", design)
        design = p["design"]
        pipeline.save(out / "1_design.json", design)
        pipeline.save(out / "1_design.md", render_design_md(design))
        job["title"] = design["title"]
    else:
        pipeline.save(out / f"storyboard_before_{stamp}.json", storyboard)
        screens = storyboard["screens"]
        by_id = {s["id"]: s for s in p["replace_screens"]}
        screens = [by_id.get(s["id"], s) for s in screens if s["id"] not in set(p["delete_screen_ids"])]
        for ins in p["insert_screens"]:
            idx = next((i for i, s in enumerate(screens) if s["id"] == ins["after_screen_id"]), len(screens) - 1)
            screens.insert(idx + 1, ins["screen"])
        storyboard = {**storyboard, "screens": screens}
        pipeline.save(out / "storyboard.json", storyboard)

    if storyboard:
        pipeline.save(out / "storyboard.md", render_storyboard_md(design, storyboard))
        pipeline.save(out / "qa_report.md", render_qa_md(f["qa_rounds"]))
        render_docx(design, storyboard, f["qa_rounds"], out / "storyboard.docx")
    pf.rename(pf.with_suffix(".saved"))
    jobs.note(job, f"Saved from chat with {AGENTS[p['agent']]['name']}: {p['summary']}")
    return p["summary"] + " saved." + (" The Word storyboard has been rebuilt." if storyboard else "")
