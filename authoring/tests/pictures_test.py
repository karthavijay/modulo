"""Checks the drawn illustrations: every kind of picture is valid SVG, nothing from a description can
inject markup, the free drawings fill every placeholder, and Vera's scenes are used (with the free
drawing as a fallback). No network, no API key, no browser needed.

    python authoring/tests/pictures_test.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from xml.dom import minidom

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))
from authoring import course as C  # noqa: E402
from authoring import illustrate as I  # noqa: E402
from authoring import pictures as P  # noqa: E402

failures = 0


def check(ok, msg):
    global failures
    print(("  PASS " if ok else "  FAIL ") + msg)
    failures += not ok


def valid(svg):
    try:
        minidom.parseString(svg)
        return True
    except Exception as e:  # noqa: BLE001
        print("     ", e)
        return False


# 1. every kind, setting, prop, pose and glyph draws valid SVG
side = {"setting": "warehouse", "people": [{"position": "center", "pose": "clipboard", "outfit": "hi_vis"}],
        "props": [{"name": "pallet", "position": "right", "state": "normal"}], "badge": "check"}
specs = [{"kind": k, "setting": s, "glyphs": list(I.GLYPHS)[:6], "highlight": 1, "loop_back_to": 0, "items": 7, "trend": "down",
          "people": [{"position": pos, "pose": pose, "outfit": o} for pos, pose, o in zip(I.POS, I.POSES, I.OUTFITS * 2)],
          "props": [{"name": n, "position": list(I.POS)[i % 7], "state": I.STATES[i % len(I.STATES)]} for i, n in enumerate(I.PROPS)],
          "focus": "center", "badge": "warning", "left": side, "right": side, "alt": "A test picture"}
         for k in I.KINDS for s in I.SETTINGS]
specs += [{"kind": "icon", "glyphs": [g]} for g in I.GLYPHS]
specs += [{"kind": "scene", "props": [{"name": n, "position": "center", "state": st}]} for n in I.PROPS for st in I.STATES]
check(all(valid(I.draw(s)[0]) for s in specs), f"{len(specs)} pictures are all valid SVG")

# 2. nothing from a description gets into the markup except escaped alt text
evil = '"><script>alert(1)</script>'
svg = I.draw({"kind": evil, "setting": evil, "people": [{"position": evil, "pose": evil, "outfit": evil, "shirt": evil, "skin": evil}],
              "props": [{"name": "desk", "position": evil, "state": evil}], "glyphs": [evil], "focus": evil, "badge": evil,
              "highlight": evil, "items": evil, "alt": evil}, accent=evil)[0]
check(valid(svg) and "<script" not in svg and "&lt;script&gt;" in svg, "bad input is ignored and alt text is escaped")
check(I.draw({"kind": "scene"}, accent="#123456")[0].count("#123456") > 0, "the course accent color is used")

# 3. the free drawings fill every placeholder in the sample course
samples = ROOT / "pipeline" / "samples" / "fake"
course = C.from_storyboard(json.loads((samples / "2_storyboard_draft3.json").read_text()),
                           json.loads((samples / "1_design.json").read_text()), "sample-forklift")
before = len(P.placeholders(course))
with tempfile.TemporaryDirectory() as d:
    assets = Path(d)
    n = P.draw_free(course, assets)
    files = list(assets.glob("drawn-*.svg"))
    check(before > 10 and n == before and not P.placeholders(course), f"{n} of {before} placeholders drawn for free")
    check(len(files) == n and all(valid(f.read_text()) for f in files), "each drawing is saved as a valid SVG file")
    saved = C.normalize(course)
    srcs = [b["src"] for s in saved["sections"] for p in s["pages"] for b in p["blocks"] if b["type"] == "image"]
    check(all(s.startswith("assets/drawn-") and s.endswith(".svg") for s in srcs), "the course keeps the drawn pictures after clean up")
    check("still a placeholder" not in " ".join(C.check(saved)["note"]), "no 'Image to come' reminder once drawn")
    from authoring import export as E
    z = E.build(saved, assets, "scorm12")
    check(not E.validate(z, "scorm12") and z.count(b"drawn-") > 0, "the drawings go into the SCORM package")

# 4. Vera's scenes are used; any she misses get the free drawing
import pipeline  # noqa: E402

course = C.from_storyboard(json.loads((samples / "2_storyboard_draft3.json").read_text()), None, "sample")
todo = P.placeholders(course)
calls = []


async def fake_vera(name, task, schema, ledger, label, **kw):
    calls.append((name, label))
    assert schema is P.SCHEMA
    first = todo[0][1]["id"]
    return {"pictures": [{"block_id": first, "kind": "icon", "setting": "plain", "people": [], "props": [], "focus": "none",
                          "badge": "none", "glyphs": ["lock"], "highlight": -1, "loop_back_to": -1, "items": 0, "trend": "up",
                          "left": side, "right": side, "alt": "A padlock"}]}

pipeline.FAKE = False
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
pipeline.run_agent = fake_vera
with tempfile.TemporaryDirectory() as d:
    n = asyncio.run(P.draw_with_vera(course, Path(d), None))
    first = todo[0][1]
    check(n == len(todo) and not P.placeholders(course), "Vera's run fills every placeholder")
    check(len(calls) == -(-len(todo) // P.BATCH) and calls[0][0] == "illustrator", f"Vera is asked in {len(calls)} batch(es)")
    check(first["alt"] == "A padlock" and 'aria-label="A padlock"' in (Path(d) / first["src"][7:]).read_text(), "Vera's scene and alt text are used")

    async def broken(*a, **k):
        raise RuntimeError("API down")
    pipeline.run_agent = broken
    course2 = C.from_storyboard(json.loads((samples / "2_storyboard_draft3.json").read_text()), None, "sample")
    n2 = asyncio.run(P.draw_with_vera(course2, Path(d), None))
    check(n2 > 0 and not P.placeholders(course2), "if Vera can't be reached, the free drawings are used and the build carries on")

print("\nAll passed." if not failures else f"\n{failures} failed.")
sys.exit(1 if failures else 0)
