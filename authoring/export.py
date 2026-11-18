"""Turn a course.json into a playable package: SCORM 1.2, SCORM 2004 (3rd edition) or a plain website zip."""

from __future__ import annotations

import html
import io
import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

PLAYER = Path(__file__).resolve().parent / "player"
KINDS = {
    "scorm12": ("SCORM 1.2", "1.2"),
    "scorm2004": ("SCORM 2004", "2004"),
    "web": ("Website (no LMS)", "none"),
}


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text or "course").strip("_")[:60] or "course"


def course_js(course: dict, version: str) -> str:
    data = json.dumps(course, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return f"window.MODULO_SCORM={json.dumps(version)};\nwindow.COURSE={data};\n"


def index_html(course: dict) -> str:
    page = (PLAYER / "index.html").read_text(encoding="utf-8")
    page = page.replace("<title>Course</title>", f"<title>{html.escape(course['title'])}</title>")
    return page.replace('<html lang="en">', f'<html lang="{html.escape(course.get("language") or "en")}">')


def used_assets(course: dict, assets_dir: Path) -> list[Path]:
    names = {b["src"][len("assets/"):] for s in course["sections"] for p in s["pages"] for b in p["blocks"]
             if b["type"] == "image" and b.get("src", "").startswith("assets/")}
    return [assets_dir / n for n in sorted(names) if (assets_dir / n).is_file()]


def _manifest_12(course: dict, files: list[str], ident: str, scored: bool) -> str:
    t = html.escape(course["title"])
    mastery = f"\n        <adlcp:masteryscore>{int(course['settings']['pass_mark'])}</adlcp:masteryscore>" if scored else ""
    file_tags = "\n      ".join(f'<file href="{html.escape(f)}"/>' for f in files)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<manifest identifier="{ident}" version="1.0"
  xmlns="http://www.imsproject.org/xsd/imscp_rootv1p1p2"
  xmlns:adlcp="http://www.adlnet.org/xsd/adlcp_rootv1p2"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xsi:schemaLocation="http://www.imsproject.org/xsd/imscp_rootv1p1p2 imscp_rootv1p1p2.xsd http://www.imsglobal.org/xsd/imsmd_rootv1p2p1 imsmd_rootv1p2p1.xsd http://www.adlnet.org/xsd/adlcp_rootv1p2 adlcp_rootv1p2.xsd">
  <metadata>
    <schema>ADL SCORM</schema>
    <schemaversion>1.2</schemaversion>
  </metadata>
  <organizations default="ORG-1">
    <organization identifier="ORG-1">
      <title>{t}</title>
      <item identifier="ITEM-1" identifierref="RES-1" isvisible="true">
        <title>{t}</title>{mastery}
      </item>
    </organization>
  </organizations>
  <resources>
    <resource identifier="RES-1" type="webcontent" adlcp:scormtype="sco" href="index.html">
      {file_tags}
    </resource>
  </resources>
</manifest>
"""


def _manifest_2004(course: dict, files: list[str], ident: str, scored: bool) -> str:
    t = html.escape(course["title"])
    file_tags = "\n      ".join(f'<file href="{html.escape(f)}"/>' for f in files)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<manifest identifier="{ident}" version="1"
  xmlns="http://www.imsglobal.org/xsd/imscp_v1p1"
  xmlns:adlcp="http://www.adlnet.org/xsd/adlcp_v1p3"
  xmlns:adlseq="http://www.adlnet.org/xsd/adlseq_v1p3"
  xmlns:adlnav="http://www.adlnet.org/xsd/adlnav_v1p3"
  xmlns:imsss="http://www.imsglobal.org/xsd/imsss"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xsi:schemaLocation="http://www.imsglobal.org/xsd/imscp_v1p1 imscp_v1p1.xsd http://www.adlnet.org/xsd/adlcp_v1p3 adlcp_v1p3.xsd http://www.adlnet.org/xsd/adlseq_v1p3 adlseq_v1p3.xsd http://www.adlnet.org/xsd/adlnav_v1p3 adlnav_v1p3.xsd http://www.imsglobal.org/xsd/imsss imsss_v1p0.xsd">
  <metadata>
    <schema>ADL SCORM</schema>
    <schemaversion>2004 3rd Edition</schemaversion>
  </metadata>
  <organizations default="ORG-1">
    <organization identifier="ORG-1">
      <title>{t}</title>
      <item identifier="ITEM-1" identifierref="RES-1" isvisible="true">
        <title>{t}</title>
      </item>
    </organization>
  </organizations>
  <resources>
    <resource identifier="RES-1" type="webcontent" adlcp:scormType="sco" href="index.html">
      {file_tags}
    </resource>
  </resources>
</manifest>
"""


def build(course: dict, assets_dir: Path, kind: str) -> bytes:
    """Return the zip file for one package kind."""
    if kind not in KINDS:
        raise ValueError(kind)
    version = KINDS[kind][1]
    files: dict[str, bytes] = {
        "index.html": index_html(course).encode("utf-8"),
        "player.js": (PLAYER / "player.js").read_bytes(),
        "player.css": (PLAYER / "player.css").read_bytes(),
        "course.js": course_js(course, version).encode("utf-8"),
    }
    for a in used_assets(course, assets_dir):
        files[f"assets/{a.name}"] = a.read_bytes()
    scored = any(b["type"] == "question" and b.get("scored") for s in course["sections"] for p in s["pages"] for b in p["blocks"])
    ident = "MODULO-" + re.sub(r"[^A-Za-z0-9-]", "-", course.get("id") or "course")[:60] + f"-r{course.get('revision', 0)}"
    if kind == "scorm12":
        files["imsmanifest.xml"] = _manifest_12(course, sorted(files), ident, scored).encode("utf-8")
    elif kind == "scorm2004":
        files["imsmanifest.xml"] = _manifest_2004(course, sorted(files), ident, scored).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # imsmanifest.xml first and at the root, as LMSs expect
        for name in sorted(files, key=lambda n: (n != "imsmanifest.xml", n)):
            z.writestr(name, files[name])
    data = buf.getvalue()
    problems = validate(data, kind)
    if problems:
        raise RuntimeError("The package failed its own checks: " + "; ".join(problems))
    return data


def filename(course: dict, kind: str) -> str:
    suffix = {"scorm12": "SCORM12", "scorm2004": "SCORM2004", "web": "web"}[kind]
    return f"{slug(course['title'])}_{suffix}.zip"


# ---------------------------------------------------------------- self-check

NS12 = {"cp": "http://www.imsproject.org/xsd/imscp_rootv1p1p2", "adlcp": "http://www.adlnet.org/xsd/adlcp_rootv1p2"}
NS04 = {"cp": "http://www.imsglobal.org/xsd/imscp_v1p1", "adlcp": "http://www.adlnet.org/xsd/adlcp_v1p3"}


def validate(data: bytes, kind: str) -> list[str]:
    """Structural checks an LMS import does: manifest at the root, valid XML, the right SCORM
    version, one SCO whose launch file exists, every listed file present, and the player files present."""
    problems: list[str] = []
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return ["not a valid zip file"]
    names = set(z.namelist())
    if z.testzip() is not None:
        problems.append("a file in the zip is damaged")
    for f in ("index.html", "player.js", "player.css", "course.js"):
        if f not in names:
            problems.append(f"{f} is missing")
    try:
        js = z.read("course.js").decode("utf-8")
        payload = js.split("window.COURSE=", 1)[1].rstrip().rstrip(";")
        json.loads(payload)
    except Exception:  # noqa: BLE001
        problems.append("course.js does not contain readable course data")
    if kind == "web":
        return problems
    if "imsmanifest.xml" not in names:
        return problems + ["imsmanifest.xml is not at the root of the zip"]
    try:
        root = ET.fromstring(z.read("imsmanifest.xml"))
    except ET.ParseError as e:
        return problems + [f"imsmanifest.xml is not valid XML ({e})"]
    ns = NS12 if kind == "scorm12" else NS04
    want = "1.2" if kind == "scorm12" else "2004 3rd Edition"
    if root.tag != f"{{{ns['cp']}}}manifest":
        problems.append("imsmanifest.xml uses the wrong namespace for " + KINDS[kind][0])
    ver = root.findtext("cp:metadata/cp:schemaversion", namespaces=ns)
    if ver != want:
        problems.append(f"schemaversion is {ver!r}, expected {want!r}")
    orgs = root.find("cp:organizations", ns)
    default = orgs.get("default") if orgs is not None else None
    org_ids = [o.get("identifier") for o in root.findall("cp:organizations/cp:organization", ns)]
    if not default or default not in org_ids:
        problems.append("the default organization is missing")
    resources = {r.get("identifier"): r for r in root.findall("cp:resources/cp:resource", ns)}
    items = root.findall(".//cp:item", ns)
    if not items:
        problems.append("there is no item to launch")
    attr = f"{{{ns['adlcp']}}}" + ("scormtype" if kind == "scorm12" else "scormType")
    for it in items:
        ref = it.get("identifierref")
        res = resources.get(ref)
        if res is None:
            problems.append(f"item {it.get('identifier')} points to a missing resource {ref}")
            continue
        if res.get(attr) != "sco":
            problems.append(f"resource {ref} is not marked as a SCO")
        if res.get("href") not in names:
            problems.append(f"launch file {res.get('href')} is missing")
    listed = {f.get("href") for f in root.findall(".//cp:file", ns)}
    for f in sorted(listed - names):
        problems.append(f"listed file {f} is missing from the zip")
    for f in sorted(names - listed - {"imsmanifest.xml"}):
        if not f.endswith("/"):
            problems.append(f"{f} is in the zip but not listed in the manifest")
    return problems
