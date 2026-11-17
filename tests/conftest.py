"""Sandbox fixtures: every test gets its own config dir, data dir and database.
External services are faked (tests/fakes.py); nothing reaches the real internet."""
from __future__ import annotations

import copy
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SANDBOX = ROOT / "tests" / ".sandbox"
SANDBOX.mkdir(exist_ok=True)
os.environ["YTM_CONFIG_DIR"] = str(SANDBOX / "boot-config")
(SANDBOX / "boot-config").mkdir(exist_ok=True)
for k in ("YTM_API_KEY", "YOUTUBE_API_KEY", "PEXELS_API_KEY", "PIXABAY_API_KEY", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "DISPLAY"):
    os.environ.pop(k, None)
os.environ["GOOGLE_CLIENT_SECRETS"] = str(SANDBOX / "client_secret.json")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fakes import FakeWeb, install_requests_redirect, make_media  # noqa: E402

SHIPPED_SETTINGS = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text())

# Your real channel (as configured on the server), with a sandbox-safe voice, plus a second
# fully automatic Shorts-only channel to exercise "no approval gates".
CHANNELS = {"channels": [
    {"slug": "tech-simplified", "name": "Demo Channel",
     "niche": "AI, technology and information security news and explainers from around the world",
     "audience": "tech-curious professionals, IT staff and students worldwide", "language": "en",
     "tone": "clear, credible and practical. Security topics stay defensive.",
     "formats": ["short", "long"], "mix": {"short": 3, "long": 1},
     "voice": {"provider": "test_tone"},
     "visual_style": "modern tech editorial photography, dark moody lighting",
     "brand": {"primary": "#00B4D8", "text": "#FFFFFF"},
     "schedule": {"short": ["16:00", "21:00"], "long": ["Sat 18:00"]},
     "max_uploads_per_day": 3, "youtube": {"category_id": "28"},
     "approval_gates": ["idea", "script", "final"],
     "research": {"trends_geo": "US", "region": "US", "youtube_category": "28",
                  "keywords": ["AI news", "cybersecurity news"],
                  "subreddits": ["technology", "cybersecurity"],
                  "rss": ["https://feeds.feedburner.com/TheHackersNews", "https://www.bleepingcomputer.com/feed/",
                          "https://krebsonsecurity.com/feed/", "https://www.cisa.gov/cybersecurity-advisories/all.xml",
                          "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
                          "https://techcrunch.com/category/artificial-intelligence/feed/"],
                  "competitors": ["UCcompetitorAAA"], "fallback_visual": "cybersecurity technology"},
     "description_footer": "AI, technology and information security, explained.\nSubscribe for weekly updates.\n"},
    {"slug": "auto-shorts", "name": "Auto Shorts", "niche": "quick AI tips", "language": "en",
     "formats": ["short"], "voice": {"provider": "test_tone"}, "approval_gates": [],
     "visuals": ["ai"], "schedule": {"short": ["09:00"]}, "max_uploads_per_day": 1,
     "research": {"google_trends": False, "rss": ["https://feeds.local/ai.xml"]}},
]}


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    return make_media(SANDBOX / "media")


@pytest.fixture(scope="session")
def tiny_sdxl():
    out = SANDBOX / "tinysdxl"
    if not (out / "model_index.json").exists():
        subprocess.run([sys.executable, str(ROOT / "tests" / "tools" / "make_tiny_sdxl.py"), str(out)],
                       check=True, capture_output=True)
    return out


@pytest.fixture(scope="session")
def tiny_ltx():
    out = SANDBOX / "tinyltx"
    if not (out / "model_index.json").exists():
        subprocess.run([sys.executable, str(ROOT / "tests" / "tools" / "make_tiny_ltx.py"), str(out)],
                       check=True, capture_output=True)
    return out


@pytest.fixture(scope="session")
def web(media):
    fw = FakeWeb(media)
    yield fw
    fw.stop()


def make_settings(tmp: Path, tiny: Path, music: Path | None, broll: Path, tiny_video: Path | None = None) -> dict:
    s = copy.deepcopy(SHIPPED_SETTINGS)
    s["app"]["data_dir"] = str(tmp / "data")
    s["app"]["timezone"] = "Asia/Dubai"          # tests run the scheduler in a non-UTC zone on purpose
    s["llm"]["base_url"] = "http://ollama.local:11434"
    s["video"]["music_dir"] = str(music) if music else str(tmp / "no-music")
    s["media"]["broll_dir"] = str(broll)
    s["ai_images"].update(model=str(tiny), vae=None, lightning_repo=None, lightning_file=None, steps=2,
                          short_size=[64, 112], long_size=[112, 64], cache_dir=str(tmp / "hf"))
    # tiny random LTX-Video (base-folder code path); small + few frames so CPU tests stay fast
    s["ai_video"].update(enabled=bool(tiny_video), model=str(tiny_video or "missing"), state_file=str(tmp / "ai_video.json"),
                         base_steps=2, steps=2, short_size=[64, 96], long_size=[96, 64], frames=9,
                         max_clips={"short": 3, "long": 3}, self_test=False)
    return s


@pytest.fixture
def env(tmp_path, monkeypatch, web, media, tiny_sdxl, tiny_ltx):
    """A fresh installation: config, data dir, DB, outside world faked."""
    from app import config, db, gpu

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    music = tmp_path / "music"
    music.mkdir()
    (music / "track.mp3").write_bytes(media["music.mp3"].read_bytes())
    broll = tmp_path / "broll"
    (broll / "city").mkdir(parents=True)
    (broll / "city" / "traffic-night-rain.mp4").write_bytes(media["clip_city.mp4"].read_bytes())
    (broll / "server-room-lights.mp4").write_bytes(media["clip_server.mp4"].read_bytes())
    settings_dict = make_settings(tmp_path, tiny_sdxl, music, broll, tiny_ltx)
    (cfg_dir / "settings.yaml").write_text(yaml.safe_dump(settings_dict, sort_keys=False))
    (cfg_dir / "channels.yaml").write_text(yaml.safe_dump(CHANNELS, sort_keys=False))
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    config.settings.reload()
    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
    db._local.conn = None
    gpu.nvenc_ok.cache_clear()
    undo = install_requests_redirect(web.port)
    web.brain.__init__()
    web.hooks.clear()
    web.pexels_status = 200
    web.feed_fail = set()
    for k in ("YTM_API_KEY", "YOUTUBE_API_KEY", "PEXELS_API_KEY", "PIXABAY_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    db.init_db()

    class Env:
        pass
    e = Env()
    e.tmp, e.cfg_dir, e.web, e.media, e.broll, e.music = tmp_path, cfg_dir, web, media, broll, music
    e.settings = config.settings

    def set_settings(**dotted):
        d = yaml.safe_load((cfg_dir / "settings.yaml").read_text())
        for k, v in dotted.items():
            cur = d
            parts = k.split("__")
            for p in parts[:-1]:
                cur = cur.setdefault(p, {})
            cur[parts[-1]] = v
        (cfg_dir / "settings.yaml").write_text(yaml.safe_dump(d, sort_keys=False))
        config.settings.reload()
    e.set = set_settings

    def set_channel(slug, **fields):
        d = yaml.safe_load((cfg_dir / "channels.yaml").read_text())
        for ch in d["channels"]:
            if ch["slug"] == slug:
                ch.update(fields)
        (cfg_dir / "channels.yaml").write_text(yaml.safe_dump(d, sort_keys=False))
        db.sync_channels()
    e.set_channel = set_channel
    e.chan = lambda slug="tech-simplified": db.get_channel_by_slug(slug)
    yield e
    undo()
    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
    db._local.conn = None


def new_video(fmt="short", status="idea_approved", slug="tech-simplified", **extra) -> int:
    from app import db
    ch = db.get_channel_by_slug(slug)
    vid = db.ex("INSERT INTO videos(channel_id, format, status, topic, angle, hook, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (ch["id"], fmt, status, extra.pop("topic", "How ransomware gangs pick their victims"), "defender's view",
                 "They don't pick you at random.", db.now_iso(), db.now_iso()))
    if extra:
        db.update_video(vid, **extra)
    return vid


def run_stage(kind, vid=None, channel_id=None):
    from app import pipeline
    return pipeline.run_job({"kind": kind, "video_id": vid, "channel_id": channel_id})
