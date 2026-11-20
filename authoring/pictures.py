"""Vera turns each "Image to come" placeholder into a drawn illustration, and the course uses it.

  1. Collect every image block that has no picture yet (its description came from Theo's storyboard via Dev).
  2. Vera (visual designer, on the lighter model) picks a scene for each: setting, people, props, icons.
     In test mode, for the free quick draft, or if Vera is unavailable, a keyword guess is used instead.
  3. illustrate.draw() turns each scene into an SVG in course/assets/, and the block points at it.

The description stays on the block, so the team can still see what was asked for, and every picture can
be replaced in the editor with an uploaded photo at any time.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from authoring import illustrate as I

log = logging.getLogger("modulo.pictures")
BATCH = 25  # pictures per request to Vera

ENUM = lambda values: {"type": "string", "enum": list(values)}  # noqa: E731
INT = {"type": "integer"}


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


PERSON = _obj({"position": ENUM(I.POS), "pose": ENUM(I.POSES), "outfit": ENUM(I.OUTFITS), "shirt": ENUM(I.SHIRTS),
               "skin": INT, "facing": ENUM(("left", "right"))})
PROP = _obj({"name": ENUM(I.PROPS), "position": ENUM(I.POS), "state": ENUM(I.STATES)})
SIDE = _obj({"setting": ENUM(I.SETTINGS), "people": {"type": "array", "items": PERSON}, "props": {"type": "array", "items": PROP}, "badge": ENUM(I.BADGES)})
SCENE = _obj({
    "block_id": {"type": "string"},
    "kind": ENUM(I.KINDS),
    "setting": ENUM(I.SETTINGS),
    "people": {"type": "array", "items": PERSON},
    "props": {"type": "array", "items": PROP},
    "focus": ENUM(("none", *I.POS)),
    "badge": ENUM(I.BADGES),
    "glyphs": {"type": "array", "items": ENUM(I.GLYPHS)},
    "highlight": INT,
    "loop_back_to": INT,
    "items": INT,
    "trend": ENUM(I.TRENDS),
    "left": SIDE,
    "right": SIDE,
    "alt": {"type": "string"},
})
SCHEMA = _obj({"pictures": {"type": "array", "items": SCENE}})


def placeholders(course: dict) -> list[tuple[dict, dict]]:
    """(page, block) for every image block with no picture yet."""
    return [(p, b) for s in course.get("sections", []) for p in s.get("pages", []) for b in p.get("blocks", [])
            if b.get("type") == "image" and not b.get("src")]


def _place(assets: Path, block: dict, spec: dict, accent: str) -> None:
    alt = (block.get("alt") or "").strip() or (spec.get("alt") or "").strip()
    svg, _, _ = I.draw({**spec, "alt": alt}, accent)
    name = f"drawn-{block['id']}.svg"
    (assets / name).write_text(svg, encoding="utf-8")
    block["src"] = f"assets/{name}"
    block["alt"] = alt


def draw_free(course: dict, assets: Path) -> int:
    """Fill every placeholder from a keyword guess (no AI, no cost). Returns how many were drawn."""
    assets.mkdir(parents=True, exist_ok=True)
    accent = (course.get("settings") or {}).get("accent")
    todo = placeholders(course)
    for _, b in todo:
        _place(assets, b, I.guess(b.get("description", ""), b.get("alt", "")), accent)
    return len(todo)


async def draw_with_vera(course: dict, assets: Path, ledger, design: dict | None = None, progress=None) -> int:
    """Vera picks a scene for each placeholder; any she skips (or if she can't be reached) get the free guess."""
    import pipeline  # imported here so the drawing code above works without the API client installed

    todo = placeholders(course)
    if not todo:
        return 0
    assets.mkdir(parents=True, exist_ok=True)
    accent = (course.get("settings") or {}).get("accent")
    specs: dict[str, dict] = {}
    if not pipeline.FAKE and os.environ.get("ANTHROPIC_API_KEY"):
        about = {"course_title": course.get("title", ""), "summary": (design or {}).get("summary", "") or course.get("description", ""),
                 "audience": (design or {}).get("audience", "")}
        chunks = [todo[s:s + BATCH] for s in range(0, len(todo), BATCH)]
        if progress:
            progress(f"Vera is drawing {len(todo)} pictures")

        async def plan(n: int, chunk: list) -> dict:
            items = [{"block_id": b["id"], "page_title": p.get("title", ""), "what_to_show": b.get("description", ""), "alt": b.get("alt", "")}
                     for p, b in chunk]
            first = n * BATCH + 1
            try:
                return await pipeline.run_agent(
                    "illustrator", pipeline.block("course", about) + "\n\n" + pipeline.block("pictures_needed", items)
                    + f"\n\nWrite one scene for each of these {len(items)} pictures, using each block_id exactly once.",
                    SCHEMA, ledger, f"Vera plans pictures {first} to {first + len(chunk) - 1}")
            except Exception:  # noqa: BLE001  a picture problem must never stop the course build
                log.warning("Vera could not plan pictures; using the free drawings instead", exc_info=True)
                return {}

        # all batches at once, so a long course takes about as long as a short one
        for data in await asyncio.gather(*(plan(n, c) for n, c in enumerate(chunks))):
            for sp in (data or {}).get("pictures", []):
                if isinstance(sp, dict) and sp.get("block_id"):
                    specs[sp["block_id"]] = sp
    for _, b in todo:
        sp = specs.get(b["id"])
        if sp:
            sp = {**sp, "focus": "" if sp.get("focus") == "none" else sp.get("focus")}
        _place(assets, b, sp or I.guess(b.get("description", ""), b.get("alt", "")), accent)
    return len(todo)
