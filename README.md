# Modulo on Render

One always-on Render web service that runs the Modulo website, the agent chat, and the course production pipeline, with approval by email.

**Setting it up? Read `SETUP_GUIDE.md`.** This file is for a developer.

## How it fits together

| Path | What it does |
|---|---|
| `render.yaml` | Render Blueprint: Python web service on the Starter plan with a 1 GB disk at `/var/data`. |
| `app.py` | Starlette app: website (`public/`), `/api/chat`, `/api/brief`, `/review/<id>`, `/files/<id>/<name>`, `/dashboard`, `/health`. |
| `agent_chat.py` | `/team` page (Designers and Admins): one-on-one chat with Iris, Theo or Quinn about a course. Iris/Theo return structured change proposals stored under `proposals/`; `/api/team/apply` saves one (backup kept) and rebuilds md + docx. Course context is prompt-cached. |
| `chat.py` | Website chat via the Anthropic SDK (`claude-opus-5-5`, low effort, JSON schema output, server-side refusal fallback). |
| `jobs.py` | One folder per brief under `$DATA_DIR/jobs/` (files) plus one row per course in the database (status, history, costs; `job.json` in the folder is kept as a mirror). A database-backed work queue runs up to `MODULO_PARALLEL_JOBS` courses at once, survives restarts, caps retries after repeated hard interruptions, and emails about failures and steps running over `MODULO_STUCK_HOURS`. Status flow, emails. |
| `db.py` | Postgres when `DATABASE_URL` is set (Render Postgres), otherwise a SQLite file at `$DATA_DIR/modulo.db`. Tables: `jobs`, `users`, `sessions`, `meta`. On start: creates tables, copies logins from the SQLite file into a fresh Postgres, and imports any course folder whose `job.json` isn't in the database yet (non-destructive). |
| `auth.py`, `accounts.py` | Team logins: scrypt password hashes, session cookie (HttpOnly, SameSite=Lax, only its SHA-256 stored), roles `reviewer` < `designer` < `admin`, one-time set-password links, sign-in throttling, same-origin check on POSTs. Pages: `/setup` (first Admin, needs `DASHBOARD_KEY`, works only while there are no users), `/login`, `/logout`, `/welcome/<code>`, `/account`, `/admin/team`, `/admin/system`. |
| `backup.py` | Nightly zip of the database export and every course folder into `$DATA_DIR/backups` (keeps `BACKUP_KEEP`), downloadable by Admins; optional upload to S3-compatible storage. |
| `tracking.py` | The client tracking page at `/track/<code>`: a private, parcel-tracker style page per course with 8 friendly stops (Brief received, Course design, Design approved, Storyboard written, Quality check, Course being built, Final check by our team, Ready), worked out from the course's status, history and files. Updates itself every 30 s via `/track/<code>/status`. Uses its own random `track_token` (separate from the email link code and the play code), so it can't approve, spend or reach internal files. The last stop unlocks only after a Designer/Admin presses **Share with client** on the course page; the course then plays at `/track/<code>/course/` and, if allowed when sharing, SCORM 1.2/2004 zips download at `/track/<code>/download/<kind>`. Studio controls on the course page: copy/email the link, share/stop sharing, turn the link off/on, make a new link. The website chat shows the link after a brief is sent, and emails it when `EMAIL_FROM` is on a verified Resend domain. |
| `emailer.py` | Resend API. Without a key, emails are written to `$DATA_DIR/outbox/`. |
| `authoring/` | The course builder (own SCORM authoring tool). `course.py`: the course format (sections, pages, blocks), checks, and a free rule-based draft from a storyboard. `build.py`: Dev builds `output/course/course.json` from `storyboard.json`, one Claude call per module, resumable. `player/`: the HTML5 player (vanilla JS, responsive, keyboard and screen reader friendly) with a SCORM 1.2 and 2004 runtime wrapper; without an LMS it saves progress in localStorage. `export.py`: SCORM 1.2, SCORM 2004 3rd edition and plain web zips, with a structural self-check run on every export. `editor/editor.html` + `routes.py`: the editor at `/builder/<id>` (Designers and Admins), the shareable player at `/play/<id>/<token>/`. `tests/`: browser tests (see below). |
| `pipeline/` | The agent pipeline (direct Claude API calls with prompt caching): Iris, Theo, Quinn (`make_design()`, `make_storyboard()`), then the optional production pack (`make_pack()`: Sam, Vera, Mila, Dev, Lars, Nia, Paige, one Markdown document each, combined into `production_pack.docx`). Instructions in `pipeline/agents/*.md`. Still runnable by hand: `python pipeline/pipeline.py brief.txt`. |

Status flow: `designing -> awaiting_approval -> (revising -> awaiting_approval) -> storyboarding -> done -> (packing -> done, after the owner presses Start the production pack) -> (building -> done, after the owner presses Have Dev build the course)`, or `failed` with a retry button. On startup the worker re-queues anything left in a working state; the pipeline reuses saved drafts, so a redeploy mid-run loses at most the step in progress.

Approve and change requests are POST forms on the review page (email links only open the page), so mail scanners that prefetch links cannot start a $7 run. Each job has a random token in its email links, which still work without signing in. Signed-in members reach the same page at `/review/<id>` and act by role. Shareable "Try the course" links use a separate `play_token` (older links made with the job token still play), so a shared link can't be used to approve or spend. Everything else needs a login; `DASHBOARD_KEY` only opens `/setup` before the first account exists.

## Environment variables

| Name | Required | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | Chat and pipeline. |
| `RESEND_API_KEY`, `NOTIFY_EMAIL` | yes | Without a verified domain, Resend only delivers to the account owner's address. |
| `DASHBOARD_KEY` | yes | Generated by the Blueprint. Only used to create the first Admin at `/setup`. |
| `DATABASE_URL` | recommended | From the Blueprint's `modulo-db`. Without it, SQLite on the disk. |
| `MODULO_PARALLEL_JOBS` | no | Courses worked on at once (default 1; Blueprint sets 3 for the Standard plan). |
| `MODULO_MAX_ATTEMPTS`, `MODULO_STUCK_HOURS` | no | Stop a step after this many hard interruptions (default 3); email when a step runs longer than this (default 3). |
| `BACKUP_HOUR_UTC`, `BACKUP_KEEP` | no | Nightly backup hour (default 2) and how many zips to keep on disk (default 2). |
| `BACKUP_S3_BUCKET`, `BACKUP_S3_ENDPOINT`, `BACKUP_S3_ACCESS_KEY_ID`, `BACKUP_S3_SECRET_ACCESS_KEY`, `BACKUP_S3_REGION`, `BACKUP_S3_PREFIX`, `BACKUP_S3_KEEP_DAYS` | no | Off-site copy of each backup (e.g. Cloudflare R2: endpoint `https://<account>.r2.cloudflarestorage.com`, region `auto`). |
| `DATA_DIR` | set | `/var/data` (the disk). |
| `EMAIL_FROM` | no | Defaults to `onboarding@resend.dev`; set after verifying a domain. |
| `MODULO_MODEL`, `MODULO_EFFORT` | no | Pipeline model and effort (default `claude-opus-5-5`, `high`). `MODULO_CHAT_MODEL` for the chat. |
| `MODULO_LIGHT_MODEL` | no | Cheaper model for Lars, Nia and Paige (default `claude-sonnet-5-5`; `claude-haiku-5-5` saves more; set it to `claude-opus-5-5` to undo). |
| `MODULO_MAX_ROUNDS` | no | QA fix rounds, default 2. |
| `MAX_BRIEFS_PER_DAY` | no | Default 10. |
| `MODULO_FAKE` | no | `1` = canned forklift answers, no API calls. |
| `PUBLIC_URL` | no | Defaults to Render's `RENDER_EXTERNAL_URL`. Set it when using a custom domain so email links use it. |

## Run locally

```
pip install -r requirements.txt
MODULO_FAKE=1 DASHBOARD_KEY=dev uvicorn app:app --reload
# open http://localhost:8000/setup?key=dev to create the first Admin; emails land in data/outbox/
# add DATABASE_URL=postgresql://... to use Postgres instead of data/modulo.db
```

## Course builder tests

```
authoring/tests/run_tests.sh            # builds the sample as SCORM 1.2 / 2004 / web, plays them in Chromium inside a mock LMS
                                        # (fail, retake, pass, score, interactions, resume, exit, phone layout, keyboard)
MODULO_FAKE=1 DASHBOARD_KEY=dev uvicorn app:app --port 8800   # create an Admin at /setup?key=dev, add the sample course
node authoring/tests/editor_test.js http://localhost:8800 <job id> you@example.com:yourpassword /tmp   # drives the editor end to end
```

Both need Node with Playwright (`NODE_PATH=$(npm root -g)`).

## Drawn pictures

Picture placeholders in a built course are filled with flat illustrations drawn in code (`authoring/illustrate.py`): settings, people, props, icons, flows, checklists, charts and side-by-side comparisons, in the course colors, saved as SVG in the course's `assets/` folder. No image service or extra install is needed, and SVG plays in every browser and LMS.

- Dev's build: Vera (`pipeline/agents/illustrator.md`, on `MODULO_LIGHT_MODEL`) picks a scene for each placeholder from its storyboard note, about a cent per few pictures (`authoring/pictures.py`).
- Free quick draft, test mode, or if Vera can't be reached: a keyword guess draws them instead, at no cost.
- Editor: "Have Vera draw it" on a placeholder, or "Draw the missing pictures" on Check and download. Any drawing can be replaced by an uploaded photo.
- Tests: `python authoring/tests/pictures_test.py`.

## Notes

- Agents call the Claude API directly (no Claude Code subprocess), so each run needs little memory. The Blueprint uses the Standard plan (2 GB) with 3 courses at once; on Starter (512 MB) keep `MODULO_PARALLEL_JOBS=1`.
- Cost control: shared course documents are sent first and cached (`run_agent(shared=...)`), Theo's QA fix rounds return only changed screens, Lars, Nia and Paige run on `MODULO_LIGHT_MODEL` and read a storyboard outline (`PACK_INPUTS` in `pipeline/pipeline.py`). Each step's model, tokens and cost are in the course's `run_log.json`.
- A Render disk attaches to one instance, so the worker runs inside the web service (no separate worker service), and the service can't scale out. That fits a single studio.
- The disk means zero-downtime deploys are off: a deploy restarts the service for about a minute.
