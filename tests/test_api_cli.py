"""Every REST endpoint (auth, validation, results) and every CLI command."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT, new_video, run_stage

KEY = "sandbox-key"
H = {"X-API-Key": KEY}


@pytest.fixture
def client(env, monkeypatch):
    from fastapi.testclient import TestClient
    from app import server
    monkeypatch.setenv("YTM_API_KEY", KEY)
    monkeypatch.setattr(server.worker, "start", lambda: None)   # jobs are driven by the tests
    with TestClient(server.app) as c:
        yield c


ENDPOINTS = [("GET", "/api/health"), ("GET", "/api/stats"), ("GET", "/api/channels"), ("POST", "/api/channels/sync"),
             ("GET", "/api/videos"), ("GET", "/api/videos/1"), ("POST", "/api/videos/1/approve"), ("POST", "/api/videos/1/reject"),
             ("POST", "/api/videos/1/retry"), ("PATCH", "/api/videos/1"), ("GET", "/api/videos/1/next-slot"),
             ("GET", "/media/1/final.mp4"), ("POST", "/api/research/run"), ("POST", "/api/pipeline/tick"),
             ("POST", "/api/analytics/sync"), ("GET", "/api/analytics/summary"), ("GET", "/api/analytics/digest"),
             ("GET", "/api/reviews/pending"), ("GET", "/api/jobs"), ("POST", "/api/jobs/1/cancel")]


@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_every_endpoint_requires_key(client, method, path):
    r = client.request(method, path, json={})
    assert r.status_code == 401
    r = client.request(method, path, json={}, headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


def test_dashboard_and_static_are_public(client):
    assert client.get("/").status_code == 200 and "YT AI Manager" in client.get("/").text
    for f in ("app.js", "style.css"):
        assert client.get(f"/static/{f}").status_code == 200
    assert client.get("/docs").status_code == 200


def test_health_and_stats(client):
    h = client.get("/api/health", headers=H).json()
    assert set(h) >= {"ok", "worker_alive", "ollama", "gpu", "ffmpeg", "nvenc", "disk_free_gb", "quota", "failed_videos"}
    assert h["ollama"]["model_present"] is True and h["quota"]["remaining"] == 10000
    s = client.get("/api/stats", headers=H).json()
    assert set(s["videos"]) >= {"idea_pending", "published", "failed"} and s["review_total"] == 0


def test_key_via_query_param_for_media_links(client, env):
    from app.util import video_dir
    vid = new_video(status="final_review")
    (video_dir(vid) / "final.mp4").write_bytes(env.media["clip_city.mp4"].read_bytes())
    assert client.get(f"/media/{vid}/final.mp4?key={KEY}").status_code == 200
    assert client.get(f"/media/{vid}/thumbnail.jpg?key={KEY}").status_code == 404     # not made yet
    assert client.get(f"/media/{vid}/plan.json?key={KEY}").status_code == 404         # not whitelisted
    assert client.get(f"/media/{vid}/..%2F..%2Fytm.db?key={KEY}").status_code == 404   # no path traversal


def test_channels_list_and_sync(client, env):
    rows = client.get("/api/channels", headers=H).json()
    assert [r["slug"] for r in rows] == ["auto-shorts", "tech-simplified"] or len(rows) == 2
    assert all("config_json" not in r and r["token"] is False for r in rows)
    env.set_channel("tech-simplified", name="Renamed")
    assert client.post("/api/channels/sync", headers=H).json() == {"channels": 2}


def test_research_tick_jobs_cancel(client, env):
    r = client.post("/api/research/run", json={"channel": "tech-simplified"}, headers=H).json()
    assert r["count"] == 1
    assert client.post("/api/research/run", json={"channel": "tech-simplified"}, headers=H).json()["count"] == 0  # de-duplicated
    r = client.post("/api/research/run", headers=H).json()
    assert r["count"] == 1   # only auto-shorts was not queued yet
    jobs = client.get("/api/jobs", headers=H).json()
    assert len(jobs) == 2 and jobs[0]["channel_slug"]
    client.post(f"/api/jobs/{jobs[0]['id']}/cancel", headers=H)
    assert client.get("/api/jobs", headers=H).json()[0]["status"] == "cancelled"
    assert set(client.post("/api/pipeline/tick", headers=H).json()) == {"script", "produce", "upload", "deferred_uploads"}


def test_video_list_filters_and_detail(client, env):
    a = new_video(status="idea_pending")
    b = new_video(status="final_review", slug="auto-shorts")
    assert [v["id"] for v in client.get("/api/videos?status=idea_pending", headers=H).json()] == [a]
    assert {v["id"] for v in client.get("/api/videos?status=idea_pending,final_review", headers=H).json()} == {a, b}
    assert [v["id"] for v in client.get("/api/videos?channel=auto-shorts", headers=H).json()] == [b]
    d = client.get(f"/api/videos/{a}", headers=H).json()
    assert d["channel_name"] == "Demo Channel" and "events" in d and "assets" in d and "script_json" not in d
    assert client.get("/api/videos/999", headers=H).status_code == 404


def test_full_review_flow_over_api(client, env):
    """idea -> approve -> script -> edit -> approve -> produce -> edit metadata -> approve -> slot."""
    vid = new_video(status="idea_pending")
    assert client.post(f"/api/videos/{vid}/approve", json={"notes": "good"}, headers=H).json()["status"] == "idea_approved"
    run_stage("script", vid)
    d = client.get(f"/api/videos/{vid}", headers=H).json()
    secs = d["script"]["sections"]
    secs[0]["text"] = "Edited opening line."
    secs.append({"text": "   ", "visual": ""})                               # blank sections are dropped
    r = client.patch(f"/api/videos/{vid}", json={"sections": secs}, headers=H).json()
    assert r["script_text"].startswith("Edited opening line.") and len(r["script"]["sections"]) == len(secs) - 1
    assert client.patch(f"/api/videos/{vid}", json={"sections": [{"text": " "}]}, headers=H).status_code == 400
    assert client.patch(f"/api/videos/{vid}", json={"scheduled_at": "2030-01-01T00:00:00+00:00"}, headers=H).status_code == 400
    assert client.post(f"/api/videos/{vid}/approve", json={}, headers=H).json()["status"] == "script_approved"
    assert client.patch(f"/api/videos/{vid}", json={"sections": secs}, headers=H).status_code == 400   # no longer in review
    run_stage("produce", vid)
    slot = client.get(f"/api/videos/{vid}/next-slot", headers=H).json()["scheduled_at"]
    r = client.patch(f"/api/videos/{vid}", json={"title": "<b>New</b> title", "description": "x" * 6000,
                                                 "tags": [" a ", "", "b"] + [f"t{i}" for i in range(40)], "scheduled_at": slot}, headers=H).json()
    assert r["title"] == "bNew/b title" and len(r["description"]) == 4900 and r["tags"][:2] == ["a", "b"] and len(r["tags"]) == 30
    assert client.post(f"/api/videos/{vid}/approve", json={}, headers=H).json()["status"] == "approved"
    v = client.get(f"/api/videos/{vid}", headers=H).json()
    assert v["scheduled_at"] == slot
    actions = [e["action"] for e in v["events"]]
    assert {"approved:idea", "approved:script", "approved:final", "edited"} <= set(actions)
    assert client.post(f"/api/videos/{vid}/approve", json={}, headers=H).status_code == 400


def test_reject_redo_retry_endpoints(client):
    from app import db
    vid = new_video(status="final_review")
    assert client.post(f"/api/videos/{vid}/reject", json={"action": "redo", "notes": "brighter"}, headers=H).json()["status"] == "script_approved"
    assert client.post(f"/api/videos/{vid}/retry", headers=H).status_code == 400
    db.update_video(vid, status="failed", resume_status="approved", error="x")
    assert client.post(f"/api/videos/{vid}/retry", headers=H).json()["status"] == "approved"
    assert client.post(f"/api/videos/{vid}/reject", json={"action": "reject"}, headers=H).json()["status"] == "rejected"


def test_reviews_pending_notifies_once_per_stage(client):
    from app import db
    a = new_video(status="idea_pending")
    b = new_video(status="final_review")
    r = client.get("/api/reviews/pending", headers=H).json()
    assert r["count"] == 2 and r["by_stage"] == {"idea": 1, "final": 1} and "New items need review" in r["text"]
    assert client.get("/api/reviews/pending", headers=H).json() == {"count": 0, "by_stage": {}, "items": [], "text": ""}
    db.update_video(a, status="script_review")
    assert client.get("/api/reviews/pending?mark=false", headers=H).json()["by_stage"] == {"script": 1}
    assert client.get("/api/reviews/pending", headers=H).json()["count"] == 1     # mark=false didn't consume it


def test_analytics_endpoints(client, monkeypatch):
    from app import db
    vid = new_video(status="published", youtube_video_id="vidX", title="Breach explained")
    db.ex("INSERT INTO analytics_snapshots(video_id, youtube_video_id, views, likes, comments, captured_at) VALUES (?,?,?,?,?,?)",
          (vid, "vidX", 1234, 56, 7, db.now_iso()))
    s = client.get("/api/analytics/summary", headers=H).json()
    assert s["videos"][0]["views"] == 1234
    d = client.get("/api/analytics/digest", headers=H).json()["text"]
    assert "Demo Channel: 1,234 views" in d and "Breach explained (1,234)" in d
    assert client.post("/api/analytics/sync", headers=H).json()["queued_job"]


def test_open_mode_without_key(env, monkeypatch):
    from fastapi.testclient import TestClient
    from app import server
    monkeypatch.delenv("YTM_API_KEY", raising=False)
    monkeypatch.setattr(server.worker, "start", lambda: None)
    with TestClient(server.app) as c:
        assert c.get("/api/stats").status_code == 200


# =========================================================================== CLI
def cli(env, *args, extra_env=None, timeout=300):
    e = {**os.environ, "YTM_CONFIG_DIR": str(env.cfg_dir), **(extra_env or {})}
    return subprocess.run([sys.executable, "-m", "app.cli", *args], cwd=ROOT, env=e, capture_output=True, text=True, timeout=timeout)


def test_cli_init_sync_tick_doctor(env):
    r = cli(env, "init-db")
    assert r.returncode == 0 and "2 channel(s) loaded" in r.stdout
    assert cli(env, "sync-channels").returncode == 0
    r = cli(env, "tick")
    assert json.loads(r.stdout) == {"script": 0, "produce": 0, "upload": 0, "deferred_uploads": 0}
    r = cli(env, "doctor")
    out = r.stdout
    for line in ("ffmpeg", "NVENC encoder", "Ollama", "LLM model", "faster-whisper", "YTM_API_KEY", "YOUTUBE_API_KEY",
                 "Visuals: own clips", "Visuals: AI images", "Visuals: at least one source", "OAuth client_secret.json",
                 "channel token: tech-simplified", "channel token: auto-shorts"):
        assert line in out, line
    assert "[OK] Visuals: own clips (assets/broll)   2 clip(s)" in out or "2 clip(s)" in out


def test_cli_help_and_bad_command(env):
    r = cli(env, "--help")
    assert r.returncode == 0 and "fetch-models" in r.stdout and "auth" in r.stdout
    assert cli(env, "explode").returncode == 2
    assert cli(env, "research", "no-such-channel").returncode == 1


def test_cli_status_against_live_api(env):
    """`status` calls the running API; with none running it must fail cleanly."""
    r = cli(env, "status")
    assert r.returncode == 1 and "API not reachable" in r.stdout


def test_cli_run_stage_failure_marks_video(env):
    vid = new_video(status="script_approved")   # no script -> produce must fail and record it
    r = cli(env, "run", str(vid), "produce")
    assert r.returncode != 0
    from app import db
    assert db.get_video(vid)["status"] == "failed"


def test_cli_fetch_models_reports_download_error(env):
    r = cli(env, "fetch-models", extra_env={"HF_HUB_OFFLINE": "1"})
    assert "Downloading SDXL" in r.stdout


def test_version_is_consistent_everywhere(env):
    """VERSION, app.__version__, the newest CHANGELOG entry and /api/health must agree."""
    import re
    from app import __version__
    from conftest import ROOT
    v = (ROOT / "VERSION").read_text().strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", v) and v == __version__
    top = re.search(r"^## \[(\d+\.\d+\.\d+)\]", (ROOT / "CHANGELOG.md").read_text(), re.M).group(1)
    assert top == v
    r = subprocess.run(["bash", str(ROOT / "ytm.sh"), "version"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == f"YT AI Manager {v}"
