"""Runs ONE job in a separate, short-lived process:  python -m app.jobrun <job_id> <result.json>

Why: the AI stages (SDXL images, LTX-Video animation, Whisper captions) load several GB of model
weights into system RAM. Freed Python/PyTorch memory is mostly kept by the process for reuse instead of
being returned to the OS, so a long-running API process sat on ~10 GB while idle. A child process that
exits after the job gives every byte back. The parent (app.worker) records the result exactly as before.
"""
from __future__ import annotations

import json
import logging
import sys
import traceback
from pathlib import Path

from . import db, pipeline
from .config import settings


def main(argv: list[str]) -> int:
    logging.basicConfig(level=settings.get("app.log_level", "INFO"), format="%(levelname)s %(name)s: %(message)s")
    job_id, out = int(argv[0]), Path(argv[1])
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job:
        out.write_text(json.dumps({"ok": False, "error": f"job {job_id} not found"}), encoding="utf-8")
        return 1
    try:
        res = {"ok": True, "result": pipeline.run_job(dict(job))}
        code = 0
    except Exception as e:  # noqa: BLE001 - reported to the parent, which marks the job/video failed
        res = {"ok": False, "error": str(e)[:2000], "traceback": traceback.format_exc()[-4000:]}
        code = 1
    tmp = out.with_suffix(".part")
    tmp.write_text(json.dumps(res, default=str), encoding="utf-8")
    tmp.replace(out)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
