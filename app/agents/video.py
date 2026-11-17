"""Video agent: assembles the final MP4 with ffmpeg (NVENC on RTX GPUs, libx264 fallback).
1) each planned clip -> normalised segment (scaled/cropped to 9:16 or 16:9, exact duration)
2) concat segments -> visuals.mp4
3) visuals + voice + optional ducked music + burned captions + loudness normalisation -> final.mp4
ffmpeg runs with cwd = the video folder so subtitle paths never need filter escaping."""
from __future__ import annotations

import json
import logging
import random
from pathlib import Path

from .. import db
from ..config import resolve_path, settings
from ..gpu import encoder_args
from ..util import ffmpeg, probe_duration, video_dir

log = logging.getLogger("ytm.video")


def _segment(src: str | None, dur: float, w: int, h: int, fps: int, out: str, cwd) -> None:
    vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps={fps},format=yuv420p"
    if src:
        try:
            ffmpeg("-stream_loop", "-1", "-i", src, "-t", f"{dur:.3f}", "-vf", vf, "-an", *encoder_args(), out, cwd=cwd)
            return
        except RuntimeError as e:
            log.warning("clip %s unusable (%s) - using placeholder", src, str(e)[:200])
    ffmpeg("-f", "lavfi", "-i", f"color=c=0x14181F:s={w}x{h}:r={fps}:d={dur:.3f}",
           "-vf", "format=yuv420p", *encoder_args(), out, cwd=cwd)


def _still(src: str, dur: float, w: int, h: int, fps: int, motion: str, out: str, cwd) -> None:
    """A still image with slow zoom or pan ("Ken Burns"). The image is first scaled to 2x the frame
    so zoompan's whole-pixel steps land on half pixels of the output - smooth, no jitter."""
    n = max(2, round(dur * fps))
    zmax = float(settings.get("ai_images.motion_zoom", 0.12))
    prog = f"on/{n - 1}"
    if motion == "out":
        z, x, y = f"{1 + zmax}-{zmax}*{prog}", "iw/2-iw/zoom/2", "ih/2-ih/zoom/2"
    elif motion == "right":
        z, x, y = f"{1 + zmax}", f"(iw-iw/zoom)*{prog}", "ih/2-ih/zoom/2"
    elif motion == "left":
        z, x, y = f"{1 + zmax}", f"(iw-iw/zoom)*(1-{prog})", "ih/2-ih/zoom/2"
    else:  # "in"
        z, x, y = f"1+{zmax}*{prog}", "iw/2-iw/zoom/2", "ih/2-ih/zoom/2"
    vf = (f"scale={2 * w}:{2 * h}:force_original_aspect_ratio=increase,crop={2 * w}:{2 * h},"
          f"zoompan=z='{z}':x='{x}':y='{y}':d={n}:s={w}x{h}:fps={fps},setsar=1,format=yuv420p")
    ffmpeg("-i", src, "-vf", vf, "-frames:v", str(n), "-an", *encoder_args(), out, cwd=cwd)


def _animated(src: str, frames: int, src_fps: int, dur: float, w: int, h: int, fps: int, out: str, cwd) -> None:
    """An AI-generated clip (low resolution, ~4 s) -> full-size segment of exactly `dur` seconds.
    Slightly slowed down to cover the scene; if it is still too short it plays forward-then-backward
    (seamless, no jump cut). Frames are blended up to the project frame rate before upscaling."""
    acfg = settings.get("ai_video", {}) or {}
    clip_len = frames / src_fps
    k = min(max(1.0, dur / clip_len), float(acfg.get("max_slowdown", 1.35)))
    interp = str(acfg.get("interpolate", "blend")).lower()
    smooth = f"minterpolate=fps={fps}:mi_mode={interp}" if interp in ("blend", "mci") else f"fps={fps}"
    sharp = float(acfg.get("sharpen", 0.5))
    vf = (f"split[a][b];[b]reverse[r];[a][r]concat=n=2:v=1,loop=loop=-1:size={2 * frames}:start=0,"
          f"setpts=N/({src_fps}*TB)*{k:.4f},{smooth},"
          f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h},"
          + (f"unsharp=5:5:{sharp}," if sharp > 0 else "") + "setsar=1,format=yuv420p")
    ffmpeg("-i", src, "-vf", vf, "-t", f"{dur:.3f}", "-an", *encoder_args(), out, cwd=cwd)


def _pick_music(cfg: dict):
    folder = resolve_path(cfg.get("music_dir") or settings.get("video.music_dir", "./assets/music"))
    tracks = [p for p in folder.glob("*") if p.suffix.lower() in (".mp3", ".wav", ".m4a", ".ogg")] if folder.exists() else []
    return random.choice(tracks) if tracks else None


def run(video_id: int) -> dict:
    v = db.get_video(video_id)
    cfg = db.get_channel(v["channel_id"]).get("config") or {}
    fmt = settings.get(f"video.{v['format']}", {})
    w, h = int(fmt.get("width", 1080)), int(fmt.get("height", 1920))
    fps = int(settings.get("video.fps", 30))
    d = video_dir(video_id)
    seg_dir = d / "segments"
    seg_dir.mkdir(exist_ok=True)
    plan = json.loads((d / "plan.json").read_text(encoding="utf-8"))
    total = probe_duration(d / "voice.wav") + 0.6  # small tail so the last word isn't clipped
    plan[-1]["duration"] += total - sum(p["duration"] for p in plan)

    names, animated = [], 0
    for i, p in enumerate(plan):
        name = f"segments/seg_{i:04d}.mp4"
        dur = max(0.3, p["duration"])
        if p.get("kind") == "image" and p.get("clip") and Path(p["clip"]).exists():
            try:
                _animated(p["clip"], int(p.get("clip_frames") or 97), int(p.get("clip_fps") or 24), dur, w, h, fps, name, d)
                names.append(name)
                animated += 1
                continue
            except RuntimeError as e:
                log.warning("animated clip %s unusable (%s) - using the still", p["clip"], str(e)[:200])
        if p.get("kind") == "image" and p.get("path"):
            try:
                _still(p["path"], dur, w, h, fps, p.get("motion", "in"), name, d)
                names.append(name)
                continue
            except RuntimeError as e:
                log.warning("image %s unusable (%s) - using placeholder", p["path"], str(e)[:200])
                p["path"] = None
        _segment(p["path"], dur, w, h, fps, name, d)
        names.append(name)
    (d / "concat.txt").write_text("".join(f"file '{n}'\n" for n in names), encoding="utf-8")
    ffmpeg("-f", "concat", "-safe", "0", "-i", "concat.txt", "-c", "copy", "visuals.mp4", cwd=d)

    inputs = ["-i", "visuals.mp4", "-i", "voice.wav"]
    music = _pick_music(cfg)
    vol = float(cfg.get("music_volume", settings.get("video.music_volume", 0.08)))
    if music:
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        audio = ("[1:a]aformat=sample_rates=48000:channel_layouts=stereo,asplit=2[vo][key];"
                 f"[2:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={vol}[mu];"
                 "[mu][key]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=400[duck];"
                 "[vo][duck]amix=inputs=2:duration=first:normalize=0,")
    else:
        audio = "[1:a]aformat=sample_rates=48000:channel_layouts=stereo,"
    audio += "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[a]"
    fonts = ":fontsdir=fonts" if (d / "fonts").is_dir() else ""
    video = f"[0:v]ass=captions.ass{fonts}[v]" if v.get("captions_ass") else "[0:v]null[v]"
    ffmpeg(*inputs, "-filter_complex", f"{video};{audio}", "-map", "[v]", "-map", "[a]",
           *encoder_args(final=True), "-r", str(fps), "-c:a", "aac", "-b:a", "192k",
           "-t", f"{total:.3f}", "-movflags", "+faststart", "final.part.mp4", cwd=d)

    final = d / "final.mp4"
    (d / "final.part.mp4").replace(final)
    dur = probe_duration(final)
    max_short = float(settings.get("video.short.max_seconds", 60))
    warn = f"Short is {dur:.0f}s (> {max_short:.0f}s target)" if v["format"] == "short" and dur > max_short else None
    db.update_video(video_id, video_path=str(final), duration_sec=round(dur, 2))
    for p in seg_dir.glob("*.mp4"):
        p.unlink(missing_ok=True)
    return {"path": str(final), "duration": round(dur, 2), "music": music.name if music else None, "animated": animated, "warning": warn}
