"""research, script, voice, media, imagegen, captions, video, thumbnail, seo."""
from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path

import pytest
from PIL import Image

from conftest import new_video, run_stage


def ffprobe(path, entries="stream=codec_type,codec_name,width,height,sample_rate:format=duration"):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", entries, "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def scripted(fmt="short", **kw):
    vid = new_video(fmt=fmt, status="idea_approved", **kw)
    run_stage("script", vid)
    from app import db, pipeline
    db.update_video(vid, status="script_approved")
    return vid


# =========================================================================== research
def test_research_collects_every_source_and_creates_ideas(env, monkeypatch):
    from app import db
    from app.agents import research
    monkeypatch.setenv("YOUTUBE_API_KEY", "yt-test-key")
    ch = env.chan()
    res = research.run(ch["id"])
    sources = {r["source"].split(":")[0] for r in db.q("SELECT source FROM research_items")}
    assert sources >= {"google_trends", "rss", "reddit", "youtube_trending", "competitors"}
    assert res["source_errors"] == {}
    assert res["signals"] == 45  # capped by research.max_signals_total, round-robin across sources
    ideas = db.q("SELECT * FROM videos ORDER BY id")
    topics = [i["topic"] for i in ideas]
    assert "How ransomware gangs pick their victims" in topics
    assert "How ransomware gangs pick their victims!" not in topics      # fuzzy duplicate dropped
    assert "" not in topics                                                # empty topic dropped
    bad = next(i for i in ideas if i["topic"].startswith("Passkeys"))
    assert bad["format"] == "short" and bad["idea_score"] == 5.0           # invalid format/score repaired
    assert all(i["status"] == "idea_pending" for i in ideas)               # idea gate on
    assert db.quota_used() >= 2                                            # trending + competitor stats counted


def test_research_without_youtube_key_and_with_dead_feed(env):
    from app import db
    from app.agents import research
    env.web.feed_fail = {"krebsonsecurity.com"}
    res = research.run(env.chan()["id"])
    assert "rss https://krebsonsecurity.com/feed/" in res["source_errors"]
    assert res["ideas"]                                       # still produced ideas
    assert db.quota_used() == 0                               # no key -> no API calls


def test_research_backlog_limit_and_gate_off(env):
    from app import db
    from app.agents import research
    env.set(research__max_pending_ideas=2)
    r1 = research.run(env.chan()["id"])
    assert len(r1["ideas"]) <= 2
    r2 = research.run(env.chan()["id"])
    assert "skipped" in r2
    auto = env.chan("auto-shorts")
    research.run(auto["id"])
    statuses = {v["status"] for v in db.q("SELECT status FROM videos WHERE channel_id=?", (auto["id"],))}
    assert statuses == {"idea_approved"}                      # no idea gate -> straight to approved
    assert {v["format"] for v in db.q("SELECT format FROM videos WHERE channel_id=?", (auto["id"],))} == {"short"}


def test_research_youtube_search_quota_guard(env, monkeypatch):
    from app import db
    from app.agents import research
    monkeypatch.setenv("YOUTUBE_API_KEY", "yt-test-key")
    env.set(research__youtube_search_enabled=True)
    db.record_quota("other", 7950)   # 2050 left, reserve 2000 -> search (100) refused, list (1) allowed
    research.collect_signals(env.chan()["config"])
    ops = [r["operation"] for r in db.q("SELECT operation FROM quota_usage")]
    assert "research.search" not in ops and "research.videos" in ops


def test_research_uses_past_performance(env):
    from app import db
    from app.agents import research
    vid = new_video(status="published", title="Old hit", youtube_video_id="vidOLD")
    db.ex("INSERT INTO analytics_snapshots(video_id, youtube_video_id, views, likes, comments, captured_at) VALUES (?,?,?,?,?,?)",
          (vid, "vidOLD", 12345, 10, 2, db.now_iso()))
    assert "Old hit" in research._past_performance(env.chan()["id"]) and "12345" in research._past_performance(env.chan()["id"])


# =========================================================================== script
def test_short_script(env):
    from app import db
    vid = new_video("short")
    res = run_stage("script", vid)
    v = db.get_video(vid)
    s = v["script"]
    assert v["status"] == "script_review" and res["sections"] == 4
    texts = [x["text"] for x in s["sections"]]
    assert texts[0] == "Your password is already leaked."                       # markdown + [music] stripped
    assert texts[1].startswith("Attackers")                                      # speaker label stripped
    assert "🔐" not in texts[3]
    assert s["sections"][1]["visual"] == "hacker hoodie laptop"                  # punctuation stripped
    assert "" not in s["title_options"] and v["title"] == "Your Password Is Already Leaked"
    assert v["script_text"].count("\n\n") == 3


def test_short_script_too_long_is_rewritten(env):
    from app import db
    env.web.brain.short_too_long = True
    vid = new_video("short")
    run_stage("script", vid)
    assert any("too long" in c or "Rewrite" in c for c in env.web.brain.calls) or len(env.web.brain.calls) >= 2
    from app.agents.script import words
    assert words(db.get_video(vid)["script"]["sections"]) < 200


def test_long_script_has_chapters_and_feedback(env):
    from app import db
    vid = new_video("long", topic="What the new EU AI Act means for you", feedback="add a real-world example")
    run_stage("script", vid)
    s = db.get_video(vid)["script"]
    chapters = [x["chapter"] for x in s["sections"]]
    assert chapters[0] == "Intro" and chapters[-1] == "Wrap-up"
    assert {"What the law covers", "What changes for users", "What happens next"} <= set(chapters)
    assert len(env.web.brain.calls) == 4   # outline + 3 chapters


def test_script_gate_off_auto_advances(env):
    from app import db
    vid = new_video("short", slug="auto-shorts")
    run_stage("script", vid)
    assert db.get_video(vid)["status"] in ("script_approved", "producing")
    assert db.one("SELECT COUNT(*) AS n FROM jobs WHERE kind='produce' AND video_id=?", (vid,))["n"] == 1


# =========================================================================== voice
def _wav_info(p):
    with wave.open(str(p)) as w:
        return w.getframerate(), w.getnchannels(), w.getnframes() / w.getframerate()


def test_voice_test_tone_timings(env):
    from app import db
    from app.agents import voice
    vid = scripted()
    res = voice.run(vid)
    v = db.get_video(vid)
    sr, ch, dur = _wav_info(v["voice_path"])
    assert (sr, ch) == (48000, 1) and dur == pytest.approx(res["duration"], abs=0.05)
    t = v["timings"]
    assert t[0]["start"] == 0.0 and t[-1]["end"] == pytest.approx(dur, abs=0.01)
    assert all(a["end"] == b["start"] for a, b in zip(t, t[1:]))        # contiguous
    assert all(x["start"] < x["speech_end"] <= x["end"] for x in t)
    assert db.one("SELECT COUNT(*) AS n FROM assets WHERE video_id=? AND kind='voice'", (vid,))["n"] == 1


def test_voice_speed_changes_duration(env):
    from app.agents import voice
    a = voice.run(scripted())["duration"]
    env.set_channel("tech-simplified", voice={"provider": "test_tone", "speed": 1.25})
    b = voice.run(scripted())["duration"]
    assert b < a * 0.9


def test_voice_elevenlabs(env, monkeypatch):
    from app.agents import voice
    env.set_channel("tech-simplified", voice={"provider": "elevenlabs", "voice_id": "v1"})
    vid = scripted()
    with pytest.raises(RuntimeError, match="ELEVENLABS_API_KEY"):
        voice.run(vid)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-test-key")
    assert voice.run(vid)["provider"] == "elevenlabs"


def test_voice_edge_provider(env, monkeypatch):
    import edge_tts
    from app.agents import voice
    used = {}

    class FakeCommunicate:
        def __init__(self, text, voice_name, rate="+0%"):
            used["voice"], used["rate"] = voice_name, rate

        async def save(self, path):
            Path(path).write_bytes(env.media["speech.mp3"].read_bytes())
    monkeypatch.setattr(edge_tts, "Communicate", FakeCommunicate)
    env.set_channel("tech-simplified", voice={"provider": "edge", "edge_voice": "en-US-AndrewNeural"})
    assert voice.run(scripted())["provider"] == "edge" and used["voice"] == "en-US-AndrewNeural"


@pytest.mark.parametrize("api", ["synthesize_wav", "synthesize"])
def test_voice_piper_both_api_versions(env, monkeypatch, api):
    import piper
    from app.agents import voice
    models = env.tmp / "piper"
    models.mkdir()
    (models / "en_US-ryan-high.onnx").write_bytes(b"x")
    env.set(voice__piper_models_dir=str(models))

    class FakeVoice:
        @staticmethod
        def load(p):
            return FakeVoice()

    def synth(self, text, wf):
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(22050)
        wf.writeframes(b"\x10\x20" * 22050)
    setattr(FakeVoice, api, synth)
    monkeypatch.setattr(piper, "PiperVoice", FakeVoice)
    voice._piper_cache.clear()
    env.set_channel("tech-simplified", voice={"provider": "piper", "model": "en_US-ryan-high"})
    assert voice.run(scripted())["provider"] == "piper"


def test_voice_piper_missing_model_and_unknown_provider(env):
    from app.agents import voice
    env.set(voice__piper_models_dir=str(env.tmp / "empty"))
    env.set_channel("tech-simplified", voice={"provider": "piper", "model": "nope"})
    with pytest.raises(FileNotFoundError, match="install.sh"):
        voice.run(scripted())
    env.set_channel("tech-simplified", voice={"provider": "robot"})
    with pytest.raises(ValueError, match="unknown voice provider"):
        voice.run(scripted())


# =========================================================================== media
def produce_until(vid, step):
    from app.agents import captions, media, voice
    voice.run(vid)
    if step == "voice":
        return
    return media.run(vid)


def test_broll_library_and_matching(env):
    from app.agents.media import broll_library, broll_match
    lib = broll_library({})
    assert {c["name"] for c in lib} == {"traffic-night-rain", "server-room-lights"}
    assert "city" in next(c for c in lib if c["name"] == "traffic-night-rain")["words"]  # folder name counts
    used = set()
    assert broll_match(lib, "city traffic night", used)["name"] == "traffic-night-rain"
    assert broll_match(lib, "server room lights", used)["name"] == "server-room-lights"
    assert broll_match(lib, "ocean waves", used) is None
    assert broll_match(lib, "the of and", used) is None
    used.add(lib[0]["path"]); used.add(lib[1]["path"])
    assert broll_match(lib, "city traffic night", used) is None        # no re-use in one video
    # per-channel folder searched first
    own = env.tmp / "chan-broll"
    own.mkdir()
    (own / "padlock-closeup.mp4").write_bytes(env.media["clip_city.mp4"].read_bytes())
    assert broll_library({"broll_dir": str(own)})[0]["name"] == "padlock-closeup"


def test_media_mixes_broll_and_ai_images(env):
    from app import db
    vid = scripted("short")
    res = produce_until(vid, "media")
    plan = json.loads((Path(db.get_video(vid)["voice_path"]).parent / "plan.json").read_text())
    assert res["placeholder"] == 0 and res["broll"] >= 2 and res["ai"] >= 1 and res["stock"] == 0
    imgs = [p for p in plan if p["kind"] == "image"]
    assert all(Path(p["path"]).exists() and p["motion"] in ("in", "out", "left", "right") for p in imgs)
    assert all(p["prompt"].startswith("dark server room") for p in imgs)    # LLM-written prompts
    total = sum(p["duration"] for p in plan)
    assert total == pytest.approx(db.get_video(vid)["duration_sec"], abs=0.05)
    kinds = {r["kind"] for r in db.q("SELECT kind FROM assets WHERE video_id=?", (vid,))}
    assert {"broll", "ai_image", "voice"} <= kinds


def test_media_stock_pexels_and_pixabay(env, monkeypatch):
    from app import db
    monkeypatch.setenv("PIXABAY_API_KEY", "pixabay-test-key")
    env.set(media__order=["stock"], media__providers=["pixabay"])
    vid = scripted("long")
    res = produce_until(vid, "media")
    assert res["stock"] >= 1 and res["placeholder"] == 0
    a = db.one("SELECT * FROM assets WHERE video_id=? AND kind='stock_video'", (vid,))
    assert a["provider"] == "pixabay" and a["license"] == "Pixabay Content License" and Path(a["path"]).exists()
    # Pexels with a wrong key: falls through to placeholders -> all-placeholder error
    monkeypatch.delenv("PIXABAY_API_KEY")
    monkeypatch.setenv("PEXELS_API_KEY", "wrong")
    env.set(media__providers=["pexels"])
    vid2 = scripted("short")
    with pytest.raises(RuntimeError, match="no visuals found"):
        produce_until(vid2, "media")
    monkeypatch.setenv("PEXELS_API_KEY", "pexels-test-key")
    res3 = produce_until(scripted("short"), "media")
    assert res3["stock"] >= 1


def test_media_channel_visuals_override_and_retry_safe(env):
    from app import db
    from app.agents import media
    vid = new_video("short", slug="auto-shorts", status="idea_approved")
    run_stage("script", vid)
    produce_until(vid, "media")
    n1 = db.one("SELECT COUNT(*) AS n FROM assets WHERE video_id=? AND kind='ai_image'", (vid,))["n"]
    assert db.one("SELECT COUNT(*) AS n FROM assets WHERE video_id=? AND kind='broll'", (vid,))["n"] == 0  # visuals: [ai]
    media.run(vid)  # retry: images reused, asset rows not duplicated
    assert db.one("SELECT COUNT(*) AS n FROM assets WHERE video_id=? AND kind='ai_image'", (vid,))["n"] == n1


def test_media_ai_failure_degrades_with_clear_error(env):
    from app.agents import media
    env.set(ai_images__model=str(env.tmp / "missing-model"), media__order=["ai"])
    vid = scripted("short")
    with pytest.raises(RuntimeError, match="AI images unavailable"):
        produce_until(vid, "media")
    env.set(media__order=["broll", "ai"])
    res = media.run(vid)                        # broll still works, ai sections become placeholders
    assert res["broll"] >= 1 and res["placeholder"] >= 1 and "ai_error" in res


def test_media_ai_disabled(env):
    env.set(ai_images__enabled=False, media__order=["broll", "ai"])
    res = produce_until(scripted("short"), "media")
    assert res["ai"] == 0


# =========================================================================== imagegen
def test_imagegen_prompts_fallback_and_llm(env):
    from app.agents import imagegen
    secs = [{"text": "Narration one", "visual": "padlock"}, {"text": "two", "visual": "server"}]
    ch = {"name": "Demo Channel", "niche": "infosec"}
    p = imagegen.build_prompts(secs, [2, 0], ch, {"visual_style": "STYLE"})
    assert len(p[0]) == 2 and p[0][0].startswith("dark server room") and p[1] == []
    env.set(ai_images__prompt_with_llm=False)
    p = imagegen.build_prompts(secs, [1, 1], ch, {"visual_style": "STYLE"})
    assert p == [["padlock, STYLE"], ["server, STYLE"]]
    env.set(ai_images__prompt_with_llm=True)
    env.web.brain.bad_json = 99
    p = imagegen.build_prompts(secs, [1, 0], ch, {})
    assert p[0][0].startswith("padlock, cinematic")   # LLM broken -> fallback, no crash


def test_imagegen_generate_resume_seed_status(env, tmp_path):
    from app.agents import imagegen
    jobs = [{"prompt": "a", "path": tmp_path / "a.png", "seed": imagegen.seed_for(1, 0)},
            {"prompt": "b", "path": tmp_path / "b.png", "seed": imagegen.seed_for(1, 1)}]
    imagegen.generate(jobs, "short")
    assert all(j["ok"] for j in jobs) and Image.open(tmp_path / "a.png").size == (64, 112)
    m = (tmp_path / "a.png").stat().st_mtime
    imagegen.generate(jobs, "short")          # already there -> skipped
    assert (tmp_path / "a.png").stat().st_mtime == m
    assert imagegen.seed_for(1, 0) == imagegen.seed_for(1, 0) != imagegen.seed_for(1, 1)
    assert imagegen.size_for("long") == (112, 64)
    st = imagegen.status()
    assert st["enabled"] and st["libs"] and st["downloaded"]


def test_imagegen_gpu_branch_simulated(env, tmp_path, monkeypatch):
    """Exercise the CUDA code path (offload + VAE slicing) on a CPU box."""
    import torch
    from diffusers import StableDiffusionXLPipeline
    from app.agents import imagegen
    calls = []
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(StableDiffusionXLPipeline, "enable_model_cpu_offload", lambda self, *a, **k: calls.append("offload"))
    jobs = [{"prompt": "x", "path": tmp_path / "g.png", "seed": 1}]
    imagegen.generate(jobs, "short")
    assert jobs[0]["ok"] and calls == ["offload"]


def test_imagegen_lightning_lora_path(env, tmp_path, monkeypatch):
    """LoRA load/fuse/unload + trailing Euler scheduler are called in the right order."""
    from diffusers import StableDiffusionXLPipeline
    import huggingface_hub
    from app.agents import imagegen
    order = []
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", lambda repo, f: order.append(("dl", repo, f)) or "/fake.safetensors")
    monkeypatch.setattr(StableDiffusionXLPipeline, "load_lora_weights", lambda self, p: order.append("load"))
    monkeypatch.setattr(StableDiffusionXLPipeline, "fuse_lora", lambda self: order.append("fuse"))
    monkeypatch.setattr(StableDiffusionXLPipeline, "unload_lora_weights", lambda self: order.append("unload"))
    env.set(ai_images__lightning_repo="ByteDance/SDXL-Lightning", ai_images__lightning_file="sdxl_lightning_8step_lora.safetensors")
    pipe, cuda = imagegen._load_pipeline()
    assert order == [("dl", "ByteDance/SDXL-Lightning", "sdxl_lightning_8step_lora.safetensors"), "load", "fuse", "unload"]
    assert pipe.scheduler.config.timestep_spacing == "trailing"


def test_imagegen_download_command(env, monkeypatch):
    import huggingface_hub
    from diffusers import DiffusionPipeline
    from app.agents import imagegen
    got = []
    monkeypatch.setattr(DiffusionPipeline, "download", classmethod(lambda cls, m, **k: got.append(("pipe", m, k.get("variant")))))
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", lambda repo, f: got.append((repo, f)))
    env.set(ai_images__model="stabilityai/stable-diffusion-xl-base-1.0", ai_images__vae="madebyollin/sdxl-vae-fp16-fix",
            ai_images__lightning_repo="ByteDance/SDXL-Lightning", ai_images__lightning_file="sdxl_lightning_8step_lora.safetensors")
    imagegen.download()
    assert ("pipe", "stabilityai/stable-diffusion-xl-base-1.0", "fp16") in got
    assert ("madebyollin/sdxl-vae-fp16-fix", "diffusion_pytorch_model.safetensors") in got
    assert ("ByteDance/SDXL-Lightning", "sdxl_lightning_8step_lora.safetensors") in got


# =========================================================================== captions
def test_captions_fallback_writes_srt_and_karaoke_ass(env):
    from app import db
    from app.agents import captions
    env.set(video__captions__short_style="karaoke")
    vid = scripted("short")
    produce_until(vid, "media")
    res = captions.run(vid)       # whisper weights can't download in the sandbox -> proportional fallback
    v = db.get_video(vid)
    assert res["method"] in ("whisper", "proportional") and res["burned_in"] is True
    srt = Path(v["captions_srt"]).read_text()
    assert srt.startswith("1\n00:00:00,") and "-->" in srt
    ass = Path(v["captions_ass"]).read_text()
    assert "PlayResX: 1080" in ass and "\\c&H00D8B400" in ass          # brand colour highlight
    assert ass.count("Dialogue:") >= res["words"] - 5                  # one event per word (karaoke)


def test_captions_whisper_path_parsing(env, monkeypatch):
    import faster_whisper
    from app.agents import captions

    class W:
        def __init__(self, word, s, e):
            self.word, self.start, self.end = word, s, e

    class Seg:
        words = [W(" Hello", 0.1, 0.4), W(" world.", 0.45, 0.9), W(" ", 1.0, 1.1)]

    class FakeModel:
        def __init__(self, size, device, compute_type):
            assert device in ("cuda", "cpu")
            if device == "cuda":
                raise RuntimeError("no CUDA")

        def transcribe(self, path, **kw):
            assert kw["word_timestamps"] is True and kw["language"] == "en"
            return [Seg()], None
    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModel)
    words = captions._whisper_words("x.wav", "en")
    assert [w["word"] for w in words] == ["Hello", "world."]          # GPU failed -> CPU retry worked


def test_caption_chunking_rules():
    from app.agents.captions import _wrap2, chunk
    words = [{"word": w, "start": i * 0.3, "end": i * 0.3 + 0.25} for i, w in enumerate("one two three. four five six seven".split())]
    c = chunk(words, 16, 3)
    assert [" ".join(x["word"] for x in ch) for ch in c] == ["one two three.", "four five six", "seven"]
    gap = [{"word": "a", "start": 0, "end": 0.2}, {"word": "b", "start": 1.5, "end": 1.7}]
    assert len(chunk(gap, 80, 14)) == 2
    assert _wrap2("short", 42) == "short"
    assert "\n" in _wrap2("a fairly long caption line that needs wrapping for sure", 42)


def test_long_form_captions_not_burned(env):
    from app import db
    from app.agents import captions
    env.set(video__captions__long_burn_in=False)
    vid = scripted("long")
    produce_until(vid, "media")
    assert captions.run(vid)["burned_in"] is False and db.get_video(vid)["captions_ass"] is None


# =========================================================================== video + thumbnail + seo
def full_produce(fmt="short", **kw):
    from app import db
    vid = scripted(fmt, **kw)
    out = run_stage("produce", vid)
    return vid, out, db.get_video(vid)


def test_produce_short_end_to_end(env):
    vid, out, v = full_produce("short")
    assert v["status"] == "final_review"
    assert set(out) == {"voice", "media", "animate", "captions", "video", "thumbnail", "seo"}
    info = ffprobe(v["video_path"])
    vs = next(s for s in info["streams"] if s["codec_type"] == "video")
    au = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert (vs["codec_name"], vs["width"], vs["height"]) == ("h264", 1080, 1920)
    assert (au["codec_name"], au["sample_rate"]) == ("aac", "48000")
    voice_len = float(ffprobe(Path(v["video_path"]).parent / "voice.wav")["format"]["duration"])
    assert float(info["format"]["duration"]) == pytest.approx(voice_len + 0.6, abs=0.15)
    assert out["video"]["music"] == "track.mp3"
    assert not list((Path(v["video_path"]).parent / "segments").glob("*.mp4"))  # temp segments cleaned
    thumb = Image.open(v["thumbnail_path"])
    assert thumb.size == (1080, 1920) and Path(v["thumbnail_path"]).stat().st_size < 2_000_000


def test_render_loudness_near_minus_14(env):
    vid, out, v = full_produce("short")
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", v["video_path"], "-af", "ebur128", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
    lufs = float(r.rsplit("I:", 1)[1].split("LUFS")[0])
    assert -17 <= lufs <= -11


def test_produce_long_seo_chapters_and_thumbnail(env):
    vid, out, v = full_produce("long", topic="What the new EU AI Act means for you")
    assert Image.open(v["thumbnail_path"]).size == (1280, 720)
    d = v["description"]
    assert "Chapters:\n00:00 " in d                       # first chapter at 0:00 (short intro merged into it)
    assert d.split("Chapters:\n")[1].count("\n") >= 2      # at least 3 chapters
    assert "Visuals: AI-generated images." in d and "Narration: AI-generated voice." in d
    assert "Subscribe for weekly updates." in d
    assert "#Shorts" not in d


def test_seo_rules(env):
    vid, out, v = full_produce("short")
    assert v["title"] == "Your Password Is Already Leaked - Here's Why"         # <> and spaces cleaned
    assert len(v["title"]) <= 100
    tags = v["tags"]
    assert sum(len(t) + (2 if " " in t else 0) for t in tags) + len(tags) - 1 <= 500
    assert "How ransomware gangs pick their victims" in tags or len(tags) >= 8
    assert v["description"].rstrip().endswith("#Shorts #cybersecurity #infosec")
    assert "<" not in v["description"] and len(v["description"].encode()) < 5000


def test_seo_chapter_rules():
    from app.agents.seo import _chapters, _fit_tags
    secs = [{"chapter": c} for c in ["Intro", "A", "A", "B", "C", "Wrap-up"]]
    t = [{"start": s} for s in [0, 5, 30, 60, 90, 118]]
    out = _chapters(secs, t, 125)
    assert out.splitlines()[0] == "00:00 A" or out.splitlines()[0].startswith("00:00")
    assert "Wrap-up" not in out                                   # last chapter < 10 s folded
    assert _chapters(secs[:2], t[:2], 20) == ""                   # < 3 chapters -> none
    assert _fit_tags(["a", "A", "b,c", "<x>", ""]) == ["a", "bc", "x"]


def test_video_handles_broken_clip_and_short_warning(env, monkeypatch):
    from app import db
    from app.agents import video
    vid = scripted("short")
    produce_until(vid, "media")
    from app.agents import captions
    captions.run(vid)
    d = Path(db.get_video(vid)["voice_path"]).parent
    plan = json.loads((d / "plan.json").read_text())
    plan[0] = {**plan[0], "kind": "video", "path": str(env.media["broken.mp4"])}
    (d / "plan.json").write_text(json.dumps(plan))
    env.set(video__short__max_seconds=5)
    res = video.run(vid)
    assert res["warning"] and "Short is" in res["warning"]
    assert Path(res["path"]).exists()


def test_still_motion_directions(env, tmp_path):
    from app.agents.video import _still
    img = tmp_path / "i.png"
    Image.new("RGB", (64, 112), (10, 80, 160)).save(img)
    for m in ("in", "out", "left", "right"):
        _still(str(img), 1.0, 360, 640, 30, m, f"{m}.mp4", tmp_path)
        assert ffprobe(tmp_path / f"{m}.mp4")["streams"][0]["width"] == 360


def test_research_several_countries(env, monkeypatch):
    from app.agents import research
    assert research._codes("AE") == ["AE"] and research._codes("UAE, us; ae") == ["AE", "US"]
    assert research._codes(["AE", "US"]) == ["AE", "US"] and research._codes("") == ["US"] and research._codes("uk") == ["GB"]
    seen = {"trends": [], "yt": []}
    monkeypatch.setattr(research, "google_trends", lambda geo, limit: seen["trends"].append(geo) or [])
    monkeypatch.setattr(research, "youtube_trending", lambda region, cat, limit: seen["yt"].append(region) or [])
    research.collect_signals({"research": {"trends_geo": "AE, US"}})
    assert seen == {"trends": ["AE", "US"], "yt": ["AE", "US"]}                          # YouTube trending follows trends_geo
    seen["trends"].clear(); seen["yt"].clear()
    research.collect_signals({"research": {"trends_geo": ["AE"], "region": "US"}})
    assert seen == {"trends": ["AE"], "yt": ["US"]}
