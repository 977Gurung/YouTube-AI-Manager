"""AI animation (image-to-video) and animated "pop" captions."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import numpy as np
import pytest

from conftest import run_stage
from test_agents import ffprobe, produce_until, scripted


def _plan(vid):
    from app import db
    d = Path(db.get_video(vid)["voice_path"]).parent
    return d, json.loads((d / "plan.json").read_text())


# =========================================================================== which scenes get animated
def _fake_plan(n, sections):
    return [{"kind": "image", "source": "ai", "path": f"img{i}.png", "section": sections[i]} for i in range(n)]


def test_select_scope_all_key_none_and_cap(env):
    from app.agents import animate
    secs = [{"chapter": "Intro"}, {"chapter": "Intro"}, {"chapter": "Risks"}, {"chapter": "Risks"}, {"chapter": "Fixes"}]
    plan = _fake_plan(10, [0, 0, 1, 1, 2, 2, 3, 3, 4, 4])
    plan[5] = {"kind": "video", "source": "broll", "path": "own.mp4", "section": 2}     # own footage is never touched
    env.set(ai_video__scope={"short": "all", "long": "key"}, ai_video__max_clips={"short": 16, "long": 12})
    assert animate.select(plan, secs, "short") == [0, 1, 2, 3, 4, 6, 7, 8, 9]
    assert animate.select(plan, secs, "long") == [0, 4, 8]                              # hook + first scene of each chapter
    env.set(ai_video__max_clips={"short": 4, "long": 2})
    capped = animate.select(plan, secs, "short")
    assert len(capped) == 4 and capped[0] == 0 and capped[-1] == 9                       # spread over the whole video
    assert animate.select(plan, secs, "long") == [0, 8]
    env.set(ai_video__scope={"short": "none", "long": "none"})
    assert animate.select(plan, secs, "short") == []
    env.set(ai_video__scope={"short": "key", "long": "key"}, ai_video__max_clips={"short": 16, "long": 12}, ai_video__key_clips=3)
    no_chapters = animate.select(_fake_plan(9, [0] * 9), [{}], "short")                  # Shorts have no chapters
    assert no_chapters == [0, 4, 8]
    assert animate.select([], secs, "short") == []


def test_sizes_frames_and_modes(env):
    from app.agents import animate
    env.set(ai_video__short_size=[500, 850], ai_video__frames=100)
    assert animate.size_for("short") == (480, 832) and animate.n_frames() == 97          # multiples of 32, 8n+1 frames
    m = animate.mode()
    assert m["fast"] is False and m["steps"] == 2 and m["guidance"] == 3.0               # nothing verified yet -> base model
    animate.state_path().write_text(json.dumps({"transformer_file": "fast.safetensors", "dtype": "float16"}))
    m = animate.mode()
    assert m["fast"] and m["guidance"] == 1.0 and m["dtype"] == "float16" and m["transformer_file"] == "fast.safetensors"
    assert "slow cinematic" in animate.motion_prompt({"prompt": "a server room"}, {})
    assert animate.motion_prompt({"prompt": "x"}, {"motion_style": "fast zoom"}).endswith("fast zoom")


# =========================================================================== generation (tiny random LTX-Video)
def test_generate_writes_clip_resumes_and_reports_errors(env, tmp_path):
    from PIL import Image
    from app.agents import animate
    img = tmp_path / "still.png"
    Image.radial_gradient("L").resize((768, 1344)).convert("RGB").save(img)
    jobs = [{"image": str(img), "prompt": "a robot, slow pan", "out": str(tmp_path / "c" / "a.mp4"), "seed": 1},
            {"image": str(tmp_path / "missing.png"), "prompt": "x", "out": str(tmp_path / "c" / "b.mp4"), "seed": 2}]
    seen = []
    animate.generate(jobs, "short", on_clip=lambda n, t, j: seen.append((n, t, bool(j.get("ok")))))
    assert jobs[0]["ok"] and jobs[0]["frames"] == 9 and jobs[0]["fps"] == 24
    assert not jobs[1]["ok"] and jobs[1]["error"]                                        # one bad still never stops the rest
    assert seen == [(1, 2, True), (2, 2, False)]
    vs = ffprobe(jobs[0]["out"], "stream=codec_name,width,height,nb_frames")["streams"][0]
    assert (vs["width"], vs["height"], int(vs["nb_frames"])) == (64, 96, 9)
    mtime = Path(jobs[0]["out"]).stat().st_mtime
    again = animate.generate([dict(jobs[0])], "short")
    assert again[0]["ok"] and Path(jobs[0]["out"]).stat().st_mtime == mtime              # existing clip kept (resume)


def test_generate_time_budget_and_frame_checks(env, tmp_path):
    from PIL import Image
    from app.agents import animate
    img = tmp_path / "still.png"
    Image.new("RGB", (64, 96), "navy").save(img)
    jobs = [{"image": str(img), "prompt": "x", "out": str(tmp_path / f"{i}.mp4"), "seed": i} for i in range(2)]
    animate.generate(jobs, "short", deadline=1.0)                                         # deadline long past
    assert [j.get("error") for j in jobs] == ["time budget used up"] * 2
    with pytest.raises(RuntimeError, match="NaN"):
        animate._check(np.full((3, 4, 4, 3), np.nan))
    with pytest.raises(RuntimeError, match="blank"):
        animate._check(np.zeros((3, 4, 4, 3)))
    animate._check(np.random.default_rng(0).random((3, 4, 4, 3)))
    animate.write_mp4(np.random.default_rng(0).random((5, 32, 48, 3)), 24, tmp_path / "w.mp4")
    assert int(ffprobe(tmp_path / "w.mp4", "stream=nb_frames")["streams"][0]["nb_frames"]) == 5


def test_text_encoder_is_freed_before_video_model_loads(env, tmp_path, monkeypatch):
    """8 GB rule: T5 must be gone before the transformer is loaded."""
    from PIL import Image
    from app.agents import animate
    order = []
    enc, load = animate._encode, animate._load_pipeline
    monkeypatch.setattr(animate, "_encode", lambda p, m: (order.append("encode"), enc(p, m))[1])

    def loader(m):
        pipe = load(m)
        order.append(("load", pipe.text_encoder is None and pipe.tokenizer is None))
        return pipe
    monkeypatch.setattr(animate, "_load_pipeline", loader)
    img = tmp_path / "s.png"
    Image.radial_gradient("L").resize((64, 96)).convert("RGB").save(img)
    animate.generate([{"image": str(img), "prompt": "x", "out": str(tmp_path / "o.mp4"), "seed": 1}], "short")
    assert order == ["encode", ("load", True)]


# =========================================================================== pipeline step
def test_animate_step_updates_plan_logs_progress_and_is_retry_safe(env):
    from app import db
    from app.agents import animate
    vid = scripted("short")
    produce_until(vid, "media")
    d, before = _plan(vid)
    stills = [i for i, p in enumerate(before) if p["kind"] == "image"]
    assert stills
    res = animate.run(vid)
    d, plan = _plan(vid)
    done = [i for i, p in enumerate(plan) if p.get("clip")]
    assert res["clips"] == len(done) == min(3, len(stills)) and res["planned"] == res["clips"]
    assert all(Path(plan[i]["clip"]).exists() and plan[i]["clip_frames"] == 9 and plan[i]["kind"] == "image" for i in done)
    assert json.loads((d / "animate.json").read_text())["clips"] == res["clips"]
    ev = [e["detail"] for e in db.q("SELECT detail FROM events WHERE video_id=? AND action='animate'", (vid,))]
    assert len(ev) == res["clips"] and ev[0].startswith("clip 1/")
    assert db.q("SELECT COUNT(*) AS n FROM assets WHERE video_id=? AND kind='ai_video'", (vid,))[0]["n"] == 1
    animate.run(vid)
    assert db.q("SELECT COUNT(*) AS n FROM assets WHERE video_id=? AND kind='ai_video'", (vid,))[0]["n"] == 1


def test_animate_degrades_to_stills_never_fails_the_video(env, monkeypatch):
    from app import db
    from app.agents import animate, video, captions
    vid = scripted("short")
    produce_until(vid, "media")
    env.set(ai_video__model="Lightricks/not-downloaded")
    res = animate.run(vid)
    assert res["clips"] == 0 and "fetch-models" in res["skipped"]
    env.set(ai_video__model=str(env.tmp.parent / "x"), ai_video__enabled=False)
    assert animate.run(vid) == {"skipped": "disabled"}
    env.set_channel("tech-simplified", animate=False)
    env.set(ai_video__enabled=True)
    assert animate.run(vid) == {"skipped": "disabled"}                                   # per-channel switch
    env.set_channel("tech-simplified", animate=True)
    # the model itself crashes -> noted, video still produced from stills
    from conftest import SANDBOX
    env.set(ai_video__model=str(SANDBOX / "tinyltx"))
    monkeypatch.setattr(animate, "generate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("CUDA exploded")))
    res = animate.run(vid)
    assert res["clips"] == 0 and "CUDA exploded" in res["note"]
    captions.run(vid)
    out = video.run(vid)
    assert out["animated"] == 0 and Path(out["path"]).exists()


def test_animated_segment_fills_scene_exactly(env, tmp_path):
    """Clip shorter than the scene: slowed a little, then forward-backward; exact duration and size."""
    from app.agents import animate, video
    rng = np.random.default_rng(1)
    frames = np.stack([np.full((96, 64, 3), i / 24.0) * 0.8 + rng.random((96, 64, 3)) * 0.2 for i in range(25)])
    animate.write_mp4(frames, 24, tmp_path / "clip.mp4")                                 # ~1 s clip that gets brighter
    for dur, mode in ((1.2, "blend"), (4.0, "off")):
        env.set(ai_video__interpolate=mode)
        out = tmp_path / f"seg{dur}.mp4"
        video._animated(str(tmp_path / "clip.mp4"), 25, 24, dur, 540, 960, 30, str(out), None)
        info = ffprobe(out, "stream=width,height,r_frame_rate:format=duration")
        s = info["streams"][0]
        assert (s["width"], s["height"], s["r_frame_rate"]) == (540, 960, "30/1")
        assert abs(float(info["format"]["duration"]) - dur) < 0.08

    def luma(t):
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(tmp_path / "seg4.0.mp4"), "-frames:v", "1",
                              "-vf", "scale=8:8,format=gray", "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
        return sum(raw) / len(raw)
    # 25 frames slowed 1.35x = 1.41 s forward, then backward: bright at the turn, dark again after it
    assert luma(1.3) > luma(0.1) + 40 and luma(2.7) < luma(1.3) - 40


def test_produce_short_uses_animated_clips(env):
    from app import db
    vid = scripted("short")
    out = run_stage("produce", vid)
    assert list(out) == ["voice", "media", "animate", "captions", "video", "thumbnail", "seo"]
    assert out["animate"]["clips"] >= 1 and out["video"]["animated"] == out["animate"]["clips"]
    assert out["captions"]["style"] == "pop"
    v = db.get_video(vid)
    vs = next(s for s in ffprobe(v["video_path"])["streams"] if s["codec_type"] == "video")
    assert (vs["width"], vs["height"]) == (1080, 1920) and v["status"] == "final_review"


def test_produce_long_animates_key_scenes_only_and_redo_cleans_up(env):
    from app import db, pipeline
    vid = scripted("long")
    env.set(ai_video__max_clips={"short": 16, "long": 12})
    out = run_stage("produce", vid)
    d, plan = _plan(vid)
    stills = [p for p in plan if p["kind"] == "image"]
    clips = [p for p in plan if p.get("clip")]
    chapters = {s.get("chapter") for s in db.get_video(vid)["script"]["sections"] if s.get("chapter")}
    assert 1 <= len(clips) <= len(chapters) + 1 and len(clips) < len(stills)             # hook + chapter openers, not everything
    assert out["captions"]["burned_in"] is True and out["captions"]["style"] == "pop"    # long-form gets animated captions too
    assert (d / "clips").is_dir() and (d / "animate.json").exists()
    pipeline.reject(vid, "redo", "more energy")
    assert not (d / "clips").exists() and not (d / "animate.json").exists() and not (d / "fonts").exists()


# =========================================================================== fetch-models: self-configuration
def _fake_hub(monkeypatch, works: set[str]):
    import diffusers
    import huggingface_hub
    calls = {"snap": [], "files": []}
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda repo, allow_patterns=None, **k: calls["snap"].append(allow_patterns))
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", lambda repo, name, **k: (calls["files"].append(name), f"/cache/{name}")[1])

    def loader(path, **k):
        if Path(path).name not in works:
            raise ValueError(f"unknown checkpoint layout: {Path(path).name}")
        return object()
    monkeypatch.setattr(diffusers.LTXVideoTransformer3DModel, "from_single_file", staticmethod(loader))
    monkeypatch.setattr(diffusers.AutoencoderKLLTXVideo, "from_single_file", staticmethod(loader))
    return calls


def test_download_picks_first_checkpoint_that_loads(env, monkeypatch):
    from app.agents import animate
    env.set(ai_video__model="Lightricks/LTX-Video", ai_video__distilled_files=["new.safetensors", "old.safetensors"])
    calls = _fake_hub(monkeypatch, works={"old.safetensors"})
    res = animate.download()
    assert res["mode"] == "fast (distilled)" and res["file"] == "old.safetensors"
    st = animate.state()
    assert st["transformer_file"] == "old.safetensors" and "new.safetensors" in st["skipped"]
    assert calls["files"] == ["new.safetensors", "old.safetensors"]
    assert len(calls["snap"]) == 1 and "text_encoder/*" in calls["snap"][0] and "transformer/*" not in calls["snap"][0]
    assert animate.mode()["fast"] and animate.mode()["steps"] == 2


def test_download_falls_back_to_base_model(env, monkeypatch):
    from app.agents import animate
    env.set(ai_video__model="Lightricks/LTX-Video", ai_video__distilled_files=["a.safetensors"])
    calls = _fake_hub(monkeypatch, works=set())
    res = animate.download()
    assert res["mode"] == "base" and animate.state()["transformer_file"] is None
    assert calls["snap"][-1] == ["transformer/*", "vae/*"]                               # base weights fetched only now
    m = animate.mode()
    assert not m["fast"] and m["guidance"] == 3.0


def test_self_test_switches_precision_when_gpu_gives_bad_frames(env, monkeypatch):
    from app.agents import animate
    tried = []

    def fake_generate(jobs, fmt, m=None, size=None, frames=None, **k):
        tried.append((m["dtype"], size, frames))
        if m["dtype"] == "bfloat16":
            jobs[0]["error"] = "model produced invalid frames (NaN)"
        else:
            jobs[0]["ok"] = True
        return jobs
    monkeypatch.setattr(animate, "generate", fake_generate)
    animate.state_path().write_text(json.dumps({"transformer_file": None}))
    res = animate.self_test()
    assert res["ok"] and res["dtype"] == "float16" and tried == [("bfloat16", (256, 416), 9), ("float16", (256, 416), 9)]
    assert animate.state()["dtype"] == "float16" and animate.mode()["dtype"] == "float16"
    monkeypatch.setattr(animate, "generate", lambda jobs, *a, **k: jobs[0].update(error="no") or jobs)
    assert animate.self_test()["ok"] is False


def test_self_test_runs_for_real_on_tiny_model(env):
    from app.agents import animate
    res = animate.self_test()
    assert res["ok"] and res["dtype"] == "bfloat16"


# =========================================================================== pop captions
def W(*pairs):
    t, out = 0.0, []
    for w in pairs:
        out.append({"word": w, "start": t, "end": t + 0.3})
        t += 0.35
    return out


def test_keyword_scoring_and_selection(env):
    from app.agents.captions import keyword_score, pick_keywords
    tw = {"password", "leaked"}
    assert keyword_score("$4.5", tw) == 3 and keyword_score("80%", tw) == 3 and keyword_score("2026,", tw) == 3
    assert keyword_score("AI", tw) == 3 and keyword_score("VPN.", tw) == 3
    assert keyword_score("never", tw) == 2 and keyword_score("Password!", tw) == 1
    assert keyword_score("the", tw) == 0 and keyword_score("A", tw) == 0 and keyword_score("...", tw) == 0
    chunks = [W("the", "quick", "fox"), W("lost", "$4.5", "million"), W("never", "again"), W("and", "so", "on"), W("your", "password")]
    keys = pick_keywords(chunks, "Your password leaked", share=0.5)
    assert keys == [None, 1, 0, None, None]                                              # strongest 2 of 5 phrases, one word each
    assert pick_keywords(chunks, "", share=1.0)[4] is None


def test_pop_ass_structure(env, tmp_path):
    from app.agents.captions import write_ass_pop
    env.set(video__captions__keyword_share=1.0)
    chunks = [W("Your", "password"), W("costs", "$5", "online"), W("ok")]
    n = write_ass_pop(chunks, tmp_path / "c.ass", 1080, 1920, "#00B4D8", True, title="Your password is leaked", font="Anton")
    ass = (tmp_path / "c.ass").read_text()
    lines = [l.split(",,", 1)[1].split(",,", 1)[-1] for l in ass.splitlines() if l.startswith("Dialogue:")]
    assert "WrapStyle: 2" in ass and "Style: Default,Anton," in ass and n == 2
    assert len(lines) == 6                                                               # one event per spoken word
    first = lines[0]
    assert first.startswith("{\\fad(70,0)}") and "YOUR" in first                         # phrase fades in, uppercase
    assert "\\alpha&HFF&}PASSWORD" in first                                              # next word reserved but invisible
    assert "\\fscx122" in first and "\\t(0,130,\\fscx100\\fscy100)" in first and "\\c&H00D8B400" in first   # pop in brand colour
    kw = lines[3]                                                                        # "$5" is being spoken
    assert "\\c&H006D4DFF" in kw and "\\fscx145" in kw and "\\frz" in kw and "$5" in kw  # key word: bigger, tilted, 2nd colour
    after = lines[4]                                                                     # "ONLINE" spoken, "$5" stays coloured
    assert re.search(r"\{\\c&H006D4DFF\}\$5\{\\r\}", after)
    assert "\\N" in lines[2] and "\\N" not in lines[5]                                   # long phrase = fixed two lines
    times = re.findall(r"Dialogue: 0,(\d:\d\d:\d\d\.\d\d),(\d:\d\d:\d\d\.\d\d)", ass)
    assert times and all(a < b for a, b in times)


def test_pop_keyword_colour_never_equals_brand(env, tmp_path):
    from app.agents.captions import write_ass_pop
    env.set(video__captions__keyword_color="#FFD400")
    write_ass_pop([W("save", "50%")], tmp_path / "c.ass", 1080, 1920, "#FFD400", True)
    assert "\\c&H00FFFFFF\\fscx145" in (tmp_path / "c.ass").read_text()


def test_pop_captions_burn_in_with_bundled_font(env, tmp_path):
    """libass must accept the file and find the copied display font."""
    from app import db
    from app.agents import captions
    from PIL import ImageFont
    font = env.tmp / "Anton-Regular.ttf"
    src = next(Path("/usr/share/fonts").rglob("*Bold*.ttf"), None) or next(Path("/usr/share/fonts").rglob("*.ttf"))
    font.write_bytes(src.read_bytes())
    family = ImageFont.truetype(str(font), 20).getname()[0]
    env.set(video__captions__pop_font=family, video__captions__pop_font_path=str(font))
    vid = scripted("short")
    produce_until(vid, "media")
    res = captions.run(vid)
    v = db.get_video(vid)
    d = Path(v["captions_ass"]).parent
    assert res["style"] == "pop" and res["burned_in"] and (d / "fonts" / "Anton-Regular.ttf").exists()
    assert f"Style: Default,{family}," in Path(v["captions_ass"]).read_text()
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x202830:s=540x960:r=30:d=3",
                    "-vf", "ass=captions.ass:fontsdir=fonts", "-frames:v", "60", "burn.mp4"], cwd=d, check=True)
    # captions really drew something bright on the dark background
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", "1.0", "-i", "burn.mp4", "-frames:v", "1", "-vf", "format=gray",
                          "-f", "rawvideo", "-"], cwd=d, capture_output=True, check=True).stdout
    assert max(raw) > 200


def test_caption_style_per_channel_and_none(env):
    from app import db
    from app.agents import captions
    vid = scripted("short")
    produce_until(vid, "media")
    env.set_channel("tech-simplified", caption_style="karaoke")
    assert captions.run(vid)["style"] == "karaoke"
    env.set_channel("tech-simplified", caption_style="none")
    res = captions.run(vid)
    assert res["burned_in"] is False and db.get_video(vid)["captions_ass"] is None and db.get_video(vid)["captions_srt"]


# =========================================================================== CLI
def test_doctor_reports_animation_state(env):
    from test_api_cli import cli
    r = cli(env, "doctor")
    line = next(l for l in r.stdout.splitlines() if "AI animation" in l)
    assert "[OK]" in line and "LTX-Video ready, base" in line
    env.set(ai_video__enabled=False)
    line = next(l for l in cli(env, "doctor").stdout.splitlines() if "AI animation" in l)
    assert "[!!]" in line and "moving-camera stills" in line
