"""Pipeline state machine: approvals, job queue, slot scheduling and stage execution."""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db
from .agents import analytics, animate, captions, media, research, script, seo, thumbnail, upload, video, voice
from .config import settings
from .states import (APPROVED, FAILED, FINAL_REVIEW, IDEA_APPROVED, IDEA_PENDING, PRODUCING, PUBLISHED, REJECTED,
                     REVIEW, SCHEDULED, SCRIPT_APPROVED, SCRIPT_REVIEW, SCRIPTING, UPLOADING, gates_for)
from .util import video_dir

log = logging.getLogger("ytm.pipeline")
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.get("app.timezone", "UTC"))


def _cfg(v: dict) -> dict:
    return db.get_channel(v["channel_id"]).get("config") or {}


# --------------------------------------------------------------------------- queue
def enqueue(kind: str, video_id: int | None = None, channel_id: int | None = None, priority: int = 0) -> int | None:
    active = db.one("SELECT id FROM jobs WHERE kind=? AND status IN ('queued','running') AND "
                    "COALESCE(video_id,0)=COALESCE(?,0) AND COALESCE(channel_id,0)=COALESCE(?,0)",
                    (kind, video_id, channel_id))
    if active:
        return None
    return db.ex("INSERT INTO jobs(kind, video_id, channel_id, priority, created_at) VALUES (?,?,?,?,?)",
                 (kind, video_id, channel_id, priority, db.now_iso()))


def tick() -> dict:
    """Advance everything that is ready. Called by n8n every few minutes and after approvals."""
    out = {"script": 0, "produce": 0, "upload": 0, "deferred_uploads": 0}
    for v in db.q("SELECT id FROM videos WHERE status=?", (IDEA_APPROVED,)):
        out["script"] += bool(enqueue("script", v["id"], priority=2))
    for v in db.q("SELECT id FROM videos WHERE status=?", (SCRIPT_APPROVED,)):
        out["produce"] += bool(enqueue("produce", v["id"], priority=1))
    need = upload.UPLOAD_COST + upload.THUMB_COST + int(settings.get("youtube.quota_reserve", 2000))
    for v in db.q("SELECT id FROM videos WHERE status=? ORDER BY scheduled_at", (APPROVED,)):
        if db.quota_remaining() - out["upload"] * 2100 < need:
            out["deferred_uploads"] += 1
            continue
        out["upload"] += bool(enqueue("upload", v["id"], priority=3))
    return out


def run_research(channel_slug: str | None = None) -> list[int]:
    chans = db.q("SELECT id FROM channels WHERE active=1" + (" AND slug=?" if channel_slug else ""),
                 (channel_slug,) if channel_slug else ())
    return [j for c in chans if (j := enqueue("research", channel_id=c["id"], priority=0))]


# --------------------------------------------------------------------------- scheduling
def _parse_slots(entries: list[str]) -> list[tuple[int | None, int, int]]:
    slots = []
    for e in entries or []:
        parts = str(e).strip().split()
        day = DAYS.index(parts[0][:3].lower()) if len(parts) == 2 else None
        hh, mm = parts[-1].split(":")
        slots.append((day, int(hh), int(mm)))
    return slots


def next_slot(channel_id: int, fmt: str) -> str:
    ch = db.get_channel(channel_id)
    cfg = ch.get("config") or {}
    slots = _parse_slots((cfg.get("schedule") or {}).get(fmt) or ["18:00"])
    max_day = int(cfg.get("max_uploads_per_day", 3))
    tz = _tz()
    earliest = datetime.now(tz) + timedelta(minutes=int(settings.get("app.min_lead_minutes", 45)))
    taken = {r["scheduled_at"] for r in db.q(
        "SELECT scheduled_at FROM videos WHERE channel_id=? AND scheduled_at IS NOT NULL AND status NOT IN (?,?)",
        (channel_id, REJECTED, FAILED))}
    per_day: dict[str, int] = {}
    for t in taken:
        d = datetime.fromisoformat(t).astimezone(tz).date().isoformat()
        per_day[d] = per_day.get(d, 0) + 1
    day0 = earliest.date()
    for offset in range(0, 120):
        day = day0 + timedelta(days=offset)
        for wd, hh, mm in sorted(slots, key=lambda s: (s[1], s[2])):
            if wd is not None and day.weekday() != wd:
                continue
            cand = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
            iso = cand.astimezone(timezone.utc).isoformat()
            if cand < earliest or iso in taken or per_day.get(day.isoformat(), 0) >= max_day:
                continue
            return iso
    raise RuntimeError("no free publishing slot in the next 120 days - check the channel schedule")


# --------------------------------------------------------------------------- approvals
def approve(video_id: int, actor: str = "dashboard", notes: str = "", scheduled_at: str | None = None) -> str:
    v = db.get_video(video_id)
    if not v:
        raise ValueError("video not found")
    st = v["status"]
    if st == IDEA_PENDING:
        new = IDEA_APPROVED
        db.update_video(video_id, status=new)
    elif st == SCRIPT_REVIEW:
        new = SCRIPT_APPROVED
        db.update_video(video_id, status=new, feedback=None)
    elif st == FINAL_REVIEW:
        new = APPROVED
        slot = scheduled_at or v.get("scheduled_at") or next_slot(v["channel_id"], v["format"])
        db.update_video(video_id, status=new, scheduled_at=slot)
    else:
        raise ValueError(f"nothing to approve in status '{st}'")
    db.log_event(video_id, f"approved:{REVIEW[st]}", notes, actor=actor, channel_id=v["channel_id"])
    tick()
    return new


def reject(video_id: int, action: str = "reject", notes: str = "", actor: str = "dashboard") -> str:
    """action: 'reject' (drop it) | 'redo' (regenerate the current stage using `notes` as feedback)."""
    v = db.get_video(video_id)
    if not v:
        raise ValueError("video not found")
    st = v["status"]
    if action == "redo" and st == SCRIPT_REVIEW:
        new = IDEA_APPROVED
        db.update_video(video_id, status=new, feedback=notes or None, script_json=None, script_text=None)
    elif action == "redo" and st == FINAL_REVIEW:
        new = SCRIPT_APPROVED
        clear_production(video_id)
    elif action == "redo" and st == IDEA_PENDING:
        raise ValueError("ideas can only be approved or rejected")
    else:
        if st in (SCHEDULED, PUBLISHED, UPLOADING):
            raise ValueError("already on YouTube - manage it in YouTube Studio")
        new = REJECTED
        db.update_video(video_id, status=new, feedback=notes or None)
    db.log_event(video_id, f"{action}:{st}", notes, actor=actor, channel_id=v["channel_id"])
    tick()
    return new


def clear_production(video_id: int) -> None:
    d = video_dir(video_id)
    for name in ("voice", "segments", "images", "clips", "fonts"):
        shutil.rmtree(d / name, ignore_errors=True)
    for name in ("voice.wav", "plan.json", "animate.json", "concat.txt", "visuals.mp4", "final.mp4", "captions.srt", "captions.ass", "thumbnail.jpg"):
        (d / name).unlink(missing_ok=True)
    db.ex("DELETE FROM assets WHERE video_id=?", (video_id,))
    db.update_video(video_id, status=SCRIPT_APPROVED, voice_path=None, timings_json=None, captions_srt=None,
                    captions_ass=None, video_path=None, thumbnail_path=None, description=None, tags_json=None)


def retry(video_id: int) -> str:
    v = db.get_video(video_id)
    if v["status"] != FAILED:
        raise ValueError("only failed videos can be retried")
    new = v.get("resume_status") or IDEA_APPROVED
    db.update_video(video_id, status=new, error=None)
    db.log_event(video_id, "retry", f"-> {new}", channel_id=v["channel_id"])
    tick()
    return new


# --------------------------------------------------------------------------- execution
def _begin(v: dict, transient: str) -> None:
    db.update_video(v["id"], status=transient, resume_status=v["status"], error=None)


def run_job(job: dict) -> dict:
    kind = job["kind"]
    if kind == "research":
        res = research.run(job["channel_id"])
        tick()
        return res
    if kind == "analytics":
        return analytics.sync()

    v = db.get_video(job["video_id"])
    if not v:
        return {"skipped": "video deleted"}
    gates = gates_for(_cfg(v))

    if kind == "script":
        if v["status"] != IDEA_APPROVED:
            return {"skipped": f"status is {v['status']}"}
        _begin(v, SCRIPTING)
        res = script.run(v["id"])
        db.update_video(v["id"], status=SCRIPT_REVIEW if "script" in gates else SCRIPT_APPROVED)
        if "script" not in gates:
            tick()
        return res

    if kind == "produce":
        if v["status"] != SCRIPT_APPROVED:
            return {"skipped": f"status is {v['status']}"}
        _begin(v, PRODUCING)
        steps = {}
        d = video_dir(v["id"])
        # each step is skipped if its output already exists, so a retry resumes where it failed
        for name, agent, out in (("voice", voice, d / "voice.wav"), ("media", media, d / "plan.json"),
                                 ("animate", animate, d / "animate.json"),
                                 ("captions", captions, d / "captions.srt"), ("video", video, d / "final.mp4"),
                                 ("thumbnail", thumbnail, d / "thumbnail.jpg")):
            steps[name] = "cached" if out.exists() else agent.run(v["id"])
        steps["seo"] = seo.run(v["id"]) if not db.get_video(v["id"]).get("description") else "cached"
        if "final" in gates:
            db.update_video(v["id"], status=FINAL_REVIEW)
        else:
            db.update_video(v["id"], status=APPROVED, scheduled_at=next_slot(v["channel_id"], v["format"]))
            tick()
        return steps

    if kind == "upload":
        if v["status"] != APPROVED:
            return {"skipped": f"status is {v['status']}"}
        _begin(v, UPLOADING)
        try:
            res = upload.run(v["id"])
        except upload.QuotaDeferred as e:
            db.update_video(v["id"], status=APPROVED)
            db.log_event(v["id"], "upload_deferred", str(e), channel_id=v["channel_id"])
            return {"deferred": str(e)}
        db.update_video(v["id"], status=SCHEDULED if res["status"] == "scheduled" else PUBLISHED)
        db.log_event(v["id"], "uploaded", res["url"], channel_id=v["channel_id"])
        return res
    raise ValueError(f"unknown job kind {kind}")


def on_job_failed(job: dict, err: Exception) -> None:
    if job.get("video_id"):
        v = db.get_video(job["video_id"])
        if v:
            db.update_video(v["id"], status=FAILED, error=f"{job['kind']}: {err}"[:2000],
                            resume_status=v.get("resume_status") or v["status"])
            db.log_event(v["id"], "failed", f"{job['kind']}: {err}", channel_id=v["channel_id"])
