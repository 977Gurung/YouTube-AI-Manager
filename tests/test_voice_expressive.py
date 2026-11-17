"""Expressive narration (Chatterbox provider): text splitting, emotion per section, the isolated
worker process, fallbacks, CLI. The real model is replaced by tests/tools/fake_chatterbox
(same interface); the worker script itself is the shipped one."""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from conftest import ROOT
from test_agents import scripted

spec = importlib.util.spec_from_file_location("voice_worker", ROOT / "scripts" / "voice_worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


@pytest.fixture
def cb(env):
    """Point the provider at a 'voice venv' whose python can import the stand-in model."""
    log = env.tmp / "cb.log"
    py = env.tmp / "voice-venv" / "bin" / "python"
    py.parent.mkdir(parents=True)

    def write(fail=""):
        py.write_text(f"#!/bin/sh\nPYTHONPATH='{ROOT / 'tests' / 'tools' / 'fake_chatterbox'}' FAKE_CB_LOG='{log}' "
                      f"FAKE_CB_FAIL='{fail}' exec '{sys.executable}' \"$@\"\n")
        py.chmod(py.stat().st_mode | stat.S_IEXEC)
    write()
    (py.parent.parent / ".ready").touch()
    env.set(voice__chatterbox__python=str(py), voice__chatterbox__fallback="test_tone")
    env.set_channel("tech-simplified", voice={"provider": "chatterbox", "exaggeration": 0.5})
    env.cb_log = lambda: [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    env.cb_fail = write
    env.cb_py = py
    return env


# =========================================================================== worker helpers
def test_split_text_sentences_and_limits():
    s = worker.split_text
    assert s("") == [] and s("   ") == []
    assert s("Hello there. How are you? Fine!", 14) == ["Hello there.", "How are you?", "Fine!"]
    assert s("One. Two. Three.", 280) == ["One. Two. Three."]                           # short text stays together
    long = "This is a very long sentence, with several clauses, that keeps going and going, far beyond the limit we set."
    parts = s(long, 40)
    assert all(len(p) <= 40 for p in parts) and " ".join(parts).replace("  ", " ") == long
    assert s("x" * 100, 30) == ["x" * 30, "x" * 30, "x" * 30, "x" * 10]                  # no spaces at all
    assert s('He said "stop." Then left.', 16) == ['He said "stop."', "Then left."]
    assert s("No full stop at the end") == ["No full stop at the end"]


def test_save_wav_format_and_clipping(tmp_path):
    import numpy as np
    secs = worker.save_wav(np.ones(12000, dtype="float32") * 3.0, 24000, tmp_path / "a.wav")
    with wave.open(str(tmp_path / "a.wav")) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 24000, 12000)
        peak = max(abs(int.from_bytes(w.readframes(1)[:2], "little", signed=True)) for _ in range(5))
    assert secs == 0.5 and 30000 < peak <= 32767                                         # loud input is scaled, not wrapped


def test_emotion_strength_per_section(env):
    from app.agents.voice import emotion_for
    env.set(voice__chatterbox__exaggeration=0.6, voice__chatterbox__emotion="auto")
    assert emotion_for("Plain statement.", 1, 4, {}) == 0.6
    assert emotion_for("The hook.", 0, 4, {}) == 0.75                                    # opening line gets more energy
    assert emotion_for("Stop! Really?", 1, 4, {}) == 0.75
    assert emotion_for("Subscribe now.", 3, 4, {}) == 0.65                               # closing line
    assert emotion_for("Stop! Now? Yes!", 0, 4, {"exaggeration": 0.9}) == 1.0            # capped
    assert emotion_for("Stop!", 0, 4, {"exaggeration": 0.4, "emotion": "fixed"}) == 0.4  # per-channel override


# =========================================================================== provider
def test_chatterbox_narration_end_to_end(cb):
    from app import db
    from app.agents import voice
    vid = scripted("short")
    res = voice.run(vid)
    v = db.get_video(vid)
    n = len(v["script"]["sections"])
    assert res["provider"] == "chatterbox" and "fallback_reason" not in res
    with wave.open(v["voice_path"]) as w:
        assert (w.getnchannels(), w.getframerate()) == (1, 48000) and abs(w.getnframes() / 48000 - res["duration"]) < 0.02
    assert len(v["timings"]) == n and v["timings"][0]["start"] == 0.0
    log = cb.cb_log()
    assert [e["event"] for e in log].count("load") == 1                                  # model loaded once for the whole video
    gens = [e for e in log if e["event"] == "generate"]
    assert len(gens) >= n and gens[0]["exaggeration"] >= 0.65                            # hook boosted above the 0.5 base
    assert {e["cfg_weight"] for e in gens} == {0.4} and {e["reference"] for e in gens} == {None}
    assert " ".join(e["text"] for e in gens).split() == " ".join(s["text"] for s in v["script"]["sections"]).split()
    assert db.q("SELECT provider FROM assets WHERE video_id=? AND kind='voice'", (vid,))[0]["provider"] == "chatterbox"


def test_chatterbox_passes_reference_clip_and_settings(cb, media):
    from app.agents import voice
    ref = cb.tmp / "my-voice.wav"
    ref.write_bytes(Path(media["music.mp3"]).read_bytes())
    cb.set_channel("tech-simplified", voice={"provider": "chatterbox", "reference": str(ref), "cfg_weight": 0.3,
                                             "temperature": 0.6, "emotion": "fixed", "exaggeration": 0.8})
    voice.run(scripted("short"))
    gens = [e for e in cb.cb_log() if e["event"] == "generate"]
    assert {(e["reference"], e["cfg_weight"], e["temperature"], e["exaggeration"]) for e in gens} == {(str(ref), 0.3, 0.6, 0.8)}


def test_worker_resumes_finished_sections(cb):
    from app.agents import voice
    vid = scripted("short")
    voice.run(vid)
    first = len([e for e in cb.cb_log() if e["event"] == "generate"])
    voice.run(vid)                                                                       # e.g. retry after a later step failed
    assert len([e for e in cb.cb_log() if e["event"] == "generate"]) == first


@pytest.mark.parametrize("problem", ["not installed", "load", "generate", "missing reference"])
def test_falls_back_to_other_voice_instead_of_failing(cb, problem):
    from app import db
    from app.agents import voice
    if problem == "not installed":
        cb.set(voice__chatterbox__python=str(cb.tmp / "nope" / "python"))
    elif problem == "missing reference":
        cb.set_channel("tech-simplified", voice={"provider": "chatterbox", "reference": "/nope/voice.wav"})
    else:
        cb.cb_fail(problem)
    vid = scripted("short")
    res = voice.run(vid)
    assert res["provider"] == "test_tone" and res["fallback_reason"] and Path(db.get_video(vid)["voice_path"]).exists()
    ev = db.q("SELECT detail FROM events WHERE video_id=? AND action='voice_fallback'", (vid,))
    assert len(ev) == 1 and "instead of chatterbox" in ev[0]["detail"]
    expect = {"not installed": "setup-voice", "load": "out of memory", "generate": "generation failed", "missing reference": "reference clip not found"}
    assert expect[problem] in res["fallback_reason"]


def test_fallback_none_fails_the_stage(cb):
    from app.agents import voice
    cb.set(voice__chatterbox__fallback="none", voice__chatterbox__python=str(cb.tmp / "nope"))
    with pytest.raises(RuntimeError, match="setup-voice"):
        voice.run(scripted("short"))


def test_produce_with_expressive_voice(cb):
    from conftest import run_stage
    from app import db
    vid = scripted("short")
    out = run_stage("produce", vid)
    assert out["voice"]["provider"] == "chatterbox" and db.get_video(vid)["status"] == "final_review"


# =========================================================================== CLI + doctor
def _cli(env, *args):
    from test_api_cli import cli
    return cli(env, *args)


def test_voice_sample_command(cb):
    r = _cli(cb, "voice-sample", "tech-simplified")
    out = json.loads(r.stdout[r.stdout.index("{"):])
    assert r.returncode == 0 and out["provider"] == "chatterbox" and out["emotion"][0] > out["emotion"][1]
    with wave.open(out["file"]) as w:
        assert w.getframerate() == 48000 and w.getnframes() / 48000 > 5
    r = _cli(cb, "voice-sample", "tech-simplified", "Just one custom line.")
    assert r.returncode == 0 and [e["text"] for e in cb.cb_log() if e["event"] == "generate"][-1] == "Just one custom line."
    assert _cli(cb, "voice-sample", "no-such-channel").returncode == 1


def test_doctor_voice_line(cb):
    line = lambda: next(l for l in _cli(cb, "doctor").stdout.splitlines() if "Voice: expressive" in l)
    assert "[OK]" in line() and "used by tech-simplified" in line()
    (cb.cb_py.parent.parent / ".ready").unlink()
    assert "[!!]" in line() and "falls back to Piper" in line()
    cb.set_channel("tech-simplified", voice={"provider": "test_tone"})
    assert "optional" in line()


# =========================================================================== installer script
def test_setup_voice_script_installs_checks_and_enables(tmp_path):
    """Runs the real scripts/setup-voice.sh in a copy of the project, installing the stand-in package
    into a real virtualenv, then checks the worker self-test and the channels.yaml switch."""
    proj = tmp_path / "proj"
    (proj / "scripts").mkdir(parents=True)
    (proj / "config").mkdir()
    (proj / ".venv" / "bin").mkdir(parents=True)
    for f in ("env.sh", "setup-voice.sh", "voice_worker.py"):
        (proj / "scripts" / f).write_bytes((ROOT / "scripts" / f).read_bytes())
    (proj / "config" / "channels.yaml").write_text(
        "channels:\n  - slug: a\n    voice:\n      provider: piper      # local\n      model: en_US-ryan-high\n"
        "  - slug: b\n    voice:\n      provider: edge\n")
    fake_py = proj / ".venv" / "bin" / "python"                                          # stands in for `app.cli sync-channels`
    fake_py.write_text(f"#!/bin/sh\necho \"$@\" >> '{proj}/cli-calls.log'\n")
    fake_py.chmod(0o755)
    e = {**os.environ, "YTM_VOICE_PACKAGE": str(ROOT / "tests" / "tools" / "fake_chatterbox"), "YTM_VOICE_PYTHON": sys.executable,
         "YTM_VOICE_MIN_GB": "100000"}
    r = subprocess.run(["bash", str(proj / "scripts" / "setup-voice.sh")], capture_output=True, text=True, env=e, timeout=60)
    assert r.returncode == 1 and "free disk space" in r.stdout and not (proj / ".venv-voice").exists()   # refuses on a full disk
    e["YTM_VOICE_MIN_GB"] = "1"
    r = subprocess.run(["bash", str(proj / "scripts" / "setup-voice.sh"), "--enable"], capture_output=True, text=True, env=e, timeout=600)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (proj / ".venv-voice" / ".ready").exists() and '"ok": true' in r.stdout
    ch = (proj / "config" / "channels.yaml").read_text()
    assert "provider: chatterbox      # local" in ch and "provider: edge" in ch and "provider: piper" not in ch
    assert list((proj / "config").glob("channels.yaml.bak.*")) and "sync-channels" in (proj / "cli-calls.log").read_text()
    # a failing self-test leaves the voice disabled and says so
    (proj / "scripts" / "voice_worker.py").write_text("import sys; print('{\"ok\": false, \"error\": \"boom\"}'); sys.exit(1)\n")
    r = subprocess.run(["bash", str(proj / "scripts" / "setup-voice.sh")], capture_output=True, text=True, env=e, timeout=600)
    assert r.returncode == 1 and "Voice test failed" in r.stdout and not (proj / ".venv-voice" / ".ready").exists()


def test_venv_python_symlink_is_not_resolved(env, tmp_path, monkeypatch):
    """Regression: a venv's bin/python is a symlink to the system interpreter. Resolving it ran the
    system Python ("No module named 'chatterbox'") and made doctor report the voice as not installed."""
    from app.agents import voice
    venv = tmp_path / ".venv-voice"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(sys.executable)
    (venv / ".ready").touch()
    monkeypatch.setattr(voice, "ROOT", tmp_path)
    env.set(voice__chatterbox__python="./.venv-voice/bin/python")
    py = voice.chatterbox_python()
    assert py == venv / "bin" / "python" and py.is_symlink()
    assert voice.chatterbox_status() == {"installed": True, "ready": True, "python": str(py)}
    env.set(voice__chatterbox__python=str(venv / "bin" / "python"))
    assert voice.chatterbox_python() == venv / "bin" / "python"
