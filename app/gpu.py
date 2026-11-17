"""GPU helpers: CUDA library discovery, nvidia-smi stats, NVENC detection."""
from __future__ import annotations

import ctypes
import functools
import logging
import os
import shutil
import site
from pathlib import Path

from .config import settings
from .util import run, tool

log = logging.getLogger("ytm.gpu")
_libs_done = False


def _nvidia_dirs(sub: str) -> list[Path]:
    out = []
    for root in set(site.getsitepackages()):
        nv = Path(root) / "nvidia"
        if nv.is_dir():
            out += sorted(p for p in nv.glob(f"*/{sub}") if p.is_dir())
    return out


def setup_cuda_libs() -> None:
    """faster-whisper (CTranslate2) needs cuBLAS + cuDNN 9 from CUDA 12. install.sh pip-installs
    nvidia-cublas-cu12 / nvidia-cudnn-cu12 into the venv. scripts/env.sh already puts them on
    LD_LIBRARY_PATH; as a safety net we also preload them here (RTLD_GLOBAL) so later dlopen()
    calls by soname resolve to the already-loaded copies."""
    global _libs_done
    if _libs_done:
        return
    _libs_done = True
    if os.name == "nt":
        for b in _nvidia_dirs("bin"):
            try:
                os.add_dll_directory(str(b))
            except (OSError, AttributeError):
                pass
        return
    # Only what CTranslate2 needs. Never preload everything: libnvblas (shipped with torch's CUDA
    # wheels) hijacks CPU BLAS calls and aborts the process ("CPU Blas library need to be provided").
    wanted = ("libcublasLt.so", "libcublas.so", "libcudnn")
    libs = [f for d in _nvidia_dirs("lib") for f in sorted(d.glob("lib*.so*"))
            if f.name.startswith(wanted) and "nvblas" not in f.name]
    pending = libs
    for _ in range(3):  # a few passes so libraries load after their dependencies
        failed = []
        for f in pending:
            try:
                ctypes.CDLL(str(f), mode=ctypes.RTLD_GLOBAL)
            except OSError:
                failed.append(f)
        if not failed or len(failed) == len(pending):
            break
        pending = failed


setup_cuda_dlls = setup_cuda_libs  # backwards-compatible name


def nvidia_smi() -> dict:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {"ok": False, "error": "nvidia-smi not found"}
    try:
        p = run([exe, "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu",
                 "--format=csv,noheader,nounits"], timeout=10)
        name, used, total, util, temp = [x.strip() for x in p.stdout.strip().splitlines()[0].split(",")]
        return {"ok": True, "name": name, "mem_used_mb": int(used), "mem_total_mb": int(total),
                "util_pct": int(util), "temp_c": int(temp)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}


@functools.lru_cache(maxsize=1)
def nvenc_ok() -> bool:
    if settings.get("gpu.video_encoder", "h264_nvenc") != "h264_nvenc":
        return False
    try:
        run([tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
             "color=c=black:s=256x256:d=0.2", "-c:v", "h264_nvenc", "-f", "null", "-"], timeout=30)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("NVENC unavailable, falling back to libx264: %s", str(e)[:200])
        return False


def encoder_args(final: bool = False) -> list[str]:
    if nvenc_ok():
        cq = "21" if final else "19"
        return ["-c:v", "h264_nvenc", "-preset", settings.get("gpu.nvenc_preset", "p5"), "-rc", "vbr",
                "-cq", cq, "-b:v", "0", "-maxrate", "20M", "-bufsize", "40M", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "veryfast" if not final else "medium",
            "-crf", "19" if not final else "21", "-pix_fmt", "yuv420p"]
