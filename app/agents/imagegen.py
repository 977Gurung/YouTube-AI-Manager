"""AI image generation on the local GPU: Stable Diffusion XL + SDXL-Lightning (8 steps).

Used by the Media Agent for sections with no matching b-roll or stock clip.
Tuned for 8 GB cards (RTX 2070 / Max-Q): fp16 weights, model CPU offload (only the
part that is running sits in VRAM), fp16-safe VAE, and everything freed afterwards so
Whisper and NVENC get the GPU back.

Licences: SDXL base 1.0 = CreativeML Open RAIL++-M; SDXL-Lightning = OpenRAIL++;
sdxl-vae-fp16-fix = MIT. Commercial use is allowed within the RAIL use restrictions.
"""
from __future__ import annotations

import gc
import hashlib
import logging
import os
import time
from pathlib import Path

from .. import llm
from ..config import resolve_path, settings

log = logging.getLogger("ytm.imagegen")


def _cfg(key: str, default=None):
    return settings.get(f"ai_images.{key}", default)


def enabled() -> bool:
    return bool(_cfg("enabled", True))


def setup_hf_cache() -> None:
    """Keep model downloads inside the project (models/hf) unless HF_HOME is already set."""
    os.environ.setdefault("HF_HOME", str(resolve_path(_cfg("cache_dir", "./models/hf"))))


def size_for(fmt: str) -> tuple[int, int]:
    w, h = _cfg(f"{fmt}_size", [768, 1344] if fmt == "short" else [1344, 768])
    return int(w) // 8 * 8, int(h) // 8 * 8


def status() -> dict:
    """For `doctor`: are the libraries installed and the weights downloaded?"""
    setup_hf_cache()
    out = {"enabled": enabled()}
    try:
        import diffusers  # noqa: F401
        import torch
        out["libs"] = True
        out["cuda"] = bool(torch.cuda.is_available())
    except Exception as e:  # noqa: BLE001
        out.update(libs=False, error=str(e)[:150])
        return out
    model = _cfg("model", "stabilityai/stable-diffusion-xl-base-1.0")
    if Path(resolve_path(model)).is_dir():
        out["downloaded"] = True
    else:
        try:
            from huggingface_hub import try_to_load_from_cache
            hit = try_to_load_from_cache(model, "model_index.json")
            out["downloaded"] = isinstance(hit, str)
        except Exception:  # noqa: BLE001
            out["downloaded"] = False
    return out


def download() -> dict:
    """Pre-fetch all weights (~7.5 GB) so the first video doesn't stall. `./ytm.sh fetch-models`."""
    setup_hf_cache()
    from diffusers import DiffusionPipeline
    from huggingface_hub import hf_hub_download

    model = _cfg("model", "stabilityai/stable-diffusion-xl-base-1.0")
    t0 = time.time()
    if not Path(resolve_path(model)).is_dir():
        DiffusionPipeline.download(model, variant="fp16", use_safetensors=True)
    if _cfg("vae"):
        for f in ("config.json", "diffusion_pytorch_model.safetensors"):
            hf_hub_download(_cfg("vae"), f)
    if _cfg("lightning_repo") and _cfg("lightning_file"):
        hf_hub_download(_cfg("lightning_repo"), _cfg("lightning_file"))
    return {"model": model, "seconds": round(time.time() - t0)}


# --------------------------------------------------------------------------- prompts
def build_prompts(sections: list[dict], counts: list[int], channel: dict, cfg: dict) -> list[list[str]]:
    """One prompt per image. The local LLM turns each section's narration + visual hint into
    concrete scene descriptions; if that fails, fall back to visual words + channel style."""
    style = cfg.get("visual_style") or _cfg("style", "cinematic photograph, natural light, rich detail")
    fallback = [[f"{s['visual']}, {style}"] * n for s, n in zip(sections, counts)]
    if not _cfg("prompt_with_llm", True) or not any(counts):
        return fallback
    out = [list(f) for f in fallback]
    todo = [i for i, n in enumerate(counts) if n]
    for start in range(0, len(todo), 10):  # 10 sections per call keeps the reply small for a 7B model
        chunk = todo[start:start + 10]
        listing = "\n".join(f'{i}. needs {counts[i]} image(s) | visual hint: "{sections[i]["visual"]}" | narration: '
                            f'"{sections[i]["text"][:300]}"' for i in chunk)
        try:
            data = llm.chat_json(
                f'You write prompts for an AI image generator that illustrates narration for the YouTube channel '
                f'"{channel["name"]}" ({channel.get("niche", "")}). Respond with JSON only.',
                f"""For each numbered section, write the requested number of image prompts.
Rules for every prompt:
- One concrete, visual scene in 15-30 words: subject, setting, lighting, camera angle.
- Must match what the narration says at that moment; consecutive prompts in a section show different shots.
- No words, letters, signs, logos, brand names, celebrities or real named people. No gore or nudity.
- End every prompt with: "{style}"

Sections:
{listing}

Return JSON: {{"sections": [{{"i": <section number>, "prompts": ["...", "..."]}}]}}""", temperature=0.6)
        except Exception as e:  # noqa: BLE001
            log.warning("prompt expansion failed, using visual hints: %s", e)
            continue
        for item in data.get("sections") or []:
            try:
                i = int(item.get("i"))
            except (TypeError, ValueError):
                continue
            if i not in chunk:
                continue
            ps = [str(p).strip()[:400] for p in (item.get("prompts") or []) if str(p).strip()]
            if ps:
                out[i] = (ps * counts[i])[:counts[i]]
    llm.unload()  # free VRAM for Stable Diffusion
    return out


# --------------------------------------------------------------------------- generation
def _load_pipeline():
    import torch
    from diffusers import AutoencoderKL, EulerDiscreteScheduler, StableDiffusionXLPipeline

    model = _cfg("model", "stabilityai/stable-diffusion-xl-base-1.0")
    src = str(resolve_path(model)) if Path(resolve_path(model)).is_dir() else model
    cuda = torch.cuda.is_available()
    dtype = torch.float16 if cuda else torch.float32
    kw = {"torch_dtype": dtype, "use_safetensors": True}
    if cuda and not Path(src).is_dir():
        kw["variant"] = "fp16"
    if _cfg("vae"):
        kw["vae"] = AutoencoderKL.from_pretrained(_cfg("vae"), torch_dtype=dtype)
    pipe = StableDiffusionXLPipeline.from_pretrained(src, **kw)
    if _cfg("lightning_repo") and _cfg("lightning_file"):
        from huggingface_hub import hf_hub_download
        pipe.load_lora_weights(hf_hub_download(_cfg("lightning_repo"), _cfg("lightning_file")))
        pipe.fuse_lora()
        pipe.unload_lora_weights()  # weights are fused; drop the adapter copies
        pipe.scheduler = EulerDiscreteScheduler.from_config(pipe.scheduler.config, timestep_spacing="trailing")
    if cuda:
        pipe.enable_model_cpu_offload()  # only the running component lives in VRAM (8 GB safe)
        # VAE slicing moved from the pipeline to the VAE in newer diffusers; optional either way
        for obj, name in ((pipe.vae, "enable_slicing"), (pipe, "enable_vae_slicing")):
            if hasattr(obj, name):
                getattr(obj, name)()
                break
    else:
        log.warning("CUDA not available - generating images on CPU (very slow)")
    pipe.set_progress_bar_config(disable=True)
    return pipe, cuda


def free_gpu() -> None:
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
    except Exception:  # noqa: BLE001
        pass


def generate(jobs: list[dict], fmt: str) -> list[dict]:
    """jobs: [{"prompt": str, "path": Path, "seed": int}] -> same dicts with "ok"/"error".
    Already-existing files are kept (a retried video resumes)."""
    todo = [j for j in jobs if not Path(j["path"]).exists()]
    for j in jobs:
        j["ok"] = Path(j["path"]).exists()
    if not todo:
        return jobs
    setup_hf_cache()
    llm.unload()
    from ..gpu import setup_cuda_libs
    setup_cuda_libs()
    import torch

    w, h = size_for(fmt)
    steps = int(_cfg("steps", 8))
    guidance = float(_cfg("guidance", 0.0))
    pipe, cuda = _load_pipeline()
    try:
        for n, j in enumerate(todo, 1):
            t0 = time.time()
            try:
                gen = torch.Generator(device="cpu").manual_seed(int(j["seed"]))
                img = pipe(prompt=j["prompt"], width=w, height=h, num_inference_steps=steps,
                           guidance_scale=guidance, generator=gen).images[0]
                tmp = Path(j["path"]).with_suffix(".part.png")
                img.save(tmp)
                tmp.replace(j["path"])
                j["ok"] = True
                log.info("image %s/%s in %.1fs", n, len(todo), time.time() - t0)
            except torch.cuda.OutOfMemoryError as e:  # type: ignore[attr-defined]
                free_gpu()
                j["error"] = f"out of GPU memory: {e}"[:300]
                log.error("image %s: %s", n, j["error"])
            except Exception as e:  # noqa: BLE001
                j["error"] = str(e)[:300]
                log.error("image %s failed: %s", n, e)
    finally:
        del pipe
        free_gpu()
    return jobs


def seed_for(video_id: int, index: int) -> int:
    return int(hashlib.sha1(f"{video_id}:{index}".encode()).hexdigest()[:8], 16)
