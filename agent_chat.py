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
PACK = {aid: {"name": n, "role": r, "prompt": pr, "doc": t} for aid, n, r, pr, t in pipeline.PACK_AGENTS}
AGENTS.update(PACK)

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
PACK_CHAT_SCHEMA = pipeline._obj({"reply": {"type": "string"}, "revised_document": {"anyOf": [{"type": "string"}, {"type": "null"}]}})
for _aid in PACK:
    SCHEMAS[_aid] = PACK_CHAT_SCHEMA

PACK_RULES = """You are now in a private one-on-one chat with the studio owner about this course. They are your manager, not the client.
Answer plainly and briefly in "reply": plain text, short paragraphs, no markdown headings, no em dashes.
Your own document ({doc}) is included below if you have written it. Only when the owner asks you to change it, put the complete revised document in Markdown in "revised_document" and say in "reply" what you changed. Otherwise set "revised_document" to null.
If your document has not been written yet, answer questions and share ideas; it is written when the owner starts the production pack.
Nothing is saved until the owner presses Save."""

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
    drafts = sorted(out.glob("2_storyboard_draft*.json"), key=lambda p: int(p.stem.rsplit("draft", 1)[1]))
    final = _read(out / "storyboard.json")
    return {
        "out": out,
        "brief": (jobs.job_dir(job_id) / "brief.txt").read_text(encoding="utf-8"),
        "design": _read(out / "1_design.json"),
        "storyboard": final,
        "draft": None if final else (_read(drafts[-1]) if drafts else None),
        "qa_rounds": [_read(p) for p in qa],
    }


STAGE = {
    "designing": "Iris is writing the first course design right now.",
    "revising": "Iris is revising the design with the owner's feedback right now.",
    "awaiting_approval": "The design is waiting for the owner's approval. The storyboard has not been started.",
    "storyboarding": "Production is running right now: Theo is writing or fixing the storyboard and Quinn is reviewing it. Any storyboard below is a work-in-progress draft.",
    "done": "Production is finished. The storyboard below is the delivered version.",
    "failed": "Production stopped with a problem and is waiting for the owner to press Try again.",
}


def editable(job: dict, f: dict, agent: str) -> str | None:
    """None if this agent's suggested changes can be saved now, else the reason (shown to the agent)."""
    if agent == "quinn":
        return "Quinn does not edit."
    if job["status"] in jobs.WORKING:
        return ("The team is working on this course right now, so changes can't be saved until the current step finishes. "
                "You may still suggest changes; the owner can ask again or save them afterwards.")
    if agent == "iris" and not f["design"]:
        return "There is no design yet to change."
    if agent in PACK and not (f["out"] / "pack" / f"{agent}.md").exists():
        return "Your document has not been written yet, so there is nothing to change."
    if agent == "theo" and not f["storyboard"]:
        return "There is no finished storyboard yet, so screens can't be changed. Answer questions and suggest ideas only."
    return None


def context_text(job: dict, f: dict, agent: str) -> str:
    parts = [pipeline.block("client_brief", f["brief"])]
    parts.append(pipeline.block("current_design", f["design"]) if f["design"]
                 else "<current_design>Not written yet.</current_design>")
    if f["storyboard"]:
        parts.append(pipeline.block("current_storyboard", f["storyboard"]))
    elif f["draft"]:
        parts.append(pipeline.block("storyboard_work_in_progress_draft", f["draft"]))
    else:
        parts.append("<current_storyboard>Not written yet.</current_storyboard>")
    if f["qa_rounds"]:
        parts.append(pipeline.block("latest_qa_review", f["qa_rounds"][-1]))
    for heading, text in pipeline.pack_docs(f["out"]):
        parts.append(pipeline.block("team_document", f"# {heading}\n\n{text}"))
    return "\n\n".join(parts)


def status_note(job: dict, f: dict, agent: str) -> str:
    why = editable(job, f, agent)
    edit = ("You can propose changes now; they are saved only when the owner presses Save." if why is None
            else "Do not propose changes in the change fields right now (leave them empty or null). Reason: " + why)
    return f"<course_status>{STAGE.get(job['status'], job['status'])} {edit}</course_status>"


def available(job: dict, agent: str) -> str | None:
    """Every agent can chat as soon as a brief exists."""
    return None if (jobs.job_dir(job["id"]) / "brief.txt").exists() else "This course has no brief."


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
    turns[0]["content"].insert(0, {"type": "text", "text": context_text(job, f, agent), "cache_control": {"type": "ephemeral"}})
    # The status can change during a conversation, so it goes with the latest message, after the cached part.
    turns[-1]["content"].insert(1 if len(turns) == 1 else 0, {"type": "text", "text": status_note(job, f, agent)})

    _client = _client or anthropic.AsyncAnthropic()
    try:
        async with _client.beta.messages.stream(
            model=MODEL,
            max_tokens=48000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMAS[agent]}},
            system=pipeline.agent_prompt(AGENTS[agent]["prompt"]) + "\n\n" + (
                CHAT_RULES[agent] if agent in CHAT_RULES else PACK_RULES.format(doc=AGENTS[agent]["doc"])),
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
    except Exception as e:  # e.g. no key set at all
        log.exception("Agent chat failed")
        return {"error": f"The agent could not reply ({e.__class__.__name__}: {str(e)[:200]}). Check the Claude key on the dashboard."}, 500

    if msg.stop_reason == "refusal":
        return {"reply": "Sorry, I can't help with that one.", "proposal": None}, 200
    if msg.stop_reason == "max_tokens":
        return {"error": "That answer was too long to finish. Try asking for a smaller change."}, 502
    data = json.loads("".join(b.text for b in msg.content if b.type == "text"))
    log.info("Agent chat %s/%s: %s in, %s out, %s cached", job["id"], agent, msg.usage.input_tokens,
             msg.usage.output_tokens, msg.usage.cache_read_input_tokens)
    proposal = _save_proposal(job, agent, data) if editable(job, f, agent) is None else None
    return {"reply": data["reply"], "proposal": proposal}, 200


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
    elif agent in PACK and data.get("revised_document"):
        change = {"document": data["revised_document"]}
        summary = f"Revised {AGENTS[agent]['doc'].lower()}"
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
    if "document" in p:
        md = out_doc = f["out"] / "pack" / f"{p['agent']}.md"
        pipeline.save(out_doc.with_name(f"{p['agent']}_before_{time.strftime('%Y%m%d-%H%M%S')}.md"), md.read_text(encoding="utf-8"))
        pipeline.save(md, p["document"])
        pipeline.rebuild_pack_docx(f["out"])
        pf.rename(pf.with_suffix(".saved"))
        jobs.note(job, f"Saved from chat with {AGENTS[p['agent']]['name']}: {p['summary']}")
        return p["summary"] + " saved. The production pack Word file has been rebuilt."
    if "design" not in p and not f["storyboard"]:
        raise ValueError("There is no finished storyboard to change yet.")
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
