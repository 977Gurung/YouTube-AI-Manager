"""config, db, util, llm, gpu, states."""
from __future__ import annotations

import json
import wave
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import requests


# --------------------------------------------------------------------------- config
def test_settings_dotted_get_and_defaults(env):
    s = env.settings
    assert s.get("llm.model") == "qwen2.5:7b-instruct"
    assert s.get("video.short.width") == 1080
    assert s.get("nope.missing", 7) == 7
    assert s.get("llm.model.deeper", "x") == "x"  # walking into a string returns default


def test_resolve_path_and_dirs(env):
    from app.config import ROOT, data_dir, logs_dir, resolve_path
    assert resolve_path("config") == ROOT / "config"
    assert resolve_path("/abs/x").as_posix() == "/abs/x"
    assert data_dir().is_dir() and str(data_dir()).startswith(str(env.tmp))
    assert logs_dir().is_dir()


def test_load_channels_validates(env):
    from app import config
    chans = config.load_channels()
    assert [c["slug"] for c in chans] == ["tech-simplified", "auto-shorts"]
    (env.cfg_dir / "channels.yaml").write_text("channels:\n  - name: no slug\n")
    with pytest.raises(ValueError, match="slug"):
        config.load_channels()


def test_env_helper(env, monkeypatch):
    from app.config import env as getenv
    monkeypatch.setenv("YTM_X", "")
    assert getenv("YTM_X", "d") == "d"
    monkeypatch.setenv("YTM_X", "v")
    assert getenv("YTM_X") == "v"


# --------------------------------------------------------------------------- db
def test_init_and_sync_channels(env):
    from app import db
    rows = db.q("SELECT slug, name, active, config_json FROM channels ORDER BY id")
    assert [(r["slug"], r["active"]) for r in rows] == [("tech-simplified", 1), ("auto-shorts", 1)]
    assert rows[0]["config"]["brand"]["primary"] == "#00B4D8"
    # removing a channel from yaml deactivates it, re-adding reactivates, renaming updates
    import yaml
    d = yaml.safe_load((env.cfg_dir / "channels.yaml").read_text())
    keep = d["channels"][0]
    keep["name"] = "Demo Channel (renamed)"
    (env.cfg_dir / "channels.yaml").write_text(yaml.safe_dump({"channels": [keep]}))
    assert db.sync_channels() == 1
    assert db.get_channel_by_slug("auto-shorts")["active"] == 0
    assert db.get_channel_by_slug("tech-simplified")["name"] == "Demo Channel (renamed)"
    # schema is idempotent
    db.init_db()
    assert db.one("SELECT COUNT(*) AS n FROM channels")["n"] == 2


def test_update_video_aliases_and_json(env):
    from app import db
    from conftest import new_video
    vid = new_video()
    db.update_video(vid, script={"sections": [{"text": "a"}]}, tags=["x", "y"], timings=[{"start": 0}], title="T")
    v = db.get_video(vid)
    assert v["script"]["sections"][0]["text"] == "a" and v["tags"] == ["x", "y"] and v["timings"][0]["start"] == 0
    assert v["channel_slug"] == "tech-simplified" and v["updated_at"]
    with pytest.raises(Exception):
        db.update_video(vid, **{"title; DROP TABLE videos": "x"})
    db.update_video(vid)  # no-op
    db.ex("UPDATE videos SET script_json='{broken' WHERE id=?", (vid,))
    assert db.get_video(vid)["script"] is None  # corrupt JSON doesn't crash reads


def test_quota_accounting_uses_pacific_day(env):
    from app import db
    assert db.quota_day() == datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    assert db.quota_remaining() == 10000
    db.record_quota("videos.insert", 1600)
    db.record_quota("thumbnails.set", 50)
    assert db.quota_used() == 1650 and db.quota_remaining() == 8350
    db.ex("INSERT INTO quota_usage(day, operation, units, created_at) VALUES ('2000-01-01','old',9000,'x')")
    assert db.quota_remaining() == 8350  # other days don't count


def test_events_log(env):
    from app import db
    from conftest import new_video
    vid = new_video()
    db.log_event(vid, "test", "x" * 5000, actor="me")
    e = db.one("SELECT * FROM events WHERE video_id=?", (vid,))
    assert e["actor"] == "me" and len(e["detail"]) == 4000


# --------------------------------------------------------------------------- util
def test_timestamps_and_colors():
    from app.util import hex_to_ass, ts_ass, ts_chapter, ts_srt
    assert ts_srt(3725.5) == "01:02:05,500" and ts_srt(-1) == "00:00:00,000"
    assert ts_ass(65.256) == "0:01:05.26"
    assert ts_chapter(65) == "01:05" and ts_chapter(3725) == "1:02:05"
    assert hex_to_ass("#00B4D8") == "&H00D8B400" and hex_to_ass("bad") == "&H00FFFFFF"


def test_clean_spoken():
    from app.util import clean_spoken
    assert clean_spoken("Narrator: **Hello** [music] world 🔐 https://x.y/z #tag") == "Hello world tag"
    assert clean_spoken("VO: line one\nHost: line two") == "line one line two"


def test_run_and_ffmpeg_errors(env):
    from app.util import ffmpeg, probe_duration, run, tool
    with pytest.raises(RuntimeError, match="exited"):
        run(["ls", "/definitely/missing"])
    with pytest.raises(RuntimeError):
        ffmpeg("-i", "/missing.mp4", "/tmp/x.mp4")
    assert probe_duration(env.media["clip_city.mp4"]) == pytest.approx(4.0, abs=0.1)
    with pytest.raises(RuntimeError, match="not found"):
        tool("ffmpeg-does-not-exist")
    env.set(tools={"ffmpeg": "/custom/ffmpeg"})
    assert tool("ffmpeg") == "/custom/ffmpeg"


def test_download_and_size_limit(env, tmp_path):
    from app.util import download
    p = download("https://files.local/clip_city.mp4", tmp_path / "a.mp4")
    assert p.stat().st_size == env.media["clip_city.mp4"].stat().st_size
    with pytest.raises(ValueError, match="exceeds"):
        download("https://files.local/clip_city.mp4", tmp_path / "b.mp4", max_mb=0.001)
    assert not (tmp_path / "b.mp4").exists() and not (tmp_path / "b.mp4.part").exists()
    with pytest.raises(requests.HTTPError):
        download("https://files.local/missing.mp4", tmp_path / "c.mp4")


def test_video_and_cache_dirs(env):
    from app.util import cache_dir, video_dir
    assert video_dir(7).name == "00007" and video_dir(7).is_dir()
    assert cache_dir("media").is_dir()


# --------------------------------------------------------------------------- llm
def test_llm_parse_variants():
    from app.llm import _parse
    assert _parse('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse('Here you go: {"a": {"b": 2}} thanks') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        _parse("no json here")
    with pytest.raises(ValueError):
        _parse("[1, 2]")


def test_llm_chat_json_repairs_bad_replies(env):
    from app import llm
    env.web.brain.bad_json = 1
    out = llm.chat_json("You are a strategist. Respond with JSON only.", "Propose exactly 3 video ideas")
    assert "ideas" in out and len(env.web.brain.calls) == 2


def test_llm_retries_http_errors_and_gives_up(env, monkeypatch):
    from app import llm
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    env.web.brain.fail_http = 2
    assert "ideas" in llm.chat_json("s", "Propose exactly 2 ideas")
    env.web.brain.bad_json = 10
    with pytest.raises(llm.LLMError):
        llm.chat_json("s", "Propose exactly 2 ideas", retries=1)


def test_llm_think_flag_and_unload_and_health(env):
    from app import llm
    env.set(llm__think=False)
    llm.chat_json("s", "Propose exactly 1 idea")
    llm.unload()
    assert ("POST", "ollama.local", "/api/generate") in env.web.calls
    h = llm.health()
    assert h == {"ok": True, "model": "qwen2.5:7b-instruct", "model_present": True}
    env.web.ollama_models = ["llama3.1:8b"]
    assert llm.health()["model_present"] is False
    env.web.ollama_models = ["qwen2.5:7b-instruct"]
    env.set(llm__base_url="http://127.0.0.1:9")
    assert llm.health()["ok"] is False


# --------------------------------------------------------------------------- gpu
def test_gpu_helpers_without_nvidia(env, monkeypatch):
    from app import gpu
    monkeypatch.setattr(gpu.shutil, "which", lambda n: None)
    assert gpu.nvidia_smi()["ok"] is False
    gpu.nvenc_ok.cache_clear()
    assert gpu.nvenc_ok() is False  # no NVIDIA in the sandbox -> CPU fallback
    args = gpu.encoder_args(final=True)
    assert "libx264" in args and "yuv420p" in args
    gpu.setup_cuda_libs()  # must be safe to call with no GPU


def test_gpu_nvidia_smi_parsing(env, monkeypatch, tmp_path):
    from app import gpu
    fake = tmp_path / "nvidia-smi"
    fake.write_text("#!/bin/sh\necho 'NVIDIA GeForce RTX 2070 with Max-Q Design, 512, 8192, 3, 41'\n")
    fake.chmod(0o755)
    monkeypatch.setattr(gpu.shutil, "which", lambda n: str(fake))
    r = gpu.nvidia_smi()
    assert r == {"ok": True, "name": "NVIDIA GeForce RTX 2070 with Max-Q Design", "mem_used_mb": 512,
                 "mem_total_mb": 8192, "util_pct": 3, "temp_c": 41}


def test_cuda_preload_skips_nvblas(env, monkeypatch, tmp_path):
    from app import gpu
    libdir = tmp_path / "nvidia" / "cublas" / "lib"
    libdir.mkdir(parents=True)
    for n in ("libcublas.so.12", "libcublasLt.so.12", "libnvblas.so.12", "libcufft.so.11"):
        (libdir / n).write_bytes(b"")
    loaded = []
    monkeypatch.setattr(gpu.site, "getsitepackages", lambda: [str(tmp_path)])
    monkeypatch.setattr(gpu.ctypes, "CDLL", lambda p, mode=0: loaded.append(p.split("/")[-1]))
    monkeypatch.setattr(gpu, "_libs_done", False)
    gpu.setup_cuda_libs()
    assert sorted(loaded) == ["libcublas.so.12", "libcublasLt.so.12"]


# --------------------------------------------------------------------------- states
def test_gates(env):
    from app.states import gates_for
    assert gates_for({}) == {"idea", "script", "final"}
    assert gates_for({"approval_gates": []}) == set()
    assert gates_for({"approval_gates": ["final"]}) == {"final"}
    env.set(approval__gates=["idea"])
    assert gates_for(None) == {"idea"}
