#!/usr/bin/env python3
"""Expressive narration with Chatterbox (Resemble AI, MIT licence). Stand-alone on purpose:
it runs inside its own virtualenv (.venv-voice) because chatterbox-tts pins library versions
(torch, diffusers, transformers) that clash with the image/animation models in the main one.

    voice_worker.py job.json      # {"items":[{"text","out","exaggeration"}], "cfg_weight", "temperature",
                                  #  "reference", "device", "sentence_pause_ms", "max_chars"}
    voice_worker.py --check       # imports + model download + one test sentence; prints JSON

Writes one mono 16-bit WAV per item and a result JSON next to the job file (job.json.result).
Long sections are split into sentences (the model handles ~30 s per call) and re-joined with
short natural pauses. Chatterbox adds an inaudible watermark to its audio (Resemble "Perth").
"""
from __future__ import annotations

import json
import re
import sys
import time
import wave
from pathlib import Path


def split_text(text: str, max_chars: int = 280) -> list[str]:
    """Sentence-sized pieces, never longer than max_chars (split on commas, then words, if needed)."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    sents = re.findall(r"[^.!?…]+(?:[.!?…]+[\"')\]]*|$)", text)
    out, cur = [], ""
    for s in (x.strip() for x in sents):
        if not s:
            continue
        while len(s) > max_chars:                       # one very long sentence
            cut = max(s.rfind(", ", 0, max_chars), s.rfind("; ", 0, max_chars), s.rfind(" ", 0, max_chars))
            cut = cut + 1 if cut > max_chars // 3 else max_chars
            if cur:
                out.append(cur)
                cur = ""
            out.append(s[:cut].strip())
            s = s[cut:].strip()
        if cur and len(cur) + 1 + len(s) > max_chars:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        out.append(cur)
    return out


def save_wav(samples, sr: int, path: Path) -> float:
    import numpy as np
    a = np.clip(np.asarray(samples, dtype="float32").reshape(-1), -1.0, 1.0)
    peak = float(np.abs(a).max()) if a.size else 0.0
    if peak > 0.99:
        a = a / peak * 0.98
    tmp = path.with_suffix(".part.wav")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((a * 32767).astype("<i2").tobytes())
    tmp.replace(path)
    return a.size / sr


def load_model(device: str):
    from chatterbox.tts import ChatterboxTTS
    try:
        import perth
        if getattr(perth, "PerthImplicitWatermarker", 1) is None:
            raise RuntimeError("the voice environment is missing 'pkg_resources' - fix: "
                               ".venv-voice/bin/python -m pip install 'setuptools<81'   then run ./ytm.sh setup-voice again")
    except ImportError:
        pass
    if device == "auto":
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            device = "cpu"
    return ChatterboxTTS.from_pretrained(device=device), device


def speak(model, text: str, job: dict, exaggeration: float):
    import numpy as np
    pieces = split_text(text, int(job.get("max_chars", 280)))
    gap = np.zeros(int(model.sr * int(job.get("sentence_pause_ms", 180)) / 1000), dtype="float32")
    chunks = []
    for i, p in enumerate(pieces):
        kw = {"exaggeration": float(exaggeration), "cfg_weight": float(job.get("cfg_weight", 0.4)),
              "temperature": float(job.get("temperature", 0.8))}
        if job.get("reference"):
            kw["audio_prompt_path"] = job["reference"]
        wav = model.generate(p, **kw)
        chunks.append(np.asarray(wav.detach().cpu().numpy() if hasattr(wav, "detach") else wav, dtype="float32").reshape(-1))
        if i < len(pieces) - 1:
            chunks.append(gap)
    return np.concatenate(chunks) if chunks else np.zeros(int(model.sr * 0.3), dtype="float32")


def run_job(job_path: Path) -> dict:
    job = json.loads(job_path.read_text(encoding="utf-8"))
    t0 = time.time()
    model, device = load_model(job.get("device", "auto"))
    loaded = time.time() - t0
    done = []
    for it in job["items"]:
        out = Path(it["out"])
        if out.exists() and out.stat().st_size > 1000:   # resume after an interrupted run
            done.append({"out": str(out), "cached": True})
            continue
        t1 = time.time()
        audio = speak(model, it["text"], job, it.get("exaggeration", job.get("exaggeration", 0.6)))
        secs = save_wav(audio, model.sr, out)
        done.append({"out": str(out), "seconds": round(secs, 2), "took": round(time.time() - t1, 1)})
        print(f"voice {len(done)}/{len(job['items'])}: {secs:.1f}s audio in {time.time() - t1:.1f}s", file=sys.stderr, flush=True)
    return {"ok": True, "device": device, "sample_rate": model.sr, "load_seconds": round(loaded, 1),
            "seconds": round(time.time() - t0, 1), "items": done}


def check() -> dict:
    import tempfile
    t0 = time.time()
    model, device = load_model("auto")
    with tempfile.TemporaryDirectory() as tmp:
        audio = speak(model, "This is a quick voice check. It sounds natural, doesn't it?", {}, 0.6)
        secs = save_wav(audio, model.sr, Path(tmp) / "check.wav")
    return {"ok": secs > 0.5, "device": device, "audio_seconds": round(secs, 1), "seconds": round(time.time() - t0, 1)}


if __name__ == "__main__":
    try:
        if len(sys.argv) == 2 and sys.argv[1] == "--check":
            res = check()
            print(json.dumps(res))
        elif len(sys.argv) == 2:
            jp = Path(sys.argv[1])
            res = run_job(jp)
            Path(str(jp) + ".result").write_text(json.dumps(res), encoding="utf-8")
        else:
            print(__doc__)
            sys.exit(2)
    except Exception as e:  # noqa: BLE001 - the caller reads the reason and falls back to another voice
        msg = {"ok": False, "error": f"{type(e).__name__}: {e}"[:600]}
        if len(sys.argv) == 2 and sys.argv[1] != "--check":
            Path(sys.argv[1] + ".result").write_text(json.dumps(msg), encoding="utf-8")
        print(json.dumps(msg))
        sys.exit(1)
