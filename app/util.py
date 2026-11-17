"""Shared helpers: subprocess, ffmpeg/ffprobe, timestamps, downloads, text cleanup."""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

import requests

from .config import data_dir, settings

log = logging.getLogger("ytm")
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: no console pop-ups when run hidden


def tool(name: str) -> str:
    custom = settings.get(f"tools.{name}")
    if custom:
        return custom
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"'{name}' was not found on PATH. Run install.sh (sudo apt install ffmpeg) or set tools.{name} in settings.yaml")
    return found


def run(cmd, cwd=None, timeout=None) -> subprocess.CompletedProcess:
    cmd = [str(c) for c in cmd]
    p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout, creationflags=_NO_WINDOW)
    if p.returncode != 0:
        raise RuntimeError(f"{Path(cmd[0]).name} exited {p.returncode}: {(p.stderr or p.stdout)[-1500:]}")
    return p


def ffmpeg(*args, cwd=None, timeout=3600) -> subprocess.CompletedProcess:
    return run([tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *args], cwd=cwd, timeout=timeout)


def probe_duration(path) -> float:
    p = run([tool("ffprobe"), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)], timeout=60)
    return float(p.stdout.strip())


def video_dir(video_id: int) -> Path:
    d = data_dir() / "videos" / f"{int(video_id):05d}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_dir(name: str) -> Path:
    d = data_dir() / "cache" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def ts_srt(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def ts_ass(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ts_chapter(t: float) -> str:
    t = int(t)
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def download(url: str, dest: Path, headers=None, max_mb: float | None = None, timeout=60) -> Path:
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with requests.get(url, headers=headers, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            size = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    size += len(chunk)
                    if max_mb and size > max_mb * 1024 * 1024:
                        raise ValueError(f"download exceeds {max_mb} MB")
                    f.write(chunk)
        tmp.replace(dest)
        return dest
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def hex_to_ass(hex_color: str, alpha: str = "00") -> str:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    return f"&H{alpha}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def clean_spoken(text: str) -> str:
    """Strip markdown, stage directions and speaker labels so TTS reads clean prose."""
    text = re.sub(r"\[[^\]]*\]", " ", text)                       # [music], [b-roll]
    text = re.sub(r"(?im)^\s*(narrator|host|voice ?over|vo)\s*:\s*", "", text)
    text = re.sub(r"[*_#`>~]|https?://\S+", " ", text)
    text = re.sub(r"[\U0001F300-\U0001FAFF☀-➿]", "", text)  # emoji
    return re.sub(r"\s+", " ", text).strip()
