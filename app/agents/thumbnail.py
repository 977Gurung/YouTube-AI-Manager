"""Thumbnail agent: grabs a strong frame from the render, darkens it and sets bold
brand-coloured text (Pillow). 1280x720 for long-form, 1080x1920 for Shorts. Kept < 2 MB (API limit)."""
from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from .. import db
from ..config import resolve_path, settings
from ..util import ffmpeg, video_dir

log = logging.getLogger("ytm.thumbnail")


def _font(size: int):
    for p in (settings.get("thumbnail.font_path"), settings.get("thumbnail.fallback_font"),
              "./assets/fonts/Anton-Regular.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if p and Path(resolve_path(p)).exists():
            return ImageFont.truetype(str(resolve_path(p)), size)
    return ImageFont.load_default(size=size)


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    return lines + ([cur] if cur else [])


def run(video_id: int) -> dict:
    v = db.get_video(video_id)
    cfg = db.get_channel(v["channel_id"]).get("config") or {}
    brand = cfg.get("brand", {}) or {}
    short = v["format"] == "short"
    W, H = (1080, 1920) if short else (1280, 720)
    d = video_dir(video_id)
    frame = d / "thumb_frame.jpg"
    t = max(0.5, float(v["duration_sec"] or 4) * 0.3)
    ffmpeg("-ss", f"{t:.2f}", "-i", v["video_path"], "-frames:v", "1", "-q:v", "2", str(frame))

    img = Image.open(frame).convert("RGB")
    scale = max(W / img.width, H / img.height)
    img = img.resize((int(img.width * scale) + 1, int(img.height * scale) + 1), Image.LANCZOS)
    left, top = (img.width - W) // 2, (img.height - H) // 2
    img = img.crop((left, top, left + W, top + H))
    img = ImageEnhance.Contrast(ImageEnhance.Brightness(img).enhance(0.55)).enhance(1.25)
    img = img.filter(ImageFilter.GaussianBlur(1.2))

    draw = ImageDraw.Draw(img)
    text = (v["script"].get("thumbnail_text") or v["topic"]).upper()[:40]
    primary, fg = brand.get("primary", "#FFD400"), brand.get("text", "#FFFFFF")
    max_w = int(W * (0.9 if short else 0.62))
    size = int(H * (0.11 if short else 0.2))
    font = _font(size)
    lines = _wrap(draw, text, font, max_w)
    while (len(lines) > 3 or size * 1.1 * len(lines) > H * 0.7) and size > 30:
        size = int(size * 0.9)
        font = _font(size)
        lines = _wrap(draw, text, font, max_w)

    x0 = int(W * 0.05)
    y = (H - int(size * 1.1 * len(lines))) // 2
    for i, line in enumerate(lines):
        x = (W - draw.textlength(line, font=font)) // 2 if short else x0
        draw.text((x, y), line, font=font, fill=primary if i == len(lines) - 1 else fg,
                  stroke_width=max(4, size // 12), stroke_fill="black")
        y += int(size * 1.1)
    bar = int(H * 0.025)
    draw.rectangle([0, H - bar, W, H], fill=primary)

    out = d / "thumbnail.jpg"
    for q in (90, 82, 72, 60):
        img.save(out, "JPEG", quality=q, optimize=True)
        if out.stat().st_size < 1_950_000:
            break
    frame.unlink(missing_ok=True)
    db.update_video(video_id, thumbnail_path=str(out))
    return {"path": str(out), "text": text}
