"""Single background worker: runs one job at a time so the 8 GB GPU is never shared
between the LLM, Whisper and NVENC at once. Survives restarts (running jobs are re-queued)."""
from __future__ import annotations

import ctypes
import gc
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import traceback
from pathlib import Path

from . import db, pipeline
from .config import ROOT, settings
from .states import TRANSIENT

log = logging.getLogger("ytm.worker")
MAX_ATTEMPTS = 2


def recover() -> None:
    db.ex("UPDATE jobs SET status='queued' WHERE status='running'")
    for v in db.q(f"SELECT id, resume_status FROM videos WHERE status IN ({','.join('?' * len(TRANSIENT))})", tuple(TRANSIENT)):
        db.update_video(v["id"], status=v["resume_status"] or "idea_approved")


def claim() -> dict | None:
    row = db.one("UPDATE jobs SET status='running', started_at=?, attempts=attempts+1 WHERE id = "
                 "(SELECT id FROM jobs WHERE status='queued' ORDER BY priority DESC, id LIMIT 1) RETURNING *",
                 (db.now_iso(),))
    return row


def finish(job: dict, status: str, result=None, error: str | None = None) -> None:
    db.ex("UPDATE jobs SET status=?, result_json=?, error=?, finished_at=? WHERE id=?",
          (status, db.jdump(result) if result is not None else None, error, db.now_iso(), job["id"]))


class JobFailed(Exception):
    pass


def isolated(kind: str) -> bool:
    """Job kinds that load AI models run in their own process (see app/jobrun.py)."""
    return kind in (settings.get("worker.isolate", ["produce"]) or [])


def run_isolated(job: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="ytm-job-") as tmp:
        out = Path(tmp) / "result.json"
        # stdout/stderr are inherited: the child's log lines go to the same journal as the API's
        p = subprocess.run([sys.executable, "-m", "app.jobrun", str(job["id"]), str(out)], cwd=str(ROOT), env=os.environ.copy())
        try:
            res = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            why = "killed - most likely out of memory" if p.returncode in (-9, 137) else f"exit code {p.returncode}"
            raise JobFailed(f"job process ended without a result ({why})") from None
    if not res.get("ok"):
        if res.get("traceback"):
            log.error("job %s failed in its process:\n%s", job["id"], res["traceback"])
        raise JobFailed(res.get("error") or "job failed")
    return res.get("result")


def release_memory() -> None:
    """Hand freed heap back to the OS (glibc keeps it otherwise)."""
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


class Worker(threading.Thread):
    def __init__(self) -> None:
        super().__init__(name="ytm-worker", daemon=True)
        self._stop_evt = threading.Event()
        self.current: dict | None = None

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        recover()
        log.info("worker started")
        while not self._stop_evt.is_set():
            try:
                job = claim()
            except Exception:  # noqa: BLE001
                log.exception("claim failed")
                self._stop_evt.wait(5)
                continue
            if not job:
                self._stop_evt.wait(3)
                continue
            self.current = job
            log.info("job %s %s video=%s channel=%s", job["id"], job["kind"], job["video_id"], job["channel_id"])
            try:
                result = run_isolated(job) if isolated(job["kind"]) else pipeline.run_job(job)
                finish(job, "done", result)
            except Exception as e:  # noqa: BLE001
                log.error("job %s failed: %s\n%s", job["id"], e, traceback.format_exc())
                finish(job, "failed", error=str(e)[:2000])
                pipeline.on_job_failed(job, e)
            finally:
                self.current = None
                release_memory()


worker = Worker()
