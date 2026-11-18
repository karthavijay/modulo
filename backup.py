"""Nightly backups.

Every night (around BACKUP_HOUR_UTC, default 2 AM UTC) the service makes one zip file with:
  database.json   every course record, team member and setting (sign-in sessions are left out)
  jobs/...        every course folder: briefs, designs, storyboards, Word files, built courses, pictures
  README.txt      what is inside

The newest BACKUP_KEEP zips (default 2) stay on the disk, and an Admin can download them from the
System page. If BACKUP_S3_BUCKET is set, each zip is also copied to S3-compatible storage outside Render
(for example Cloudflare R2), and copies older than BACKUP_S3_KEEP_DAYS (default 30) are removed there.

This is on top of what Render does by itself: a snapshot of the disk every 24 hours, kept 7 days, and
point-in-time recovery for paid Postgres databases.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import zipfile
from pathlib import Path

import db

log = logging.getLogger("modulo.backup")
DATA_DIR = db.DATA_DIR
BACKUP_DIR = DATA_DIR / "backups"
KEEP = max(1, int(os.environ.get("BACKUP_KEEP", "2")))
HOUR = int(os.environ.get("BACKUP_HOUR_UTC", "2"))
S3_KEEP_DAYS = int(os.environ.get("BACKUP_S3_KEEP_DAYS", "30"))

README = """Modulo backup, made {when} UTC.

database.json  Every course record (status, history, costs), team member and setting.
               Passwords are stored as one-way hashes, never as plain text.
jobs/          One folder per course, exactly as on the server: the brief, Iris's design,
               Theo's storyboard drafts, Quinn's QA rounds, the Word files, the production pack,
               and the built course with its pictures.

To put a course back, a developer copies its folder into /var/data/jobs on the server and
restarts the service: courses found on the disk are added to the database automatically.
"""


def offsite_configured() -> bool:
    return bool(os.environ.get("BACKUP_S3_BUCKET"))


def _s3():
    import boto3  # only needed when off-site backups are switched on
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("BACKUP_S3_ENDPOINT") or None,
        aws_access_key_id=os.environ.get("BACKUP_S3_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("BACKUP_S3_SECRET_ACCESS_KEY"),
        region_name=os.environ.get("BACKUP_S3_REGION", "auto"),
    )


def list_backups() -> list[Path]:
    if not BACKUP_DIR.exists():
        return []
    return sorted(BACKUP_DIR.glob("modulo-backup-*.zip"), reverse=True)


def _folder_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0


def make_backup() -> Path:
    """Makes one backup zip and returns its path. Raises with a plain-language reason if it can't."""
    jobs_dir = DATA_DIR / "jobs"
    need = _folder_size(jobs_dir) + 5_000_000
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(BACKUP_DIR).free
    if free < need * 1.2:
        raise RuntimeError(f"Not enough free space on the disk for a backup (needs about {need // 1_000_000 + 1} MB, "
                           f"{free // 1_000_000} MB free). Make the disk bigger in Render (your service, Disks).")
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    final = BACKUP_DIR / f"modulo-backup-{stamp}.zip"
    tmp = final.with_suffix(".zip.tmp")
    data = db.export_all()
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("README.txt", README.format(when=time.strftime("%Y-%m-%d %H:%M", time.gmtime())))
        z.writestr("database.json", json.dumps(data, indent=1, ensure_ascii=False, default=str))
        if jobs_dir.exists():
            for p in sorted(jobs_dir.rglob("*")):
                if p.is_file() and not p.name.endswith(".tmp"):
                    z.write(p, "jobs/" + p.relative_to(jobs_dir).as_posix())
    with zipfile.ZipFile(tmp) as z:  # check it reads back before keeping it
        bad = z.testzip()
        if bad:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"The backup file came out damaged ({bad}).")
    tmp.replace(final)
    for old in list_backups()[KEEP:]:
        old.unlink(missing_ok=True)
    log.info("Backup made: %s (%d KB, %d courses)", final.name, final.stat().st_size // 1024, len(data["jobs"]))
    return final


def upload_offsite(path: Path) -> str:
    bucket = os.environ["BACKUP_S3_BUCKET"]
    prefix = os.environ.get("BACKUP_S3_PREFIX", "modulo/").lstrip("/")
    s3 = _s3()
    key = prefix + path.name
    s3.upload_file(str(path), bucket, key)
    cutoff = time.time() - S3_KEEP_DAYS * 86400
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix + "modulo-backup-")
    for obj in resp.get("Contents", []):
        if obj["LastModified"].timestamp() < cutoff and obj["Key"] != key:
            s3.delete_object(Bucket=bucket, Key=obj["Key"])
    log.info("Backup copied off-site to %s/%s", bucket, key)
    return key


def run_backup() -> dict:
    """Makes a backup and copies it off-site when that is set up. Records the result for the System page."""
    started = time.time()
    result = {"at": started, "ok": False, "file": "", "offsite": "", "error": ""}
    try:
        path = make_backup()
        result.update(ok=True, file=path.name, size=path.stat().st_size)
        if offsite_configured():
            try:
                result["offsite"] = upload_offsite(path)
            except Exception as e:  # noqa: BLE001  the local backup still counts
                result["ok"] = False
                result["error"] = f"The backup was made on the disk, but copying it off-site failed: {e}"
    except Exception as e:  # noqa: BLE001
        result["error"] = str(e)[:600]
        log.exception("Backup failed")
    db.set_meta("last_backup", json.dumps(result))
    return result


def last_result() -> dict | None:
    raw = db.get_meta("last_backup")
    return json.loads(raw) if raw else None


def due(now: float | None = None) -> bool:
    now = now or time.time()
    last = last_result()
    if not last:
        return True
    age = now - last["at"]
    if not last.get("ok") and age > 3 * 3600:  # after a failure, try again a few hours later
        return True
    return (age > 20 * 3600 and time.gmtime(now).tm_hour == HOUR) or age > 26 * 3600


async def nightly(on_failure) -> None:
    """Runs forever inside the web server. on_failure(result) is called (in a thread) when a backup fails."""
    await asyncio.sleep(60)  # let the service finish starting
    while True:
        try:
            if await asyncio.to_thread(due):
                result = await asyncio.to_thread(run_backup)
                if not result["ok"]:
                    await asyncio.to_thread(on_failure, result)
        except Exception:  # noqa: BLE001
            log.exception("Backup scheduler problem")
        await asyncio.sleep(600)
