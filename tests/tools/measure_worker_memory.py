"""Acts like the API server's worker for ONE produce job, then reports the parent's memory.
Used by tests/test_memory_power.py:  python measure_worker_memory.py <video_id> <isolate: 0|1>"""
import json
import sys
import time

from app import db, pipeline, worker as wmod
from app.config import settings

vid, iso = int(sys.argv[1]), sys.argv[2] == "1"
settings.raw.setdefault("worker", {})["isolate"] = ["produce"] if iso else []
pipeline.enqueue("produce", vid)
w = wmod.Worker()
w.start()
t0 = time.time()
while time.time() - t0 < 900 and db.one("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued','running')")["n"]:
    time.sleep(0.3)
w.stop()
w.join(timeout=10)
rss = int(next(l for l in open("/proc/self/status") if l.startswith("VmRSS")).split()[1]) // 1024
job = db.one("SELECT status, error FROM jobs WHERE video_id=? AND kind='produce'", (vid,))
print(json.dumps({"rss_mb": rss, "torch_loaded": "torch" in sys.modules, "diffusers_loaded": "diffusers" in sys.modules,
                  "job": job["status"], "error": job["error"], "video": db.get_video(vid)["status"]}))
