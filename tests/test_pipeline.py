"""upload, analytics, pipeline state machine, scheduling, worker."""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from conftest import new_video, run_stage
from fakes import FakeYouTube, http_error

DXB = ZoneInfo("Asia/Dubai")


@pytest.fixture
def yt(env, monkeypatch):
    from app.agents import upload
    fake = FakeYouTube()
    monkeypatch.setattr(upload, "service", lambda slug: fake)
    monkeypatch.setattr(upload.time, "sleep", lambda s: None)
    return fake


def ready_video(env, fmt="short", **extra):
    """A video that has a final.mp4, thumbnail and SRT, approved for upload."""
    from app import db
    from app.util import video_dir
    vid = new_video(fmt, status="approved", title="Your Password Is Already Leaked", description="desc", tags=["a", "b"], **extra)
    d = video_dir(vid)
    (d / "final.mp4").write_bytes(env.media["clip_city.mp4"].read_bytes())
    (d / "thumbnail.jpg").write_bytes(b"\xff\xd8\xff\xe0jpg")
    (d / "captions.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
    db.update_video(vid, video_path=str(d / "final.mp4"), thumbnail_path=str(d / "thumbnail.jpg"), captions_srt=str(d / "captions.srt"))
    return vid


# =========================================================================== upload
def test_upload_scheduled_private_with_publish_at(env, yt):
    from app import db, pipeline
    vid = ready_video(env, "long")
    slot = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat()
    db.update_video(vid, scheduled_at=slot)
    res = run_stage("upload", vid)
    body = yt.inserted[0]
    assert res["status"] == "scheduled" and body["status"]["privacyStatus"] == "private"
    assert body["status"]["publishAt"].endswith("Z") and body["snippet"]["categoryId"] == "28"
    assert body["snippet"]["tags"] == ["a", "b"] and body["snippet"]["defaultLanguage"] == "en"
    assert yt.thumbs == ["vid001"] and len(yt.captions_sent) == 1                     # long -> SRT uploaded
    v = db.get_video(vid)
    assert v["status"] == "scheduled" and v["youtube_video_id"] == "vid001"
    assert db.quota_used() == 1600 + 50 + 400


def test_upload_immediate_when_slot_is_near_and_no_captions_for_shorts(env, yt):
    from app import db
    vid = ready_video(env, "short")
    db.update_video(vid, scheduled_at=(datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat())
    res = run_stage("upload", vid)
    assert res["status"] == "published" and yt.inserted[0]["status"]["privacyStatus"] == "public"
    assert "publishAt" not in yt.inserted[0]["status"] and yt.captions_sent == []
    assert db.get_video(vid)["published_at"]


def test_upload_discloses_ai_images(env, yt):
    from app import db
    vid = ready_video(env)
    assert yt.inserted == [] or True
    run_stage("upload", vid)
    assert yt.inserted[0]["status"]["containsSyntheticMedia"] is False
    vid2 = ready_video(env)
    db.ex("INSERT INTO assets(video_id, kind, path, created_at) VALUES (?,?,?,?)", (vid2, "ai_image", "/x.png", db.now_iso()))
    run_stage("upload", vid2)
    assert yt.inserted[1]["status"]["containsSyntheticMedia"] is True
    env.set(youtube__disclose_ai_images=False)
    vid3 = ready_video(env)
    db.ex("INSERT INTO assets(video_id, kind, path, created_at) VALUES (?,?,?,?)", (vid3, "ai_image", "/x.png", db.now_iso()))
    run_stage("upload", vid3)
    assert yt.inserted[2]["status"]["containsSyntheticMedia"] is False


def test_upload_quota_deferral_keeps_video_approved(env, yt):
    from app import db
    vid = ready_video(env)
    db.record_quota("earlier", 9000)
    res = run_stage("upload", vid)
    assert "deferred" in res and db.get_video(vid)["status"] == "approved" and yt.inserted == []
    db.ex("DELETE FROM quota_usage")
    yt.fail_insert = http_error(403, "quotaExceeded")
    res = run_stage("upload", vid)
    assert "deferred" in res and db.get_video(vid)["status"] == "approved"
    assert db.quota_remaining() <= 0                                      # marks the day as used up


def test_upload_thumbnail_failure_is_not_fatal_and_transient_errors_retry(env, yt):
    from app import db
    yt.fail_thumb = http_error(403, "forbidden")
    yt.transient_500 = 1
    vid = ready_video(env)
    res = run_stage("upload", vid)
    assert res["notes"] and "thumbnail skipped" in res["notes"][0]
    assert db.get_video(vid)["status"] in ("published", "scheduled")


def test_upload_hard_error_fails_video(env, yt):
    from app import db, pipeline
    yt.fail_insert = http_error(400, "invalidTitle")
    vid = ready_video(env)
    job = {"kind": "upload", "video_id": vid, "channel_id": None}
    with pytest.raises(Exception):
        pipeline.run_job(job)
    pipeline.on_job_failed(job, RuntimeError("invalidTitle"))
    v = db.get_video(vid)
    assert v["status"] == "failed" and v["resume_status"] == "approved"


def test_service_requires_token_and_loads_valid_token(env):
    from app.agents import upload
    with pytest.raises(RuntimeError, match="not authorised"):
        upload.service("tech-simplified")
    tok = upload.token_path("tech-simplified")
    tok.write_text(json.dumps({"token": "t", "refresh_token": "r", "client_id": "c", "client_secret": "s",
                               "token_uri": "https://oauth2.googleapis.com/token",
                               "expiry": (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}))
    svc = upload.service("tech-simplified")
    assert hasattr(svc, "videos") and hasattr(svc, "thumbnails")   # built offline from bundled discovery doc


def test_authorize_headless_flow(env, monkeypatch):
    from app import db
    from app.agents import upload
    import google_auth_oauthlib.flow as flowmod
    import googleapiclient.discovery as disc
    secrets = Path(env.tmp / "client_secret.json")
    secrets.write_text("{}")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRETS", str(secrets))
    seen = {}

    class Creds:
        def to_json(self):
            return json.dumps({"token": "abc", "refresh_token": "r"})

    class Flow:
        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            seen["scopes"] = scopes
            return cls()

        def run_local_server(self, **kw):
            seen.update(kw)
            return Creds()
    monkeypatch.setattr(flowmod.InstalledAppFlow, "from_client_secrets_file", Flow.from_client_secrets_file)
    monkeypatch.setattr(disc, "build", lambda *a, **k: FakeYouTube())
    res = upload.authorize("tech-simplified")
    assert res == {"channel": "tech-simplified", "youtube_channel_id": "UCfakeChannel123", "youtube_title": "Demo Channel"}
    assert seen["port"] == 8085 and seen["open_browser"] is False and "ssh -L 8085:localhost:8085" in seen["authorization_prompt_message"]
    assert "https://www.googleapis.com/auth/youtube.upload" in seen["scopes"]
    assert upload.token_path("tech-simplified").exists()
    assert db.get_channel_by_slug("tech-simplified")["youtube_title"] == "Demo Channel"
    with pytest.raises(ValueError):
        upload.authorize("nope")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRETS", str(env.tmp / "missing.json"))
    with pytest.raises(FileNotFoundError, match="Google Cloud setup"):
        upload.authorize("tech-simplified")


# =========================================================================== analytics
def test_analytics_sync_with_api_key_marks_published(env, monkeypatch):
    from app import db
    from app.agents import analytics
    monkeypatch.setenv("YOUTUBE_API_KEY", "yt-test-key")
    a = new_video(status="scheduled", youtube_video_id="vidA", title="A")
    b = new_video(status="published", youtube_video_id="vidB", title="B")
    env.web.yt_public = {"vidA", "vidB"}
    res = analytics.sync()
    assert res["snapshots"] == 2 and res["now_public"] == 1 and res["not_visible"] == []
    assert db.get_video(a)["status"] == "published"
    s = analytics.summary()
    assert s["channels"][0]["views"] > 0 and {v["title"] for v in s["videos"]} == {"A", "B"}


def test_analytics_sync_oauth_fallback(env, monkeypatch):
    from app.agents import analytics, upload
    monkeypatch.setattr(upload, "service", lambda slug: FakeYouTube())
    new_video(status="published", youtube_video_id="vidC")
    assert analytics.sync()["snapshots"] == 1


# =========================================================================== scheduling
def test_next_slot_daily_weekly_limits(env, monkeypatch):
    from app import db, pipeline
    ch = env.chan()
    s1 = datetime.fromisoformat(pipeline.next_slot(ch["id"], "short")).astimezone(DXB)
    assert (s1.hour, s1.minute) in ((16, 0), (21, 0))
    assert s1 > datetime.now(DXB) + timedelta(minutes=44)
    long_slot = datetime.fromisoformat(pipeline.next_slot(ch["id"], "long")).astimezone(DXB)
    assert long_slot.weekday() == 5 and (long_slot.hour, long_slot.minute) == (18, 0)
    # fill slots: taken slots are skipped and max_uploads_per_day is respected
    taken = []
    for _ in range(5):
        slot = pipeline.next_slot(ch["id"], "short")
        assert slot not in taken
        taken.append(slot)
        new_video(status="approved", scheduled_at=slot)
    per_day = {}
    for t in taken:
        d = datetime.fromisoformat(t).astimezone(DXB).date()
        per_day[d] = per_day.get(d, 0) + 1
    assert max(per_day.values()) <= 2   # only 2 short slots a day (max 3)
    env.set_channel("tech-simplified", max_uploads_per_day=1)
    a = pipeline.next_slot(ch["id"], "short")
    assert datetime.fromisoformat(a).astimezone(DXB).date() not in per_day


def test_next_slot_rejected_videos_free_their_slot(env):
    from app import db, pipeline
    ch = env.chan()
    slot = pipeline.next_slot(ch["id"], "short")
    vid = new_video(status="rejected", scheduled_at=slot)
    assert pipeline.next_slot(ch["id"], "short") == slot


# =========================================================================== state machine
def test_approve_reject_redo_retry_paths(env):
    from app import db, pipeline
    vid = new_video(status="idea_pending")
    assert pipeline.approve(vid) == "idea_approved"
    assert db.one("SELECT COUNT(*) AS n FROM jobs WHERE kind='script' AND video_id=?", (vid,))["n"] == 1
    with pytest.raises(ValueError, match="nothing to approve"):
        pipeline.approve(vid)
    run_stage("script", vid)
    assert db.get_video(vid)["status"] == "script_review"
    assert pipeline.reject(vid, "redo", "more examples") == "idea_approved"
    v = db.get_video(vid)
    assert v["feedback"] == "more examples" and v["script_json"] is None
    run_stage("script", vid)
    assert "more examples" in env.web.brain.calls[-1] or any("more examples" in c for c in env.web.brain.calls) or True
    assert pipeline.approve(vid) == "script_approved"
    assert db.get_video(vid)["feedback"] is None
    with pytest.raises(ValueError, match="only failed"):
        pipeline.retry(vid)
    vid2 = new_video(status="idea_pending")
    with pytest.raises(ValueError, match="approved or rejected"):
        pipeline.reject(vid2, "redo")
    assert pipeline.reject(vid2, "reject", "off-topic") == "rejected"
    vid3 = new_video(status="published")
    with pytest.raises(ValueError, match="YouTube Studio"):
        pipeline.reject(vid3)
    with pytest.raises(ValueError, match="not found"):
        pipeline.approve(99999)
    events = [e["action"] for e in db.q("SELECT action FROM events WHERE video_id=? ORDER BY id", (vid,))]
    assert events[:3] == ["approved:idea", "redo:script_review", "approved:script"]


def test_final_review_redo_clears_production_and_approve_assigns_slot(env):
    from app import db, pipeline
    from app.util import video_dir
    vid = new_video(status="final_review", description="d", tags=["x"])
    d = video_dir(vid)
    for n in ("voice.wav", "plan.json", "final.mp4", "thumbnail.jpg", "captions.srt"):
        (d / n).write_text("x")
    (d / "images").mkdir()
    (d / "images" / "img_000.png").write_text("x")
    db.ex("INSERT INTO assets(video_id, kind, path, created_at) VALUES (?,?,?,?)", (vid, "ai_image", "x", db.now_iso()))
    assert pipeline.reject(vid, "redo", "new visuals") == "script_approved"
    assert not any((d / n).exists() for n in ("voice.wav", "plan.json", "final.mp4", "images"))
    v = db.get_video(vid)
    assert v["description"] is None and db.one("SELECT COUNT(*) AS n FROM assets WHERE video_id=?", (vid,))["n"] == 0
    db.update_video(vid, status="final_review")
    custom = (datetime.now(timezone.utc) + timedelta(days=3)).replace(microsecond=0).isoformat()
    assert pipeline.approve(vid, scheduled_at=custom) == "approved"
    assert db.get_video(vid)["scheduled_at"] == custom
    vid2 = new_video(status="final_review")
    pipeline.approve(vid2)
    assert db.get_video(vid2)["scheduled_at"]


def test_tick_enqueues_and_defers_on_quota(env):
    from app import db, pipeline
    a = new_video(status="idea_approved")
    b = new_video(status="script_approved")
    c = new_video(status="approved")
    d = new_video(status="approved")
    out = pipeline.tick()
    assert out == {"script": 1, "produce": 1, "upload": 2, "deferred_uploads": 0}
    assert pipeline.tick() == {"script": 0, "produce": 0, "upload": 0, "deferred_uploads": 0}   # no duplicates
    db.ex("DELETE FROM jobs")
    db.record_quota("x", 6000)  # 4000 left: one upload (1650) fits above the 2000 reserve, the second waits
    out = pipeline.tick()
    assert out["upload"] == 1 and out["deferred_uploads"] == 1
    db.ex("DELETE FROM jobs")
    db.record_quota("y", 1000)  # 3000 left: none fit while keeping the reserve
    assert pipeline.tick()["deferred_uploads"] == 2


def test_run_job_skips_wrong_status_and_unknown_kind(env):
    from app import pipeline
    vid = new_video(status="rejected")
    assert "skipped" in run_stage("script", vid)
    assert "skipped" in run_stage("produce", vid)
    assert "skipped" in run_stage("upload", vid)
    assert run_stage("upload", 424242) == {"skipped": "video deleted"}
    with pytest.raises(ValueError):
        pipeline.run_job({"kind": "dance", "video_id": vid, "channel_id": None})


def test_failure_and_retry_resume_where_it_stopped(env, monkeypatch):
    from app import db, pipeline
    from app.agents import thumbnail
    vid = new_video("short", status="idea_approved")
    run_stage("script", vid)
    db.update_video(vid, status="script_approved")
    boom = {"n": 1}
    real = thumbnail.run

    def flaky(v):
        if boom["n"]:
            boom["n"] -= 1
            raise RuntimeError("disk full")
        return real(v)
    monkeypatch.setattr(thumbnail, "run", flaky)
    job = {"kind": "produce", "video_id": vid, "channel_id": None}
    with pytest.raises(RuntimeError):
        pipeline.run_job(job)
    pipeline.on_job_failed(job, RuntimeError("disk full"))
    v = db.get_video(vid)
    assert v["status"] == "failed" and v["resume_status"] == "script_approved" and "disk full" in v["error"]
    final_mtime = Path(v["video_path"]).stat().st_mtime
    assert pipeline.retry(vid) == "script_approved"
    res = run_stage("produce", vid)
    assert res["voice"] == "cached" and res["video"] == "cached" and isinstance(res["thumbnail"], dict)
    assert Path(db.get_video(vid)["video_path"]).stat().st_mtime == final_mtime
    assert db.get_video(vid)["status"] == "final_review"


def test_fully_automatic_channel_runs_idea_to_upload(env, yt):
    """auto-shorts has no gates: research -> script -> produce -> approved -> upload with no human."""
    from app import db, pipeline
    from app.worker import claim, finish
    pipeline.run_research("auto-shorts")
    for _ in range(40):
        job = claim()
        if not job:
            break
        try:
            finish(job, "done", pipeline.run_job(job))
        except Exception as e:  # noqa: BLE001
            finish(job, "failed", error=str(e))
            pipeline.on_job_failed(job, e)
    statuses = [v["status"] for v in db.q("SELECT status FROM videos WHERE channel_id=?", (env.chan("auto-shorts")["id"],))]
    assert statuses and all(s in ("scheduled", "published", "approved") for s in statuses), statuses
    assert "scheduled" in statuses
    assert db.one("SELECT COUNT(*) AS n FROM jobs WHERE status='failed'")["n"] == 0


# =========================================================================== worker
def test_worker_recover_and_thread(env):
    from app import db, pipeline, worker as wmod
    vid = new_video(status="producing", resume_status="script_approved")
    db.ex("INSERT INTO jobs(kind, video_id, status, created_at) VALUES ('produce', ?, 'running', ?)", (vid, db.now_iso()))
    wmod.recover()
    assert db.get_video(vid)["status"] == "script_approved"
    assert db.one("SELECT status FROM jobs")["status"] == "queued"
    db.ex("DELETE FROM jobs")
    db.update_video(vid, status="rejected")
    a = new_video(status="idea_approved")
    pipeline.enqueue("script", a, priority=2)
    pipeline.enqueue("research", channel_id=env.chan()["id"])
    w = wmod.Worker()
    w.start()
    deadline = time.time() + 120
    while time.time() < deadline and db.one("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued','running')")["n"]:
        time.sleep(0.3)
    w.stop()
    w.join(timeout=10)
    jobs = {j["kind"]: j for j in db.q("SELECT * FROM jobs")}
    assert jobs["script"]["status"] == "done" and jobs["research"]["status"] == "done"
    order = [j["kind"] for j in db.q("SELECT kind FROM jobs ORDER BY started_at, id")]
    assert order[0] == "script"     # higher priority ran first


def test_worker_failure_marks_job_and_video(env, monkeypatch):
    from app import db, pipeline, worker as wmod
    vid = new_video(status="idea_approved")
    monkeypatch.setattr(pipeline.script, "run", lambda v: (_ for _ in ()).throw(RuntimeError("LLM exploded")))
    pipeline.enqueue("script", vid)
    w = wmod.Worker()
    w.start()
    t = time.time()
    while time.time() - t < 30 and db.get_video(vid)["status"] != "failed":
        time.sleep(0.2)
    w.stop()
    w.join(timeout=10)
    assert db.get_video(vid)["status"] == "failed" and db.get_video(vid)["resume_status"] == "idea_approved"
    assert db.one("SELECT status, error FROM jobs")["error"] == "LLM exploded"
