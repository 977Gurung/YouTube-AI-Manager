"""Voice agent: AI narration per script section, normalised and stitched into one WAV
with exact per-section timings (used to cut visuals and build chapters).

Providers:
  chatterbox  - local expressive TTS on the GPU (Resemble AI Chatterbox, MIT): natural prosody with an
                emotion-strength setting, optional voice reference clip. Lives in its own virtualenv
                (.venv-voice, `./ytm.sh setup-voice`) and is called through scripts/voice_worker.py.
                If it is not installed or fails, narration falls back to voice.chatterbox.fallback.
  piper       - local neural TTS (CPU, fast, free). Check each voice's MODEL_CARD licence before monetising.
  edge        - Microsoft Edge online voices via edge-tts (free, high quality, unofficial service).
  elevenlabs  - paid API, best quality, commercial use per your plan.
  test_tone   - generates a tone of the right length (pipeline testing without a voice model).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import wave
from pathlib import Path

import requests

from .. import db
from ..config import ROOT, env, resolve_path, settings
from ..util import ffmpeg, video_dir

log = logging.getLogger("ytm.voice")
SR = 48000
_piper_cache: dict = {}


def _piper(text: str, out: Path, vcfg: dict) -> Path:
    from piper import PiperVoice  # type: ignore

    name = vcfg.get("model", "en_US-ryan-high")
    path = resolve_path(settings.get("voice.piper_models_dir", "./models/piper")) / f"{name}.onnx"
    if not path.exists():
        raise FileNotFoundError(f"Piper model not found: {path} (install.sh downloads the default voice)")
    voice = _piper_cache.get(str(path)) or PiperVoice.load(str(path))
    _piper_cache[str(path)] = voice
    out = out.with_suffix(".wav")
    with wave.open(str(out), "wb") as wf:
        if hasattr(voice, "synthesize_wav"):      # piper-tts >= 1.3
            voice.synthesize_wav(text, wf)
        else:                                      # piper-tts 1.2.x
            voice.synthesize(text, wf)
    return out


def _edge(text: str, out: Path, vcfg: dict) -> Path:
    import edge_tts  # type: ignore

    out = out.with_suffix(".mp3")
    comm = edge_tts.Communicate(text, vcfg.get("edge_voice", "en-US-AndrewNeural"), rate=vcfg.get("rate", "+0%"))
    asyncio.run(comm.save(str(out)))
    return out


def _elevenlabs(text: str, out: Path, vcfg: dict) -> Path:
    key = env("ELEVENLABS_API_KEY")
    if not key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set in .env")
    voice_id = vcfg.get("voice_id") or env("ELEVENLABS_VOICE_ID")
    if not voice_id:
        raise RuntimeError("Set voice.voice_id for this channel (or ELEVENLABS_VOICE_ID)")
    r = requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                      headers={"xi-api-key": key, "Accept": "audio/mpeg"},
                      json={"text": text, "model_id": vcfg.get("model_id", "eleven_multilingual_v2"),
                            "voice_settings": {"stability": 0.45, "similarity_boost": 0.8}}, timeout=180)
    r.raise_for_status()
    out = out.with_suffix(".mp3")
    out.write_bytes(r.content)
    return out


def _test_tone(text: str, out: Path, vcfg: dict) -> Path:
    out = out.with_suffix(".wav")
    dur = max(1.0, len(text.split()) / 2.5)
    ffmpeg("-f", "lavfi", "-i", f"sine=frequency=330:duration={dur:.2f}", "-af", "volume=0.2", str(out))
    return out


# --------------------------------------------------------------------------- chatterbox (expressive, own venv)
def _cb(key: str, default=None):
    return settings.get(f"voice.chatterbox.{key}", default)


def chatterbox_python() -> Path:
    """The voice virtualenv's interpreter. NOT resolved: bin/python is a symlink to the system Python,
    and following it would start that one instead, without the venv's packages."""
    p = Path(str(_cb("python", "./.venv-voice/bin/python"))).expanduser()
    return p if p.is_absolute() else ROOT / p


def chatterbox_status() -> dict:
    py = chatterbox_python()
    return {"installed": py.exists(), "ready": py.exists() and (py.parent.parent / ".ready").exists(), "python": str(py)}


def emotion_for(text: str, index: int, total: int, vcfg: dict) -> float:
    """Emotion strength per section (Chatterbox `exaggeration`: ~0.3 calm, 0.5 neutral, 0.7+ lively).
    emotion: auto lifts the hook, exclamations/questions and the closing line a little above the base."""
    base = float(vcfg.get("exaggeration", _cb("exaggeration", 0.6)))
    if str(vcfg.get("emotion", _cb("emotion", "auto"))).lower() != "auto":
        return round(base, 2)
    boost = (0.15 if index == 0 else 0.0) + (0.10 if "!" in text else 0.0) + (0.05 if "?" in text else 0.0) \
        + (0.05 if total > 1 and index == total - 1 else 0.0)
    return round(min(base + boost, float(_cb("max_exaggeration", 1.0))), 2)


def _chatterbox_all(texts: list[str], wd: Path, vcfg: dict) -> list[Path]:
    """All sections in one worker run (the model loads once). Returns one raw WAV per section."""
    py = chatterbox_python()
    if not py.exists():
        raise RuntimeError("expressive voice is not installed - run ./ytm.sh setup-voice")
    ref = vcfg.get("reference")
    if ref and not Path(resolve_path(ref)).is_file():
        raise RuntimeError(f"voice reference clip not found: {ref}")
    outs = [wd / f"sec_{i:03d}.cb.wav" for i in range(len(texts))]
    job = {"items": [{"text": t, "out": str(o), "exaggeration": emotion_for(t, i, len(texts), vcfg)}
                     for i, (t, o) in enumerate(zip(texts, outs))],
           "cfg_weight": float(vcfg.get("cfg_weight", _cb("cfg_weight", 0.4))),
           "temperature": float(vcfg.get("temperature", _cb("temperature", 0.8))),
           "reference": str(resolve_path(ref)) if ref else None, "device": _cb("device", "auto"),
           "sentence_pause_ms": int(_cb("sentence_pause_ms", 180)), "max_chars": int(_cb("max_chars", 280))}
    jp = wd / "voice_job.json"
    jp.write_text(json.dumps(job), encoding="utf-8")
    result = Path(str(jp) + ".result")
    result.unlink(missing_ok=True)
    from .. import llm
    llm.unload()                                    # the voice model needs the GPU
    e = {**os.environ, "HF_HOME": str(resolve_path(settings.get("ai_images.cache_dir", "./models/hf"))), "PYTHONUNBUFFERED": "1"}
    e.pop("PYTHONPATH", None)
    try:
        p = subprocess.run([str(py), str(resolve_path(_cb("worker", str(ROOT / "scripts" / "voice_worker.py")))), str(jp)],
                           capture_output=True, text=True, env=e, timeout=int(_cb("timeout_s", 5400)))
    except subprocess.TimeoutExpired:
        raise RuntimeError("expressive voice timed out") from None
    try:
        res = json.loads(result.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        res = {"ok": False, "error": (p.stderr or p.stdout or "no output")[-400:]}
    if not res.get("ok") or any(not o.exists() for o in outs):
        raise RuntimeError(f"expressive voice failed: {res.get('error', 'missing audio')}")
    log.info("chatterbox: %s sections in %ss on %s", len(outs), res.get("seconds"), res.get("device"))
    return outs


PROVIDERS = {"chatterbox": None, "piper": _piper, "edge": _edge, "elevenlabs": _elevenlabs, "test_tone": _test_tone}


def _normalise(src: Path, dst: Path, speed: float) -> None:
    trim = ("silenceremove=start_periods=1:start_threshold=-50dB,areverse,"
            "silenceremove=start_periods=1:start_threshold=-50dB,areverse")
    af = trim + (f",atempo={speed:.3f}" if abs(speed - 1.0) > 0.01 else "")
    ffmpeg("-i", str(src), "-af", af, "-ar", str(SR), "-ac", "1", "-sample_fmt", "s16", str(dst))


def run(video_id: int) -> dict:
    v = db.get_video(video_id)
    cfg = (db.get_channel(v["channel_id"]).get("config") or {})
    vcfg = cfg.get("voice", {}) or {}
    provider = vcfg.get("provider", settings.get("voice.default_provider", "piper"))
    if provider not in PROVIDERS:
        raise ValueError(f"unknown voice provider {provider}")
    sections = v["script"]["sections"]
    wd = video_dir(video_id) / "voice"
    wd.mkdir(exist_ok=True)

    raws, fallback = None, None
    if provider == "chatterbox":
        try:
            raws = _chatterbox_all([s["text"] for s in sections], wd, vcfg)
        except Exception as e:  # noqa: BLE001 - a missing/broken voice model must not stop production
            fb = str(_cb("fallback", "piper") or "none")
            if fb in ("none", "chatterbox") or fb not in PROVIDERS:
                raise
            fallback = str(e)[:300]
            log.warning("expressive voice unavailable (%s) - using %s", fallback, fb)
            db.log_event(video_id, "voice_fallback", f"{fb} used instead of chatterbox: {fallback}", channel_id=v["channel_id"])
            provider = fb
    parts = []
    for i, s in enumerate(sections):
        raw = raws[i] if raws else PROVIDERS[provider](s["text"], wd / f"sec_{i:03d}", vcfg)
        norm = wd / f"sec_{i:03d}.norm.wav"
        _normalise(raw, norm, float(vcfg.get("speed", 1.0)))
        parts.append(norm)

    pause = int(SR * settings.get("voice.section_pause_ms", 350) / 1000)
    lead = int(SR * 0.15)
    out_path = video_dir(video_id) / "voice.wav"
    part_path = video_dir(video_id) / "voice.part.wav"
    timings, cursor = [], lead
    with wave.open(str(part_path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SR)
        out.writeframes(b"\x00\x00" * lead)
        for i, p in enumerate(parts):
            with wave.open(str(p), "rb") as w:
                frames = w.readframes(w.getnframes())
                n = w.getnframes()
            out.writeframes(frames)
            start, speech_end = cursor / SR, (cursor + n) / SR
            cursor += n
            if i < len(parts) - 1:
                out.writeframes(b"\x00\x00" * pause)
                cursor += pause
            timings.append({"index": i, "start": round(start, 3), "speech_end": round(speech_end, 3),
                            "chapter": sections[i].get("chapter")})
    part_path.replace(out_path)
    total = cursor / SR
    # contiguous visual spans: each section owns the time until the next one starts
    for i, t in enumerate(timings):
        t["end"] = timings[i + 1]["start"] if i + 1 < len(timings) else round(total, 3)
    timings[0]["start"] = 0.0
    db.update_video(video_id, voice_path=str(out_path), timings=timings, duration_sec=round(total, 2))
    db.ex("INSERT INTO assets(video_id, kind, path, provider, license, created_at) VALUES (?,?,?,?,?,?)",
          (video_id, "voice", str(out_path), provider, "AI-generated narration", db.now_iso()))
    return {"provider": provider, "duration": round(total, 2), **({"fallback_reason": fallback} if fallback else {})}


def sample(slug: str, text: str | None = None) -> dict:
    """`./ytm.sh voice-sample <slug>`: speak a short paragraph with the channel's voice settings,
    so the voice can be judged and tuned without producing a whole video."""
    ch = db.get_channel_by_slug(slug)
    if not ch:
        raise ValueError(f"unknown channel {slug}")
    vcfg = (ch.get("config") or {}).get("voice", {}) or {}
    provider = vcfg.get("provider", settings.get("voice.default_provider", "piper"))
    texts = [text] if text else [
        "Stop scrolling! Your password is probably already for sale, and it costs less than a cup of coffee.",
        "Here is what actually happened. Attackers collected old breach lists, tried them everywhere, and waited.",
        "The good news? One small change today protects you. Turn on multi-factor authentication, right now."]
    from ..config import data_dir
    wd = data_dir() / "voice-sample" / slug
    wd.mkdir(parents=True, exist_ok=True)
    for old in wd.glob("sec_*"):
        old.unlink()
    raws = _chatterbox_all(texts, wd, vcfg) if provider == "chatterbox" else \
        [PROVIDERS[provider](t, wd / f"sec_{i:03d}", vcfg) for i, t in enumerate(texts)]
    norms = []
    for i, r in enumerate(raws):
        n = wd / f"sec_{i:03d}.norm.wav"
        _normalise(Path(r), n, float(vcfg.get("speed", 1.0)))
        norms.append(n)
    out = data_dir() / f"voice-sample-{slug}.wav"
    lst = wd / "list.txt"
    lst.write_text("".join(f"file '{n.as_posix()}'\n" for n in norms), encoding="utf-8")
    ffmpeg("-f", "concat", "-safe", "0", "-i", str(lst), "-ar", str(SR), "-ac", "1", str(out))
    return {"provider": provider, "file": str(out), "emotion": [emotion_for(t, i, len(texts), vcfg) for i, t in enumerate(texts)]}
