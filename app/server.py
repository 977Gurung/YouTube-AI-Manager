"""FastAPI service: REST API for n8n + the review dashboard. Runs the GPU worker in-process.
Runs as the ytm-api systemd service (scripts/run-api.sh). Manual: ./ytm.sh serve"""
from __future__ import annotations

import hmac
import logging
import shutil
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, db, llm, pipeline, power, sysmon
from .agents import analytics
from .config import ROOT, data_dir, env, logs_dir, settings
from .gpu import nvenc_ok, nvidia_smi
from .states import ALL, REVIEW
from .util import video_dir
from .worker import worker

log = logging.getLogger("ytm")
DASH = Path(__file__).parent / "dashboard"


def _setup_logging() -> None:
    root = logging.getLogger()
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return
    root.setLevel(settings.get("app.log_level", "INFO"))
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = RotatingFileHandler(logs_dir() / "ytm.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _setup_logging()
    db.init_db()
    if not env("YTM_API_KEY"):
        log.warning("YTM_API_KEY is not set - the API is UNPROTECTED. Set it in .env")
    if not worker.is_alive():
        worker.start()
    sysmon.start()
    yield
    sysmon.stop()
    worker.stop()


app = FastAPI(title="YouTube AI Manager", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=DASH), name="static")


def require_key(x_api_key: str | None = Header(default=None), key: str | None = Query(default=None)) -> None:
    expected = env("YTM_API_KEY")
    if not expected:
        return
    supplied = x_api_key or key or ""
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(401, "invalid or missing API key")


Auth = Depends(require_key)


def _video_out(v: dict) -> dict:
    for k in list(v):
        if k.endswith("_json"):
            v.pop(k)
    return v


# --------------------------------------------------------------------------- pages
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(DASH / "index.html", headers={"Cache-Control": "no-cache"})


# --------------------------------------------------------------------------- health & stats
@app.get("/api/health")
def health(_=Auth):
    disk = shutil.disk_usage(data_dir())
    ffmpeg_ok = shutil.which(settings.get("tools.ffmpeg") or "ffmpeg") is not None
    ollama = llm.health()
    gpu = nvidia_smi()
    failed = db.one("SELECT COUNT(*) AS n FROM videos WHERE status='failed'")["n"]
    ok = ollama.get("ok") and ollama.get("model_present") and ffmpeg_ok and worker.is_alive() and disk.free > 5e9
    return {"ok": bool(ok), "version": __version__, "worker_alive": worker.is_alive(),
            "current_job": worker.current, "ollama": ollama, "gpu": gpu, "ffmpeg": ffmpeg_ok,
            "nvenc": nvenc_ok() if ffmpeg_ok else False, "disk_free_gb": round(disk.free / 1e9, 1),
            "quota": {"used": db.quota_used(), "remaining": db.quota_remaining(), "day_pt": db.quota_day()},
            "failed_videos": failed}


@app.get("/api/system")
def system(since: float = 0.0, _=Auth):
    """Live CPU / memory / GPU / network / disk samples for the System tab. `since` = timestamp of the
    newest sample the client already has, so each poll only carries the new ones."""
    if not settings.get("monitor.enabled", True):
        raise HTTPException(404, "system monitor is disabled (monitor.enabled in settings.yaml)")
    m = sysmon.monitor()
    m.start()
    snap = m.snapshot(since)
    job = worker.current
    snap["job"] = {"kind": job.get("kind"), "video_id": job.get("video_id")} if job else None
    return snap


@app.get("/api/system/power")
def power_status(_=Auth):
    st = power.status()
    job = worker.current
    return {**st, "job": {"kind": job.get("kind"), "video_id": job.get("video_id")} if job else None}


class PowerIn(BaseModel):
    action: str
    confirm: str = ""
    force: bool = False


@app.post("/api/system/power")
def power_action(body: PowerIn, _=Auth):
    """Restart the app services, reboot, or shut down. `confirm` must repeat the action (guards
    against accidental calls); a running job needs force=true (it resumes after the restart)."""
    if body.action not in power.COMMANDS:
        raise HTTPException(400, f"unknown action {body.action!r} (use one of {', '.join(power.COMMANDS)})")
    if body.confirm != body.action:
        raise HTTPException(400, "confirm must repeat the action name")
    st = power.status()
    if body.action not in st["actions"]:
        raise HTTPException(403, st["reason"] or f"{body.action} is not allowed on this machine")
    job = worker.current
    if job and not body.force:
        raise HTTPException(409, f"the worker is busy ({job.get('kind')} for video #{job.get('video_id')}); "
                                 "send force=true to go ahead - the job restarts from its last finished step")
    db.log_event(None, f"power_{body.action}", "requested from the dashboard" + (f" while {job.get('kind')} was running" if job else ""),
                 actor="dashboard")
    power.run(body.action)
    return {"ok": True, "action": body.action, "in_seconds": 2}


@app.get("/api/stats")
def stats(_=Auth):
    counts = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) AS n FROM videos GROUP BY status")}
    jobs = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}
    return {"videos": {s: counts.get(s, 0) for s in ALL}, "jobs": jobs,
            "review_total": sum(counts.get(s, 0) for s in REVIEW)}


# --------------------------------------------------------------------------- channels
@app.get("/api/channels")
def channels(_=Auth):
    rows = db.q("SELECT c.*, (SELECT COUNT(*) FROM videos v WHERE v.channel_id=c.id) AS videos, "
                "(SELECT COUNT(*) FROM videos v WHERE v.channel_id=c.id AND v.status IN ('scheduled','published')) AS uploaded "
                "FROM channels c ORDER BY c.active DESC, c.name")
    for r in rows:
        r.pop("config_json", None)
        r["token"] = (data_dir() / "tokens" / f"{r['slug']}.json").exists()
    return rows


@app.post("/api/channels/sync")
def channels_sync(_=Auth):
    settings.reload()
    return {"channels": db.sync_channels()}


# --------------------------------------------------------------------------- videos
@app.get("/api/videos")
def videos(status: str | None = None, channel: str | None = None, limit: int = 200, _=Auth):
    sql = ("SELECT v.*, c.slug AS channel_slug, c.name AS channel_name FROM videos v JOIN channels c ON c.id=v.channel_id WHERE 1=1")
    params: list = []
    if status:
        sts = status.split(",")
        sql += f" AND v.status IN ({','.join('?' * len(sts))})"
        params += sts
    if channel:
        sql += " AND c.slug=?"
        params.append(channel)
    sql += " ORDER BY v.updated_at DESC LIMIT ?"
    params.append(min(limit, 1000))
    return [_video_out(v) for v in db.q(sql, params)]


@app.get("/api/videos/{vid}")
def video_detail(vid: int, _=Auth):
    v = db.get_video(vid)
    if not v:
        raise HTTPException(404)
    out = _video_out(v)
    out["events"] = db.q("SELECT * FROM events WHERE video_id=? ORDER BY id DESC LIMIT 50", (vid,))
    out["assets"] = db.q("SELECT kind, provider, author, source_url, license FROM assets WHERE video_id=?", (vid,))
    out["stats"] = db.one("SELECT * FROM v_latest_stats WHERE video_id=?", (vid,))
    return out


class ApproveBody(BaseModel):
    notes: str = ""
    scheduled_at: str | None = None


class RejectBody(BaseModel):
    action: str = "reject"  # reject | redo
    notes: str = ""


class PatchBody(BaseModel):
    title: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    scheduled_at: str | None = None
    sections: list[dict] | None = None


@app.post("/api/videos/{vid}/approve")
def approve(vid: int, body: ApproveBody, _=Auth):
    try:
        return {"status": pipeline.approve(vid, notes=body.notes, scheduled_at=body.scheduled_at)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/videos/{vid}/reject")
def reject(vid: int, body: RejectBody, _=Auth):
    try:
        return {"status": pipeline.reject(vid, action=body.action, notes=body.notes)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/videos/{vid}/retry")
def retry(vid: int, _=Auth):
    try:
        return {"status": pipeline.retry(vid)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.patch("/api/videos/{vid}")
def patch(vid: int, body: PatchBody, _=Auth):
    v = db.get_video(vid)
    if not v:
        raise HTTPException(404)
    upd: dict = {}
    if body.sections is not None:
        if v["status"] != "script_review":
            raise HTTPException(400, "script can only be edited during script review")
        script = v["script"] or {}
        clean = [{"text": str(s.get("text", "")).strip(), "visual": str(s.get("visual", "")).strip() or "abstract background",
                  "chapter": s.get("chapter")} for s in body.sections if str(s.get("text", "")).strip()]
        if not clean:
            raise HTTPException(400, "script needs at least one section")
        script["sections"] = clean
        upd.update(script=script, script_text="\n\n".join(s["text"] for s in clean))
    if body.title is not None:
        upd["title"] = body.title.replace("<", "").replace(">", "")[:100]
    if body.description is not None:
        upd["description"] = body.description.replace("<", "").replace(">", "")[:4900]
    if body.tags is not None:
        upd["tags"] = [t.strip() for t in body.tags if t.strip()][:30]
    if body.scheduled_at is not None:
        if v["status"] not in ("final_review", "approved"):
            raise HTTPException(400, "schedule can be changed only before upload")
        upd["scheduled_at"] = body.scheduled_at or None
    if upd:
        db.update_video(vid, **upd)
        db.log_event(vid, "edited", ", ".join(upd), actor="dashboard", channel_id=v["channel_id"])
    return _video_out(db.get_video(vid))


@app.get("/api/videos/{vid}/next-slot")
def next_slot(vid: int, _=Auth):
    v = db.get_video(vid)
    return {"scheduled_at": pipeline.next_slot(v["channel_id"], v["format"])}


@app.get("/media/{vid}/{name}")
def media(vid: int, name: str, _=Auth):
    if name not in ("final.mp4", "thumbnail.jpg", "voice.wav", "captions.srt"):
        raise HTTPException(404)
    p = video_dir(vid) / name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


# --------------------------------------------------------------------------- automation endpoints (n8n)
class ResearchBody(BaseModel):
    channel: str | None = None


@app.post("/api/research/run")
def research_run(body: ResearchBody | None = None, _=Auth):
    jobs = pipeline.run_research(body.channel if body else None)
    return {"queued_jobs": jobs, "count": len(jobs)}


@app.post("/api/pipeline/tick")
def tick(_=Auth):
    return pipeline.tick()


@app.post("/api/analytics/sync")
def analytics_sync(_=Auth):
    job = pipeline.enqueue("analytics", priority=-1)
    return {"queued_job": job}


@app.get("/api/analytics/summary")
def analytics_summary(_=Auth):
    return analytics.summary()


@app.get("/api/analytics/digest")
def analytics_digest(_=Auth):
    s = analytics.summary()
    lines = [f"• {c['name']}: {c['views']:,} views across {c['videos']} tracked videos" for c in s["channels"]]
    top = s["videos"][:3]
    if top:
        lines.append("Top videos: " + "; ".join(f"{t['title']} ({t['views']:,})" for t in top))
    st = stats()
    lines.append(f"Waiting for review: {st['review_total']} · failed: {st['videos'].get('failed', 0)}")
    return {"text": "📊 YouTube AI Manager daily digest\n" + "\n".join(lines)}


@app.get("/api/reviews/pending")
def reviews_pending(mark: bool = True, _=Auth):
    """Items that entered a review state since the last notification (for n8n alerts)."""
    rows = db.q("SELECT v.id, v.status, v.topic, v.title, v.format, c.name AS channel FROM videos v "
                "JOIN channels c ON c.id=v.channel_id WHERE v.status IN ('idea_pending','script_review','final_review') "
                "AND (v.notified_status IS NULL OR v.notified_status != v.status) ORDER BY v.id")
    if mark:
        for r in rows:
            db.ex("UPDATE videos SET notified_status=? WHERE id=?", (r["status"], r["id"]))
    by_stage: dict[str, int] = {}
    for r in rows:
        by_stage[REVIEW[r["status"]]] = by_stage.get(REVIEW[r["status"]], 0) + 1
    base = f"http://{settings.get('app.host', '127.0.0.1')}:{settings.get('app.port', 8765)}"
    text = ("🎬 New items need review: " + ", ".join(f"{n} {k}" for k, n in by_stage.items()) + f"\nOpen {base}/") if rows else ""
    return {"count": len(rows), "by_stage": by_stage, "items": rows, "text": text}


@app.get("/api/jobs")
def jobs(limit: int = 100, _=Auth):
    return db.q("SELECT j.*, c.slug AS channel_slug FROM jobs j LEFT JOIN channels c ON c.id=j.channel_id "
                "ORDER BY j.id DESC LIMIT ?", (min(limit, 500),))


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: int, _=Auth):
    db.ex("UPDATE jobs SET status='cancelled', finished_at=? WHERE id=? AND status='queued'", (db.now_iso(), jid))
    return {"ok": True}


def main() -> None:
    import uvicorn

    _setup_logging()
    uvicorn.run(app, host=settings.get("app.host", "127.0.0.1"), port=int(settings.get("app.port", 8765)),
                log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
