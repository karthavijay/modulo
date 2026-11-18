"""Build the sample forklift course as SCORM 1.2, SCORM 2004 and web packages, for the browser test.

    python authoring/tests/make_packages.py OUTDIR
"""
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from authoring import course as C  # noqa: E402
from authoring import export as E  # noqa: E402

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
samples = ROOT / "pipeline" / "samples" / "fake"
course = C.from_storyboard(json.loads((samples / "2_storyboard_draft3.json").read_text()),
                           json.loads((samples / "1_design.json").read_text()), "sample-forklift")
course["settings"]["max_attempts"] = 2
course = C.normalize(course)
for kind in E.KINDS:
    data = E.build(course, out / "no-assets", kind)
    problems = E.validate(data, kind)
    print(kind, len(data), "bytes", "OK" if not problems else problems)
    (out / f"{kind}.zip").write_bytes(data)
    d = out / kind
    d.mkdir(parents=True, exist_ok=True)
    zipfile.ZipFile(out / f"{kind}.zip").extractall(d)
# answer key for the browser test
key = {b["id"]: [i for i, o in enumerate(b["options"]) if o["correct"]]
       for s in course["sections"] for p in s["pages"] for b in p["blocks"] if b["type"] == "question"}
(out / "key.json").write_text(json.dumps(key))
