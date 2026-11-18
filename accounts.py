"""Pages for signing in, the one-time owner setup, team members, your account, and the System page.

  /setup            create the first Admin (owner) account; needs the old dashboard key, works only once
  /login, /logout   sign in and out
  /welcome/<code>   a new member sets their password (also used for "forgot password" links an Admin makes)
  /account          change your own password
  /admin/team       Admins: add members, change roles, switch someone off, make set-password links
  /admin/system     Admins: database, work queue, backups (download, make one now), email and key checks
"""

from __future__ import annotations

import asyncio
import html
import time

from starlette.requests import Request
from starlette.responses import FileResponse, RedirectResponse
from starlette.routing import Route

import auth
import backup
import db
import jobs


def _A():
    import app
    return app


def _ip(request: Request) -> str:
    return (request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "?"))


def _form_page(title: str, body: str, request: Request, status: int = 200):
    r = _A().page(title, f"<div style='max-width:460px'>{body}</div>", request=request)
    r.status_code = status
    return r


def _when(t: float | None) -> str:
    return time.strftime("%d %b %Y %H:%M UTC", time.gmtime(t)) if t else "never"


# ---------------------------------------------------------------- first owner account

async def setup(request: Request):
    A = _A()
    if auth.has_users():
        return RedirectResponse("/login", 303)
    key_ok = A.key_ok(request)
    if not key_ok:
        return _form_page("Set up Modulo", "<h1>Set up your owner account</h1>"
                          "<p>To create the first account, open the dashboard link from your email "
                          "<b>“Your Modulo service is live”</b> (or the newer email <b>“Create your Modulo login”</b>). "
                          "It contains the private key that proves you own this service.</p>", request, 403)
    k = html.escape(request.query_params.get("key", ""))
    error = ""
    if request.method == "POST":
        if not auth.same_origin(request):
            return A.forbidden(request, "This form came from another website.")
        form = await request.form()
        name, email, pw, pw2 = (str(form.get(x) or "").strip() for x in ("name", "email", "password", "password2"))
        if pw != pw2:
            error = "The two passwords are different."
        else:
            try:
                user, _ = await asyncio.to_thread(auth.create_user, email, name, "admin", pw)
            except ValueError as e:
                error = str(e)
            else:
                code = await asyncio.to_thread(auth.start_session, user["id"])
                r = RedirectResponse("/dashboard?welcome=1", 303)
                auth.set_cookie(r, request, code)
                return r
    return _form_page("Set up Modulo", f"""<h1>Set up your owner account</h1>
<p>Modulo now has its own logins, so you and your team each sign in with an email and password instead of sharing one secret link.
This account is the <b>Admin</b>: you can add your team afterwards.</p>
<p class=err>{html.escape(error)}</p>
<form method=post action="/setup?key={k}">
<label for=name>Your name</label><input id=name name=name required autocomplete=name>
<label for=email>Your email</label><input id=email name=email type=email required autocomplete=username value="{html.escape(jobs.emailer.notify_address())}">
<label for=password>Choose a password</label><input id=password name=password type=password required minlength={auth.MIN_PASSWORD} autocomplete=new-password>
<p class=muted style="margin:4px 0 0">At least {auth.MIN_PASSWORD} characters. A short sentence you'll remember works well.</p>
<label for=password2>Type it again</label><input id=password2 name=password2 type=password required autocomplete=new-password>
<p><button>Create my account</button></p></form>""", request)


# ---------------------------------------------------------------- sign in and out

async def login(request: Request):
    if not auth.has_users():
        return RedirectResponse("/setup", 303)
    nxt = auth.safe_next(request.query_params.get("next"))
    error = ""
    email = ""
    if request.method == "POST":
        form = await request.form()
        email = str(form.get("email") or "").strip().lower()
        pw = str(form.get("password") or "")
        nxt = auth.safe_next(str(form.get("next") or nxt))
        ip = _ip(request)
        if auth.too_many_attempts(ip) or auth.too_many_attempts(email):
            error = "Too many tries. Please wait 15 minutes and try again."
        else:
            u = await asyncio.to_thread(auth.by_email, email)
            ok = await asyncio.to_thread(auth.check_password, pw, u["pw_hash"] if u else auth.hash_password("x" * 12))
            if u and ok and u["active"]:
                code = await asyncio.to_thread(auth.start_session, u["id"])
                r = RedirectResponse(nxt, 303)
                auth.set_cookie(r, request, code)
                return r
            auth.record_failure(ip)
            auth.record_failure(email)
            error = ("This account has been switched off. Ask an Admin." if u and ok else
                     "That email and password don't match. Check them and try again.")
    note = ""
    if request.query_params.get("key"):
        note = ("<div class=card>Modulo now uses personal logins instead of the old secret dashboard link. "
                "Sign in with your email and password.</div>")
    return _form_page("Sign in", f"""<h1>Sign in to Modulo</h1>{note}
<p class=err>{html.escape(error)}</p>
<form method=post action="/login"><input type=hidden name=next value="{html.escape(nxt)}">
<label for=email>Email</label><input id=email name=email type=email required autocomplete=username value="{html.escape(email)}">
<label for=password>Password</label><input id=password name=password type=password required autocomplete=current-password>
<p><button>Sign in</button></p></form>
<p class=muted>Forgot your password? Ask an Admin for a new set-password link.</p>""", request, 401 if error else 200)


async def logout(request: Request):
    await asyncio.to_thread(auth.end_session, request.cookies.get(auth.COOKIE, ""))
    r = RedirectResponse("/login", 303)
    auth.clear_cookie(r)
    return r


async def welcome(request: Request):
    code = request.path_params["code"]
    u = await asyncio.to_thread(auth.user_for_invite, code)
    if not u:
        return _form_page("Link expired", "<h1>This link has expired or was already used</h1>"
                          "<p>Ask an Admin for a new set-password link. Links work for 7 days and only once.</p>", request, 404)
    error = ""
    if request.method == "POST":
        form = await request.form()
        pw, pw2 = str(form.get("password") or ""), str(form.get("password2") or "")
        if pw != pw2:
            error = "The two passwords are different."
        else:
            try:
                await asyncio.to_thread(auth.set_password, u["id"], pw)
            except ValueError as e:
                error = str(e)
            else:
                session = await asyncio.to_thread(auth.start_session, u["id"])
                r = RedirectResponse("/dashboard", 303)
                auth.set_cookie(r, request, session)
                return r
    return _form_page("Set your password", f"""<h1>Welcome, {html.escape(u['name'])}</h1>
<p>You've been added to the Modulo team as <b>{auth.ROLE_NAMES[u['role']]}</b>. {html.escape(auth.ROLE_HELP[u['role']])}</p>
<p>Choose a password. You'll sign in with <b>{html.escape(u['email'])}</b>.</p><p class=err>{html.escape(error)}</p>
<form method=post><label for=password>Password</label><input id=password name=password type=password required minlength={auth.MIN_PASSWORD} autocomplete=new-password>
<p class=muted style="margin:4px 0 0">At least {auth.MIN_PASSWORD} characters.</p>
<label for=password2>Type it again</label><input id=password2 name=password2 type=password required autocomplete=new-password>
<p><button>Save and sign in</button></p></form>""", request)


async def account(request: Request):
    A = _A()
    if blocked := A.gate(request, "reviewer"):
        return blocked
    u = auth.current_user(request)
    msg, error = "", ""
    if request.method == "POST":
        form = await request.form()
        old, pw, pw2 = (str(form.get(x) or "") for x in ("old", "password", "password2"))
        if not auth.check_password(old, u["pw_hash"]):
            error = "Your current password is not right."
        elif pw != pw2:
            error = "The two new passwords are different."
        else:
            try:
                await asyncio.to_thread(auth.set_password, u["id"], pw)
            except ValueError as e:
                error = str(e)
            else:
                session = await asyncio.to_thread(auth.start_session, u["id"])
                r = RedirectResponse("/account?saved=1", 303)
                auth.set_cookie(r, request, session)
                return r
    if request.query_params.get("saved"):
        msg = "Password changed. You've been signed out on your other devices."
    return _form_page("Your account", f"""<h1>Your account</h1>
<p>{html.escape(u['name'])} · {html.escape(u['email'])} · <b>{auth.ROLE_NAMES[u['role']]}</b></p>
<p class=muted>{html.escape(auth.ROLE_HELP[u['role']])}</p>
<h2>Change your password</h2><p class=ok>{html.escape(msg)}</p><p class=err>{html.escape(error)}</p>
<form method=post><label for=old>Current password</label><input id=old name=old type=password required autocomplete=current-password>
<label for=password>New password</label><input id=password name=password type=password required minlength={auth.MIN_PASSWORD} autocomplete=new-password>
<label for=password2>Type it again</label><input id=password2 name=password2 type=password required autocomplete=new-password>
<p><button>Change password</button></p></form>""", request)


# ---------------------------------------------------------------- team members (Admins)

def _invite_box(u: dict, code: str, request: Request) -> str:
    link = f"{jobs.public_url()}/welcome/{code}"
    return (f"<div class=card style='border-color:var(--accent)'><p style='margin-top:0'><b>Send this link to {html.escape(u['name'])}</b> "
            f"({html.escape(u['email'])}). It lets them choose their password, works once, and expires in {auth.INVITE_DAYS} days. "
            "It is shown only now, so copy it before leaving this page.</p>"
            f"<p><input readonly value='{html.escape(link)}' onclick='this.select()' aria-label='Set-password link'></p></div>")


async def team_admin(request: Request):
    A = _A()
    if blocked := A.gate(request, "admin"):
        return blocked
    me = auth.current_user(request)
    msg, error = "", ""
    if request.method == "POST":
        form = await request.form()
        action = str(form.get("action") or "")
        try:
            if action == "add":
                u, code = await asyncio.to_thread(auth.create_user, str(form.get("email") or ""), str(form.get("name") or ""),
                                                  str(form.get("role") or ""))
                msg = _invite_box(u, code, request)
            elif action == "update":
                uid = str(form.get("user") or "")
                await asyncio.to_thread(auth.update_user, uid, str(form.get("role") or ""), str(form.get("active") or "1") == "1")
                msg = "<p class=ok>Saved.</p>"
            elif action == "link":
                uid = str(form.get("user") or "")
                u = auth.get_user(uid)
                if not u:
                    raise ValueError("That team member was not found.")
                code = await asyncio.to_thread(auth.new_invite, uid)
                msg = _invite_box(u, code, request)
        except ValueError as e:
            error = str(e)
    rows = []
    for u in auth.users():
        opts = "".join(f"<option value={r}{' selected' if u['role'] == r else ''}>{auth.ROLE_NAMES[r]}</option>" for r in auth.ROLES)
        state = ("" if u["active"] else " <span class=err>(switched off)</span>")
        pending = "" if u["has_password"] else " <span class=muted>(hasn't set a password yet)</span>"
        you = " <span class=muted>(you)</span>" if u["id"] == me["id"] else ""
        rows.append(f"""<tr><td><b>{html.escape(u['name'])}</b>{you}{state}{pending}<div class=muted>{html.escape(u['email'])}</div>
<div class=muted style="font-size:12px">Last sign-in: {_when(u['last_login'])}</div></td>
<td><form method=post class=row style="gap:6px"><input type=hidden name=action value=update><input type=hidden name=user value="{u['id']}">
<select name=role aria-label="Role">{opts}</select>
<select name=active aria-label="Access"><option value=1{' selected' if u['active'] else ''}>Can sign in</option><option value=0{'' if u['active'] else ' selected'}>Switched off</option></select>
<button class=ghost>Save</button></form>
<form method=post style="margin-top:6px"><input type=hidden name=action value=link><input type=hidden name=user value="{u['id']}">
<button class=link>Make a new set-password link</button></form></td></tr>""")
    roles = "".join(f"<li><b>{auth.ROLE_NAMES[r]}</b>: {html.escape(auth.ROLE_HELP[r])}</li>" for r in auth.ROLES)
    opts = "".join(f"<option value={r}{' selected' if r == 'designer' else ''}>{auth.ROLE_NAMES[r]}</option>" for r in auth.ROLES)
    body = f"""<h1>Team members</h1><p class=muted>Everyone who can sign in to Modulo, and what they're allowed to do.</p>
<ul>{roles}</ul>{msg}<p class=err>{html.escape(error)}</p>
<div class=card><table><tr><th>Person</th><th>Role and access</th></tr>{''.join(rows)}</table></div>
<div class=card><h2 style="margin-top:0">Add someone</h2>
<p class=muted>You'll get a link to send them (by email, chat, however you like). They open it and choose their own password.</p>
<form method=post><input type=hidden name=action value=add>
<label for=n>Name</label><input id=n name=name required>
<label for=e>Email</label><input id=e name=email type=email required>
<label for=r>Role</label><select id=r name=role>{opts}</select>
<p><button>Add and make their link</button></p></form></div>
<p class=muted>Someone forgot their password? Press <b>Make a new set-password link</b> next to their name and send it to them.
Someone left? Set them to <b>Switched off</b>: they're signed out at once, and their name stays in the course history.</p>"""
    return A.page("Team members", body, request=request)


# ---------------------------------------------------------------- system (Admins)

async def system(request: Request):
    A = _A()
    if blocked := A.gate(request, "admin"):
        return blocked
    msg = ""
    if request.method == "POST":
        result = await asyncio.to_thread(backup.run_backup)
        msg = ("<p class=ok>Backup made.</p>" if result["ok"] else
               f"<p class=err>The backup did not work: {html.escape(result['error'])}</p>")
    db_ok = await asyncio.to_thread(db.ping)
    q = jobs.queue_state()
    last = backup.last_result()
    files = backup.list_backups()
    if db.is_postgres():
        db_line = "<b>Postgres</b> (Render). Render backs it up by itself and can restore it to any moment of the last few days."
    else:
        db_line = ("<b>A file on the disk</b> (no separate database yet). It works, and it's included in the nightly backup, "
                   "but adding the Render Postgres database is recommended (see the setup steps).")
    backups = "".join(
        f"<tr><td>{html.escape(p.name)}</td><td class=muted>{p.stat().st_size / 1_000_000:.1f} MB</td>"
        f"<td><a href='/admin/backups/{html.escape(p.name)}'>Download</a></td></tr>" for p in files
    ) or "<tr><td colspan=3 class=muted>No backup yet. The first one is made tonight, or press the button below.</td></tr>"
    if last:
        last_line = (f"Last backup: {_when(last['at'])}, " + ("worked." if last["ok"] else
                     f"<span class=err>did not work: {html.escape(last.get('error', ''))}</span>"))
    else:
        last_line = "No backup has run yet."
    offsite = (f"Off-site copy: <b>on</b> (bucket {html.escape(backup.os.environ.get('BACKUP_S3_BUCKET', ''))})."
               if backup.offsite_configured() else
               "Off-site copy: <b>off</b>. Optional: copy each backup to storage outside Render, such as Cloudflare R2 (free up to 10 GB).")
    running = "".join(f"<li>{html.escape(r['id'])} (try {r['attempts']}, since {_when(r['claimed_at'])})</li>" for r in q["running"]) or "<li class=muted>Nothing</li>"
    waiting = "".join(f"<li>{html.escape(r['id'])}</li>" for r in q["waiting"]) or "<li class=muted>Nothing</li>"
    body = f"""<h1>System</h1><p class=muted>How the service is doing. Everything here looks after itself; this page is for checking.</p>
<div class=card><h2 style="margin-top:0">Database</h2><p>{'<span class=ok>Working.</span>' if db_ok else '<span class=err>Not reachable right now.</span>'} {db_line}</p></div>
<div class=card><h2 style="margin-top:0">Work queue</h2><p>Up to <b>{q['parallel']}</b> courses are worked on at the same time.
If the service restarts, work picks up again from the last finished step. If a step fails, or runs for more than {jobs.STUCK_HOURS:g} hours, you get an email.</p>
<p><b>Running</b></p><ul>{running}</ul><p><b>Waiting</b></p><ul>{waiting}</ul></div>
<div class=card><h2 style="margin-top:0">Backups</h2>
<p>Every night a backup of all courses, team members and settings is made. {last_line}</p>
<p class=muted>On top of this, Render keeps a daily snapshot of the disk for 7 days{', and can restore the Postgres database to any moment of the last few days' if db.is_postgres() else ''}.</p>
<p>{offsite}</p>{msg}
<table><tr><th>Backup file</th><th>Size</th><th></th></tr>{backups}</table>
<form method=post style="margin-top:12px"><button class=ghost>Make a backup now</button></form>
<p class=muted>Tip: download a backup now and then and keep it on your own computer or drive. It's a zip you can open: every course folder is inside.</p></div>"""
    return A.page("System", body, request=request)


async def backup_download(request: Request):
    A = _A()
    if blocked := A.gate(request, "admin"):
        return blocked
    name = request.path_params["name"]
    match = next((p for p in backup.list_backups() if p.name == name), None)
    if not match:
        return A.not_found()
    return FileResponse(match, filename=name, media_type="application/zip")


ROUTES = [
    Route("/setup", setup, methods=["GET", "POST"]),
    Route("/login", login, methods=["GET", "POST"]),
    Route("/logout", logout, methods=["POST"]),
    Route("/welcome/{code}", welcome, methods=["GET", "POST"]),
    Route("/account", account, methods=["GET", "POST"]),
    Route("/admin/team", team_admin, methods=["GET", "POST"]),
    Route("/admin/system", system, methods=["GET", "POST"]),
    Route("/admin/backups/{name}", backup_download),
]
