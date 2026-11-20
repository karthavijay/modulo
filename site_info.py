"""Public trust pages and the studio's own details.

  /sample/                  a finished sample course anyone can try (showcase/sample, made by showcase/make_sample.py)
  /security                 how client content is handled, in plain words
  /privacy, /terms          privacy policy and terms of service (starting points: have them checked by a lawyer)
  /api/site                 the public details the homepage shows (About, contact, pricing guide)
  /admin/website            Admin form to fill in those details

Every detail is optional. The homepage hides the About and Pricing sections until they are filled in, so the live
site never shows a placeholder.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Route

import db

HERE = Path(__file__).resolve().parent
SAMPLE = HERE / "showcase" / "sample"
META_KEY = "site_details"

# (key, label, help text, kind) in the order the Admin form shows them. kind: line, text, url, email
FIELDS = [
    ("studio", "Your studio", "", "heading"),
    ("legal_name", "Company name", "As registered, for the footer, privacy policy and terms. Example: Modulo Learning Studio Ltd.", "line"),
    ("location", "Where you are based", "City and country. Example: Bengaluru, India", "line"),
    ("contact_email", "Contact email", "Shown on the website. Use an address on your own domain if you have one, such as hello@yourstudio.com.", "email"),
    ("phone", "Phone (optional)", "", "line"),
    ("linkedin_url", "LinkedIn page (optional)", "Your company or personal LinkedIn link.", "url"),
    ("company_number", "Company registration number (optional)", "", "line"),
    ("about", "About section", "", "heading"),
    ("founder_name", "Your name", "The person behind Modulo. Buyers want to know who is accountable.", "line"),
    ("founder_role", "Your role", "Example: Founder and lead instructional designer", "line"),
    ("founder_photo_url", "Photo link (optional)", "A link to a square photo of you, such as your LinkedIn photo.", "url"),
    ("founder_bio", "A few lines about you", "Your background in learning and development: years, industries, a well-known client or two if you may name them.", "text"),
    ("pricing", "Pricing guide", "", "heading"),
    ("price_short", "Short course price", "For a course up to about 15 minutes. Example: from $2,500", "line"),
    ("price_standard", "Standard course price", "For a course of about 15 to 45 minutes. Example: from $5,000", "line"),
    ("price_program", "Larger programs", "Several courses or a curriculum. Example: from $15,000, or 'Let's talk'", "line"),
    ("price_note", "Note under the prices (optional)", "Example: Prices include design, storyboard, the built course and two rounds of changes.", "text"),
]
KEYS = [k for k, _, _, kind in FIELDS if kind != "heading"]
DEFAULTS = {"legal_name": "Modulo Learning Studio"}


# ---------------------------------------------------------------- the details

def get() -> dict:
    try:
        raw = json.loads(db.get_meta(META_KEY) or "{}")
    except (ValueError, TypeError):
        raw = {}
    out = {k: str(raw.get(k) or "").strip() for k in KEYS}
    for k, v in DEFAULTS.items():
        out[k] = out[k] or v
    return out


def _safe_url(v: str) -> str:
    return v if re.match(r"https://[^\s\"'<>]+$", v) else ""


def save(form) -> dict:
    details = {}
    for key, _, _, kind in FIELDS:
        if kind == "heading":
            continue
        v = str(form.get(key) or "").strip()[: 1200 if kind == "text" else 200]
        if kind == "url" and v and not v.startswith("https://"):
            v = "https://" + v.removeprefix("http://")
        if kind == "url":
            v = _safe_url(v)
        if kind == "email" and v and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
            raise ValueError("The contact email doesn't look right.")
        details[key] = v
    db.set_meta(META_KEY, json.dumps(details))
    return get()


def public() -> dict:
    d = get()
    d["has_about"] = bool(d["founder_name"])
    d["has_pricing"] = bool(d["price_short"] or d["price_standard"] or d["price_program"])
    return d


async def api_site(_: Request):
    return JSONResponse(public(), headers={"cache-control": "public, max-age=60"})


# ---------------------------------------------------------------- public pages

PAGE_CSS = """
:root{--bg:#F3F6F4;--surface:#fff;--fg:#13201B;--muted:#56655E;--line:#D3DDD7;--accent:#0E7A6C;--mark:#F4C53D;
--display:"Bricolage Grotesque","Avenir Next","Segoe UI",system-ui,sans-serif;--body:"Inter","Segoe UI",system-ui,-apple-system,sans-serif}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0D1412;--surface:#15201C;--fg:#E4EDE8;--muted:#97A8A0;--line:#2A3A33;--accent:#3CC2AE;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#0D1412;--surface:#15201C;--fg:#E4EDE8;--muted:#97A8A0;--line:#2A3A33;--accent:#3CC2AE;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.65 var(--body);-webkit-font-smoothing:antialiased}
a{color:var(--accent)}a:focus-visible{outline:3px solid var(--mark);outline-offset:2px}
.wrap{max-width:760px;margin:0 auto;padding:0 max(16px,env(safe-area-inset-left,0px))}
header{border-bottom:1px solid var(--line)}header .wrap{display:flex;align-items:center;height:64px;gap:16px}
.logo{display:flex;align-items:center;gap:10px;font:800 20px/1 var(--display);text-decoration:none;color:var(--fg);letter-spacing:-.02em}
.logo-mark{width:28px;height:28px;display:grid;grid-template-columns:1fr 1fr;gap:3px}.logo-mark i{border-radius:3px;background:var(--fg)}
.logo-mark i:nth-child(2){background:var(--mark)}.logo-mark i:nth-child(3){background:var(--accent)}
header nav{margin-left:auto;display:flex;gap:18px;font-size:14px}header nav a{color:var(--muted);text-decoration:none}header nav a:hover{color:var(--fg)}
main{padding:48px 0 32px}h1,h2{font-family:var(--display);letter-spacing:-.02em;line-height:1.15;text-wrap:balance}
h1{font-size:clamp(32px,5vw,44px);margin:0 0 8px}h2{font-size:22px;margin:36px 0 8px}p,li{color:var(--fg)}.lede{color:var(--muted);font-size:18px;margin:0 0 8px}
.muted{color:var(--muted)}.card{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:18px 20px;margin:16px 0}
table{width:100%;border-collapse:collapse;margin:12px 0;font-size:15px}td,th{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-weight:600;width:34%}
footer{border-top:1px solid var(--line);padding:24px 0 40px;color:var(--muted);font-size:14px}footer .wrap{display:flex;gap:16px;flex-wrap:wrap;justify-content:space-between}
footer a{color:var(--muted)}
@media (max-width:560px){header nav a.hide-sm{display:none}th{width:auto}td,th{display:block;padding:4px 0}tr{display:block;padding:8px 0;border-bottom:1px solid var(--line)}td,th{border:0}}
"""
FONTS = ("<link rel=preconnect href='https://fonts.googleapis.com'><link rel=stylesheet href='https://fonts.googleapis.com/css2?"
         "family=Bricolage+Grotesque:opsz,wght@12..96,700;12..96,800&family=Inter:wght@400;500;600&display=swap'>")


def contact_line(d: dict) -> str:
    if d["contact_email"]:
        e = html.escape(d["contact_email"])
        return f"<a href='mailto:{e}'>{e}</a>"
    return "a reply to any email from us"


def footer(d: dict) -> str:
    bits = [f"© 2026 {html.escape(d['legal_name'])}"]
    if d["location"]:
        bits.append(html.escape(d["location"]))
    if d["contact_email"]:
        bits.append(contact_line(d))
    return (f"<footer><div class=wrap><span>{' · '.join(bits)}</span><span><a href='/security'>Security and privacy</a> · "
            "<a href='/privacy'>Privacy policy</a> · <a href='/terms'>Terms</a></span></div></footer>")


def public_page(title: str, body: str, d: dict, description: str = "") -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)} · Modulo</title><meta name=description content='{html.escape(description)}'>{FONTS}"
        f"<style>{PAGE_CSS}</style></head><body><header><div class=wrap><a class=logo href='/'><span class=logo-mark aria-hidden=true>"
        "<i></i><i></i><i></i><i></i></span>Modulo</a><nav aria-label=Main><a href='/sample/' class=hide-sm>Sample course</a>"
        "<a href='/security'>Security</a><a href='/#start'>Start a course</a></nav></div></header>"
        f"<main><div class=wrap>{body}</div></main>{footer(d)}</body></html>",
        headers={"cache-control": "no-cache", "referrer-policy": "same-origin"},
    )


async def security_page(_: Request):
    d = get()
    body = f"""<h1>Security and privacy</h1>
<p class=lede>How we look after your course content, in plain words. If your security team has a questionnaire, send it to {contact_line(d)} and we'll fill it in.</p>
<h2>Where your content lives</h2>
<table>
<tr><th>Website, briefs and course files</th><td>Render, a cloud hosting company, in its United States (Oregon) region. Render holds SOC 2 Type II certification. Everything travels over encrypted connections (HTTPS).</td></tr>
<tr><th>The AI that writes with us</th><td>Claude, by Anthropic, used through Anthropic's business service. Under Anthropic's commercial terms, your content is not used to train their AI models. Anthropic holds SOC 2 Type II and ISO 27001 certification.</td></tr>
<tr><th>Email</th><td>Resend sends our emails (tracking links and course updates).</td></tr>
<tr><th>Backups</th><td>A backup of all courses is made every night and the newest copies are kept, so nothing is lost if something goes wrong.</td></tr>
</table>
<h2>Who can see your content</h2>
<p>Only the people on our studio team who work on your course. Team members sign in with their own account and password, and each person only gets the access their role needs. Your tracking page has its own private link, and you can ask us to turn it off or make a new one at any time.</p>
<h2>People check the work</h2>
<p>Our AI agents draft the design, storyboard and course. A person on our team checks and approves the course design before writing starts, and checks the finished course before it is shared with you.</p>
<h2>Deleting your content</h2>
<p>Ask {contact_line(d)} and we delete your course files and brief. Copies in older backups are removed as those backups are replaced.</p>
<h2>Contracts</h2>
<p>We're happy to sign your NDA or data processing agreement before you share any content. Until then, a brief with no confidential details is plenty to get a proposal.</p>
<h2>Certifications</h2>
<p>Modulo is a small studio and does not hold its own SOC 2 or ISO 27001 certification. We rely on providers that do, and we answer security questionnaires openly.</p>
<p class=muted style="margin-top:28px">Read our <a href='/privacy'>privacy policy</a> and <a href='/terms'>terms</a>.</p>"""
    return public_page("Security and privacy", body, d, "How Modulo looks after your course content.")


async def privacy_page(_: Request):
    d = get()
    who = html.escape(d["legal_name"])
    body = f"""<h1>Privacy policy</h1>
<p class=lede>This policy explains what {who} ("we") collects when you use this website and work with us, and what we do with it.</p>
<h2>What we collect</h2>
<ul><li><b>Your brief:</b> what you tell our team in the website chat, and your name and work email when you send the brief.</li>
<li><b>Course material:</b> anything you send us to build your course, and the course we make.</li>
<li><b>Basic technical data:</b> our hosting provider keeps standard server logs (such as IP address and time of visit) to keep the service running and secure.</li></ul>
<p>We don't use advertising trackers on this website.</p>
<h2>Why we use it</h2>
<p>To reply to your brief, prepare a proposal, build and deliver your course, send you progress updates, and keep records our business needs. We don't sell your information.</p>
<h2>Who we share it with</h2>
<p>Only the providers that run our service: Render (hosting), Anthropic (the Claude AI that helps write your course; it does not train on your content under its commercial terms) and Resend (email). They process it on our behalf. See <a href='/security'>Security and privacy</a> for details.</p>
<h2>How long we keep it</h2>
<p>While we work together, and afterwards for as long as you may want changes or our business records require. Ask us at any time and we delete your brief and course files.</p>
<h2>Your rights</h2>
<p>You can ask to see, correct or delete the personal information we hold about you. Contact {contact_line(d)}.</p>
<h2>Changes</h2>
<p>If this policy changes, we update this page.</p>
<p class=muted style="margin-top:28px">{who}{', ' + html.escape(d['location']) if d['location'] else ''}{', company number ' + html.escape(d['company_number']) if d['company_number'] else ''}.</p>"""
    return public_page("Privacy policy", body, d, "What Modulo collects and how it is used.")


async def terms_page(_: Request):
    d = get()
    who = html.escape(d["legal_name"])
    body = f"""<h1>Terms of service</h1>
<p class=lede>The basic terms for using this website and working with {who} ("we"). A signed proposal or contract for your project takes priority over these terms.</p>
<h2>Briefs and proposals</h2>
<p>Sending a brief is free and doesn't commit you to anything. Prices, scope and dates become firm only in a proposal you accept in writing. Figures the team mentions in the website chat are rough guides.</p>
<h2>How we work</h2>
<p>Our AI agents draft the design, storyboard and course, and people on our team review the work. You are responsible for checking that the content is correct for your organisation, especially policies, procedures and safety rules, before you give it to learners.</p>
<h2>Your content</h2>
<p>You keep ownership of the material you send us. You confirm that you have the right to share it with us for your course.</p>
<h2>The finished course</h2>
<p>Once a project is paid for, the finished course is yours to use, change and share inside your organisation, unless your proposal says otherwise. Our course player and tools stay ours, and you may use them as part of the course.</p>
<h2>Confidentiality</h2>
<p>We keep your material confidential and use it only for your project. We're happy to sign your NDA.</p>
<h2>Liability</h2>
<p>We work with care, but the website and sample materials are provided as they are. Our total liability for a project is limited to the amount paid for that project, unless a contract says otherwise.</p>
<h2>Contact</h2>
<p>Questions about these terms: {contact_line(d)}.</p>"""
    return public_page("Terms of service", body, d, "Terms for using the Modulo website and services.")


# ---------------------------------------------------------------- the sample course

async def sample(request: Request):
    """The public sample course: the normal course player, with the course from showcase/sample."""
    from authoring import export as E
    from authoring.routes import PLAYER_FILES
    path = request.path_params.get("path", "")
    headers = {"cache-control": "public, max-age=300", "referrer-policy": "same-origin"}
    course_file = SAMPLE / "course.json"
    if not course_file.is_file():
        return Response("The sample course isn't available right now.", status_code=404)
    if path in ("", "index.html"):
        course = json.loads(course_file.read_text(encoding="utf-8"))
        return HTMLResponse(E.index_html(course), headers=headers)
    if path == "course.js":
        course = json.loads(course_file.read_text(encoding="utf-8"))
        return Response(E.course_js(course, "none"), media_type="application/javascript", headers=headers)
    if path in PLAYER_FILES:
        return Response((E.PLAYER / path).read_bytes(), media_type=PLAYER_FILES[path], headers=headers)
    name = path[len("assets/"):] if path.startswith("assets/") else ""
    if name and re.fullmatch(r"[A-Za-z0-9._-]+", name) and (SAMPLE / "assets" / name).is_file():
        h = {**headers, "x-content-type-options": "nosniff"}
        if name.endswith(".svg"):
            h["content-security-policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
        return FileResponse(SAMPLE / "assets" / name, headers=h)
    return Response("Not found", status_code=404)


async def sample_root(_: Request):
    from starlette.responses import RedirectResponse
    return RedirectResponse("/sample/", 308)


# ---------------------------------------------------------------- Admin form

async def admin_page(request: Request):
    import app as A
    if blocked := A.gate(request, "admin"):
        return blocked
    msg = ""
    d = get()
    if request.method == "POST":
        try:
            d = save(await request.form())
            msg = A.note("good", "Saved. The website shows the new details within a minute.")
        except ValueError as e:
            msg = A.note("bad", html.escape(str(e)))
    rows = []
    for key, label, help_text, kind in FIELDS:
        if kind == "heading":
            rows.append(f"</div><div class=card><h2 style='margin-top:0'>{html.escape(label)}</h2>")
            continue
        v = html.escape(d.get(key, ""))
        hint = f"<p class='muted small' style='margin:2px 0 6px'>{html.escape(help_text)}</p>" if help_text else ""
        field = (f"<textarea id={key} name={key} maxlength=1200 style='min-height:90px'>{v}</textarea>" if kind == "text" else
                 f"<input id={key} name={key} value='{v}' maxlength=200{' type=email' if kind == 'email' else ''}>")
        rows.append(f"<label for={key}>{html.escape(label)}</label>{hint}{field}")
    filled = sum(1 for k in KEYS if d.get(k) and d.get(k) != DEFAULTS.get(k))
    body = (f"<h1>Website details</h1><p class=muted>The details buyers look for before they trust a studio with a brief. "
            f"Empty sections stay hidden on the website, so nothing half-finished shows. {filled} of {len(KEYS)} filled in.</p>{msg}"
            f"<form method=post><div>{''.join(rows)}</div>"
            "<p><button>Save</button> <a class='btn ghost' href='/' target=_blank>Open the website</a></p></form>"
            "<div class=card><h2 style='margin-top:0'>Your public pages</h2><ul>"
            "<li><a href='/sample/' target=_blank>Sample course</a>: the forklift course, tidied for the public.</li>"
            "<li><a href='/security' target=_blank>Security and privacy</a></li>"
            "<li><a href='/privacy' target=_blank>Privacy policy</a> and <a href='/terms' target=_blank>Terms</a>: good starting "
            "points, but have a lawyer check them for your country before you rely on them.</li></ul></div>")
    return A.page("Website details", body, request=request)


ROUTES = [
    Route("/api/site", api_site),
    Route("/security", security_page),
    Route("/privacy", privacy_page),
    Route("/terms", terms_page),
    Route("/sample", sample_root),
    Route("/sample/{path:path}", sample),
    Route("/admin/website", admin_page, methods=["GET", "POST"]),
]
