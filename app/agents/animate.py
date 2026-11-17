"""Animation agent: turns AI still images into short moving clips with an image-to-video model
(LTX-Video 2B) on the local GPU. Runs after the Media agent; the Video agent then uses the clip
instead of the slow zoom/pan on the still.

Built for 8 GB cards (RTX 2070 / Max-Q):
  1. the big T5 text encoder runs on the CPU (system RAM), once per video, then is freed
  2. only then the 2B video transformer + VAE are loaded, with model CPU offload and VAE tiling
Anything that goes wrong (model not downloaded, out of memory, time budget used up) is not fatal:
the affected scenes simply stay as moving-camera stills.

Which scenes are animated (ai_video.scope): "all" | "key" (hook + first scene of each chapter) | "none".
Licence: LTX-Video Open Weights License (Lightricks) - read it before commercial use.
"""
from __future__ import annotations

import gc
import json
import logging
import os
import subprocess
import time
from pathlib import Path

from .. import db, llm
from ..config import resolve_path, settings
from ..util import tool, video_dir
from . import imagegen

log = logging.getLogger("ytm.animate")
NEGATIVE = "worst quality, inconsistent motion, blurry, jittery, distorted, warped, text, watermark, flicker"


def _cfg(key: str, default=None):
    return settings.get(f"ai_video.{key}", default)


def enabled() -> bool:
    return bool(_cfg("enabled", True))


def repo() -> str:
    return str(_cfg("model", "Lightricks/LTX-Video"))


def state_path() -> Path:
    return resolve_path(_cfg("state_file", "./models/ai_video.json"))


def state() -> dict:
    """Written by `fetch-models` after it has found a checkpoint that loads on this machine."""
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def size_for(fmt: str) -> tuple[int, int]:
    w, h = _cfg(f"{fmt}_size", [480, 832] if fmt == "short" else [832, 480])
    return max(64, int(w) // 32 * 32), max(64, int(h) // 32 * 32)   # LTX needs multiples of 32


def n_frames() -> int:
    n = int(_cfg("frames", 97))
    return max(9, (n - 1) // 8 * 8 + 1)                             # ...and 8n+1 frames


def mode() -> dict:
    """How to run the model: fast distilled checkpoint if one was verified, else the base model."""
    st = state()
    tf = st.get("transformer_file") if st else _cfg("transformer_file")
    fast = bool(tf)
    return {"repo": repo(), "transformer_file": tf or None, "fast": fast,
            "steps": int(_cfg("steps", 8) if fast else _cfg("base_steps", 30)),
            "guidance": float(_cfg("guidance", 1.0) if fast else _cfg("base_guidance", 3.0)),
            "dtype": st.get("dtype") or _cfg("dtype", "bfloat16")}


def status() -> dict:
    """For `doctor`."""
    imagegen.setup_hf_cache()
    out = {"enabled": enabled(), "model": repo()}
    try:
        import diffusers  # noqa: F401
        import torch
        out["libs"] = hasattr(diffusers, "LTXImageToVideoPipeline")
        out["cuda"] = bool(torch.cuda.is_available())
        if not out["libs"]:
            out["error"] = "diffusers is too old for LTX-Video (need >= 0.32)"
    except Exception as e:  # noqa: BLE001
        out.update(libs=False, error=str(e)[:150])
        return out
    st = state()
    if Path(resolve_path(repo())).is_dir():
        out["downloaded"] = True
    else:
        try:
            from huggingface_hub import try_to_load_from_cache
            out["downloaded"] = isinstance(try_to_load_from_cache(repo(), "model_index.json"), str) and bool(st)
        except Exception:  # noqa: BLE001
            out["downloaded"] = False
    out["mode"] = "fast (distilled)" if st.get("transformer_file") else "base"
    return out


# --------------------------------------------------------------------------- which scenes
def select(plan: list[dict], sections: list[dict], fmt: str) -> list[int]:
    """Indexes of plan entries (AI stills) that get animated."""
    cand = [i for i, p in enumerate(plan) if p.get("kind") == "image" and p.get("source") == "ai" and p.get("path")]
    scope = str(_cfg(f"scope.{fmt}", "all" if fmt == "short" else "key")).lower()
    if scope in ("none", "off", "false") or not cand:
        return []
    if scope == "key":
        picks, seen = [cand[0]], set()                 # the hook
        for i in cand:
            si = plan[i].get("section", 0)
            chap = (sections[si].get("chapter") if 0 <= si < len(sections) else None) or None
            if chap and chap not in seen:
                seen.add(chap)
                picks.append(i)
        if len(seen) <= 1:                             # no chapters (Shorts): hook + evenly spread highlights
            k = max(1, int(_cfg("key_clips", 4)))
            picks += [cand[round(j * (len(cand) - 1) / max(1, k - 1))] for j in range(k)] if len(cand) > 1 else []
        cand = sorted(set(picks))
    cap = int(_cfg(f"max_clips.{fmt}", 16 if fmt == "short" else 12))
    if cap > 0 and len(cand) > cap:                    # keep the first one, spread the rest evenly
        cand = sorted({cand[round(j * (len(cand) - 1) / (cap - 1))] for j in range(cap)}) if cap > 1 else cand[:1]
    return cand


def motion_prompt(piece: dict, cfg: dict) -> str:
    base = (piece.get("prompt") or "").strip() or "cinematic scene"
    motion = cfg.get("motion_style") or _cfg("motion", "slow cinematic camera movement, subtle natural motion, smooth, high quality")
    return f"{base[:380]}. {motion}"


# --------------------------------------------------------------------------- model
def _dtype(name: str):
    import torch
    return {"bfloat16": torch.bfloat16, "float16": torch.float16, "float32": torch.float32}.get(str(name), torch.bfloat16)


def _src() -> str:
    r = repo()
    return str(resolve_path(r)) if Path(resolve_path(r)).is_dir() else r


def _single_file(name: str) -> str:
    """A checkpoint file: local path, or a file inside the model repo (downloaded/cached by the Hub)."""
    p = resolve_path(name)
    if Path(p).is_file():
        return str(p)
    from huggingface_hub import hf_hub_download
    return hf_hub_download(repo(), name)


def _ram_gb() -> float:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
    except (ValueError, OSError, AttributeError):
        return 0.0


def _encode(prompts: list[str], m: dict) -> list[tuple]:
    """Text embeddings for every prompt, computed once; the T5 encoder is freed before the video
    model loads. T5-XXL (~10 GB in half precision) does not fit next to the transformer in 8 GB."""
    import torch
    from diffusers import LTXImageToVideoPipeline

    where = str(_cfg("text_encoder_device", "auto")).lower()
    cuda = torch.cuda.is_available()
    if where == "auto":
        where = "cpu" if (not cuda or _ram_gb() >= float(_cfg("text_encoder_cpu_min_ram_gb", 22))) else "gpu"
    on_gpu = where == "gpu" and cuda
    tp = LTXImageToVideoPipeline.from_pretrained(_src(), transformer=None, vae=None,
                                                 torch_dtype=_dtype(m["dtype"]) if on_gpu else torch.float32)
    if on_gpu:
        tp.enable_sequential_cpu_offload()             # slow but needs little VRAM and RAM
    out = []
    try:
        with torch.no_grad():
            for p in prompts:
                e = tp.encode_prompt(p, NEGATIVE, do_classifier_free_guidance=m["guidance"] > 1.0,
                                     max_sequence_length=int(_cfg("max_sequence_length", 128)),
                                     device="cuda" if on_gpu else "cpu")
                out.append(tuple(x.detach().to("cpu") if x is not None else None for x in e))
    finally:
        del tp
        imagegen.free_gpu()
    return out


def _load_pipeline(m: dict):
    import torch
    from diffusers import AutoencoderKLLTXVideo, LTXImageToVideoPipeline, LTXVideoTransformer3DModel

    cuda = torch.cuda.is_available()
    dtype = _dtype(m["dtype"]) if cuda else torch.float32
    kw = {"torch_dtype": dtype, "text_encoder": None, "tokenizer": None}
    if m.get("transformer_file"):
        f = _single_file(m["transformer_file"])
        kw["transformer"] = LTXVideoTransformer3DModel.from_single_file(f, torch_dtype=dtype)
        kw["vae"] = AutoencoderKLLTXVideo.from_single_file(f, torch_dtype=dtype)
    pipe = LTXImageToVideoPipeline.from_pretrained(_src(), **kw)
    if cuda:
        pipe.enable_model_cpu_offload()
    else:
        log.warning("CUDA not available - animating on CPU (extremely slow)")
    if hasattr(pipe.vae, "enable_tiling"):
        pipe.vae.enable_tiling()                        # decode the clip in tiles: keeps VRAM low
    pipe.set_progress_bar_config(disable=True)
    return pipe


def _fit(img, w: int, h: int):
    """Centre-crop to the target aspect, then resize."""
    from PIL import Image
    iw, ih = img.size
    s = max(w / iw, h / ih)
    img = img.resize((max(w, round(iw * s)), max(h, round(ih * s))), Image.LANCZOS)
    l, t = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((l, t, l + w, t + h))


def write_mp4(frames, fps: int, out: Path) -> None:
    """frames: float array [F, H, W, 3] in 0..1 -> H.264 (near-lossless; it is re-encoded later)."""
    import numpy as np
    arr = (np.clip(np.asarray(frames, dtype="float32"), 0, 1) * 255).round().astype("uint8")
    f, h, w, _ = arr.shape
    tmp = out.with_suffix(".part.mp4")
    p = subprocess.run([tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                        "-s", f"{w}x{h}", "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "12",
                        "-pix_fmt", "yuv420p", str(tmp)], input=arr.tobytes(), capture_output=True, timeout=600)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg could not write the clip: {p.stderr.decode(errors='replace')[:300]}")
    tmp.replace(out)


def _check(frames) -> None:
    import numpy as np
    arr = np.asarray(frames, dtype="float32")
    if not np.isfinite(arr).all():
        raise RuntimeError("model produced invalid frames (NaN) - try ai_video.dtype: float16 or float32")
    if float(arr.std()) < 0.004:
        raise RuntimeError("model produced blank frames - try another ai_video.dtype")


def generate(jobs: list[dict], fmt: str, on_clip=None, deadline: float | None = None,
             m: dict | None = None, size: tuple[int, int] | None = None, frames: int | None = None) -> list[dict]:
    """jobs: [{"image", "prompt", "out", "seed"}] -> same dicts with "ok" / "error" / "seconds".
    Existing clips are kept, so a retried video resumes."""
    for j in jobs:
        j["ok"] = Path(j["out"]).exists()
    todo = [j for j in jobs if not j["ok"]]
    if not todo:
        return jobs
    imagegen.setup_hf_cache()
    llm.unload()
    from ..gpu import setup_cuda_libs
    setup_cuda_libs()
    import torch
    from PIL import Image

    m = m or mode()
    w, h = size or size_for(fmt)
    frames, fps = frames or n_frames(), int(_cfg("fps", 24))
    t0 = time.time()
    embeds = _encode([j["prompt"] for j in todo], m)
    log.info("text encoded for %s clips in %.0fs", len(todo), time.time() - t0)
    pipe = _load_pipeline(m)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    try:
        for n, (j, e) in enumerate(zip(todo, embeds), 1):
            if deadline and time.time() > deadline:
                j["error"] = "time budget used up"
                continue
            t1 = time.time()
            try:
                pe, pm, ne, nm = e
                kw = {"prompt_embeds": pe.to(dev), "prompt_attention_mask": pm.to(dev)}
                if ne is not None:
                    kw.update(negative_prompt_embeds=ne.to(dev), negative_prompt_attention_mask=nm.to(dev))
                gen = torch.Generator(device="cpu").manual_seed(int(j["seed"]))
                res = pipe(image=_fit(Image.open(j["image"]).convert("RGB"), w, h), width=w, height=h, num_frames=frames,
                           frame_rate=fps, num_inference_steps=m["steps"], guidance_scale=m["guidance"],
                           decode_timestep=float(_cfg("decode_timestep", 0.05)),
                           decode_noise_scale=float(_cfg("decode_noise_scale", 0.025)),
                           generator=gen, output_type="np", **kw).frames[0]
                _check(res)
                Path(j["out"]).parent.mkdir(parents=True, exist_ok=True)
                write_mp4(res, fps, Path(j["out"]))
                j.update(ok=True, seconds=round(time.time() - t1), frames=int(len(res)), fps=fps)
                log.info("clip %s/%s in %.0fs", n, len(todo), time.time() - t1)
            except torch.cuda.OutOfMemoryError as ex:  # type: ignore[attr-defined]
                imagegen.free_gpu()
                j["error"] = f"out of GPU memory: {ex}"[:300]
                log.error("clip %s: %s", n, j["error"])
            except Exception as ex:  # noqa: BLE001
                j["error"] = str(ex)[:300]
                log.error("clip %s failed: %s", n, ex)
            if on_clip:
                on_clip(n, len(todo), j)
    finally:
        del pipe
        imagegen.free_gpu()
    return jobs


# --------------------------------------------------------------------------- download + self-configuration
def download() -> dict:
    """`./ytm.sh fetch-models`: fetch the weights (~25 GB, mostly the T5 text encoder), then find a
    checkpoint that really loads with the installed libraries and remember it in models/ai_video.json:
    the fast distilled files first, the slower base model as the safe fallback."""
    imagegen.setup_hf_cache()
    import diffusers
    import torch
    from huggingface_hub import hf_hub_download, snapshot_download

    t0 = time.time()
    r = repo()
    local = Path(resolve_path(r)).is_dir()
    if not local:
        snapshot_download(r, allow_patterns=["model_index.json", "scheduler/*", "tokenizer/*", "text_encoder/*"])
    chosen, errors = None, {}
    for name in ([] if local else list(_cfg("distilled_files", []) or [])):
        try:
            f = hf_hub_download(r, name)
            from diffusers import AutoencoderKLLTXVideo, LTXVideoTransformer3DModel
            t = LTXVideoTransformer3DModel.from_single_file(f, torch_dtype=torch.bfloat16)
            v = AutoencoderKLLTXVideo.from_single_file(f, torch_dtype=torch.bfloat16)
            del t, v
            gc.collect()
            chosen = name
            break
        except Exception as e:  # noqa: BLE001 - try the next candidate
            errors[name] = str(e)[:200]
            log.warning("checkpoint %s not usable: %s", name, str(e)[:200])
    if not chosen and not local:
        snapshot_download(r, allow_patterns=["transformer/*", "vae/*"])
    st = {"model": r, "transformer_file": chosen, "dtype": _cfg("dtype", "bfloat16"),
          "diffusers": diffusers.__version__, "checked_at": db.now_iso(), **({"skipped": errors} if errors else {})}
    state_path().parent.mkdir(parents=True, exist_ok=True)
    state_path().write_text(json.dumps(st, indent=1), encoding="utf-8")
    test = self_test() if _cfg("self_test", True) else {"skipped": True}
    return {"model": r, "mode": "fast (distilled)" if chosen else "base", "file": chosen,
            "self_test": test, "seconds": round(time.time() - t0)}


def self_test() -> dict:
    """Generate one tiny clip end to end. Tries the configured precision first, then the others,
    and stores the first one that gives valid frames (older GPUs differ in what they handle)."""
    import tempfile

    from PIL import Image

    tried = {}
    first = _cfg("dtype", "bfloat16")
    with tempfile.TemporaryDirectory() as tmp:
        img = Path(tmp) / "in.png"
        Image.linear_gradient("L").resize((416, 256)).convert("RGB").save(img)
        for name in [first] + [d for d in ("bfloat16", "float16") if d != first]:
            job = {"image": str(img), "prompt": "slow camera push-in over a calm landscape", "out": str(Path(tmp) / f"{name}.mp4"), "seed": 1}
            t0 = time.time()
            try:
                generate([job], "short", m={**mode(), "dtype": name}, size=(256, 416), frames=9)
            except Exception as e:  # noqa: BLE001
                job["error"] = str(e)[:300]
            if job.get("ok"):
                state_path().write_text(json.dumps({**state(), "dtype": name, "self_test": "ok"}, indent=1), encoding="utf-8")
                return {"ok": True, "dtype": name, "seconds": round(time.time() - t0)}
            tried[name] = job.get("error")
    return {"ok": False, "errors": tried}


# --------------------------------------------------------------------------- pipeline step
def run(video_id: int) -> dict:
    v = db.get_video(video_id)
    ch = db.get_channel(v["channel_id"])
    cfg = ch.get("config") or {}
    fmt = v["format"]
    d = video_dir(video_id)
    marker = d / "animate.json"

    def finish(res: dict) -> dict:
        marker.write_text(json.dumps(res, indent=1), encoding="utf-8")
        return res

    if not enabled() or cfg.get("animate") is False:
        return finish({"skipped": "disabled"})
    plan = json.loads((d / "plan.json").read_text(encoding="utf-8"))
    picks = select(plan, v["script"]["sections"], fmt)
    if not picks:
        return finish({"skipped": "no AI stills to animate", "clips": 0})
    st = status()
    if not (st.get("libs") and st.get("downloaded")):
        why = st.get("error") or "model not downloaded - run ./ytm.sh fetch-models"
        db.log_event(video_id, "animate_skipped", why, channel_id=v["channel_id"])
        return finish({"skipped": why, "clips": 0})

    jobs = [{"index": i, "image": plan[i]["path"], "prompt": motion_prompt(plan[i], cfg),
             "out": str(d / "clips" / f"clip_{i:03d}.mp4"), "seed": imagegen.seed_for(video_id, 1000 + i)} for i in picks]
    budget = float(_cfg(f"max_minutes.{fmt}", 150 if fmt == "short" else 240))
    t0 = time.time()

    def on_clip(n, total, j):
        db.log_event(video_id, "animate", f"clip {n}/{total} " + (f"done in {j.get('seconds')}s" if j.get("ok") else f"skipped: {j.get('error')}"),
                     channel_id=v["channel_id"])

    error = None
    try:
        generate(jobs, fmt, on_clip=on_clip, deadline=t0 + budget * 60 if budget > 0 else None)
    except Exception as e:  # noqa: BLE001 - never fail the video because of animation
        error = f"animation unavailable: {e}"[:400]
        log.error(error)
        db.log_event(video_id, "animate_failed", error, channel_id=v["channel_id"])
    done = 0
    for j in jobs:
        if Path(j["out"]).exists():
            plan[j["index"]].update(clip=j["out"], clip_fps=int(j.get("fps") or _cfg("fps", 24)), clip_frames=int(j.get("frames") or n_frames()))
            done += 1
    (d / "plan.json").write_text(json.dumps(plan, indent=1), encoding="utf-8")
    db.ex("DELETE FROM assets WHERE video_id=? AND kind='ai_video'", (video_id,))   # retry-safe
    if done:
        db.ex("INSERT INTO assets(video_id, kind, path, provider, license, meta_json, created_at) VALUES (?,?,?,?,?,?,?)",
              (video_id, "ai_video", str(d / "clips"), "ltx-video", "LTX-Video Open Weights License",
               db.jdump({"clips": done, "mode": mode()["fast"] and "distilled" or "base"}), db.now_iso()))
    first_err = error or next((j["error"] for j in jobs if j.get("error")), None)
    return finish({"clips": done, "planned": len(jobs), "stills": sum(1 for p in plan if p.get("kind") == "image") - done,
                   "minutes": round((time.time() - t0) / 60, 1), **({"note": first_err} if first_err else {})})
