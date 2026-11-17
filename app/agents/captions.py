"""Captions agent: word-level timestamps from faster-whisper on the GPU (fp16 on RTX 20xx),
falling back to CPU and finally to script-proportional timing. Writes SRT (for upload)
and ASS (burned in). Burned-in styles:
  pop     - words appear one by one in time with the voice; the spoken word pops (scales in) in the brand
            colour, and key words (numbers, acronyms, power words, title words) get a bigger, tilted,
            second-colour pop and stay highlighted
  karaoke - the whole phrase is visible and the spoken word is highlighted
  plain   - classic subtitles"""
from __future__ import annotations

import gc
import logging
import re
import shutil
from pathlib import Path

from .. import db, llm
from ..config import resolve_path, settings
from ..gpu import setup_cuda_libs
from ..util import hex_to_ass, ts_ass, ts_srt, video_dir

log = logging.getLogger("ytm.captions")


def _whisper_words(path: str, language: str) -> list[dict]:
    setup_cuda_libs()
    from faster_whisper import WhisperModel  # type: ignore

    size = settings.get("gpu.whisper_model", "small")
    attempts = [("cuda", settings.get("gpu.whisper_compute_type", "float16")), ("cpu", "int8")]
    last = None
    for device, ctype in attempts:
        try:
            model = WhisperModel(size, device=device, compute_type=ctype)
            segments, _ = model.transcribe(path, language=language[:2] or None, word_timestamps=True, vad_filter=False)
            out = [{"word": w.word.strip(), "start": float(w.start), "end": float(w.end)}
                   for seg in segments for w in (seg.words or []) if w.word.strip()]
            del model
            gc.collect()
            if out:
                return out
        except Exception as e:  # noqa: BLE001
            last = e
            log.warning("whisper on %s failed: %s", device, str(e)[:300])
    raise RuntimeError(f"whisper unavailable: {last}")


def _proportional_words(sections: list[dict], timings: list[dict]) -> list[dict]:
    """No ASR available: spread each section's words over its speech window by character length."""
    out = []
    for s, t in zip(sections, timings):
        ws = s["text"].split()
        start, end = t["start"] if t["index"] else max(t["start"], 0.15), t.get("speech_end", t["end"])
        total = sum(len(w) + 1 for w in ws) or 1
        cur = start
        for w in ws:
            d = (end - start) * (len(w) + 1) / total
            out.append({"word": w, "start": cur, "end": cur + d})
            cur += d
    return out


def chunk(words: list[dict], max_chars: int, max_words: int) -> list[list[dict]]:
    chunks, cur = [], []
    for w in words:
        if cur:
            text = " ".join(x["word"] for x in cur + [w])
            if (len(text) > max_chars or len(cur) >= max_words or w["start"] - cur[-1]["end"] > 0.6
                    or cur[-1]["word"][-1:] in ".?!"):
                chunks.append(cur)
                cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def _wrap2(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    mid = len(text) // 2
    cut = min((i for i, c in enumerate(text) if c == " "), key=lambda i: abs(i - mid), default=-1)
    return text if cut < 0 else text[:cut] + "\n" + text[cut + 1:]


def _ass_escape(s: str) -> str:
    return s.replace("\\", "").replace("{", "(").replace("}", ")")


def write_srt(chunks, path):
    lines = []
    for i, c in enumerate(chunks, 1):
        start, end = c[0]["start"], max(c[-1]["end"], c[0]["start"] + 0.6)
        lines += [str(i), f"{ts_srt(start)} --> {ts_srt(end)}", _wrap2(" ".join(w["word"] for w in c), 42), ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_ass(chunks, path, width, height, brand_hex: str, karaoke: bool):
    ccfg = settings.get("video.captions", {}) or {}
    font = ccfg.get("font", "Arial")
    size = int(height * (0.052 if karaoke else 0.045))
    margin_v = int(height * (0.28 if karaoke else 0.07))
    hl = hex_to_ass(brand_hex)
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,{max(3, size // 12)},2,2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for c in chunks:
        end_chunk = max(c[-1]["end"], c[0]["start"] + 0.5)
        if karaoke:
            words = [_ass_escape(w["word"].upper()) for w in c]
            for i, w in enumerate(c):
                s = w["start"]
                e = c[i + 1]["start"] if i + 1 < len(c) else end_chunk
                if e - s < 0.05:
                    continue
                txt = " ".join(f"{{\\c{hl}}}{t}{{\\c&H00FFFFFF&}}" if j == i else t for j, t in enumerate(words))
                events.append(f"Dialogue: 0,{ts_ass(s)},{ts_ass(e)},Default,,0,0,0,,{txt}")
        else:
            txt = _ass_escape(_wrap2(" ".join(w["word"] for w in c), 42)).replace("\n", "\\N")
            events.append(f"Dialogue: 0,{ts_ass(c[0]['start'])},{ts_ass(end_chunk)},Default,,0,0,0,,{txt}")
    path.write_text(head + "\n".join(events) + "\n", encoding="utf-8")


POWER = {"never", "always", "free", "secret", "secrets", "hack", "hacked", "hackers", "warning", "danger", "dangerous",
         "million", "millions", "billion", "billions", "trillion", "first", "best", "worst", "now", "stop", "new",
         "leaked", "breach", "attack", "risk", "scam", "banned", "illegal", "instantly", "today", "everyone", "nobody",
         "biggest", "fastest", "critical", "urgent", "proof", "truth", "mistake", "easy", "simple", "zero"}
_STOPWORDS = {"about", "after", "again", "their", "there", "these", "those", "which", "while", "would", "could", "should",
              "where", "every", "other", "being", "because", "before", "through", "really", "things", "people"}


def _bare(word: str) -> str:
    return re.sub(r"^[^\w$#@]+|[^\w%+]+$", "", word)


def keyword_score(word: str, title_words: set[str]) -> int:
    """How much a word deserves emphasis: 0 = none."""
    b = _bare(word)
    low = b.lower()
    if not b:
        return 0
    if any(c.isdigit() for c in b) or b[:1] in "$#" or b.endswith("%"):
        return 3
    if len(b) >= 2 and b.isupper() and b.isalpha():          # acronyms: AI, VPN, GDPR
        return 3
    if low in POWER:
        return 2
    if len(low) >= 5 and low in title_words and low not in _STOPWORDS:
        return 1
    return 0


def pick_keywords(chunks: list[list[dict]], title: str, share: float = 0.5) -> list[int | None]:
    """At most one emphasised word per phrase, and not in every phrase (or nothing stands out)."""
    tw = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z'-]+", title or "")}
    best = []
    for c in chunks:
        scores = [keyword_score(w["word"], tw) for w in c]
        top = max(scores) if scores else 0
        best.append((top, scores.index(top)) if top else (0, None))
    allowed = max(1, int(len(chunks) * share))
    keep = set(sorted((i for i, (sc, _) in enumerate(best) if sc), key=lambda i: (-best[i][0], i))[:allowed])
    return [best[i][1] if i in keep else None for i in range(len(chunks))]


def write_ass_pop(chunks, path, width, height, brand_hex: str, short: bool, title: str = "", font: str | None = None):
    ccfg = settings.get("video.captions", {}) or {}
    font = font or ccfg.get("font", "Arial")
    size = int(height * float(ccfg.get("pop_size_short", 0.058) if short else ccfg.get("pop_size_long", 0.062)))
    margin_v = int(height * (0.27 if short else 0.085))
    hl = hex_to_ass(brand_hex)
    kw_hex = ccfg.get("keyword_color", "#FF4D6D")
    if kw_hex.lower() == (brand_hex or "").lower():
        kw_hex = "#FFFFFF" if kw_hex.lower() != "#ffffff" else "#FFD400"
    kc = hex_to_ass(kw_hex)
    upper = bool(ccfg.get("pop_uppercase", True))
    pop_ms = int(ccfg.get("pop_ms", 130))
    outline = max(4, size // 9)
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,&HA0000000,-1,0,0,0,100,100,1,0,1,{outline},{max(2, size // 22)},2,{int(width * 0.07)},{int(width * 0.07)},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    keys = pick_keywords(chunks, title, float(ccfg.get("keyword_share", 0.5)))
    events = []
    for ci, c in enumerate(chunks):
        end_chunk = max(c[-1]["end"], c[0]["start"] + 0.5)
        words = [_ass_escape(w["word"].upper() if upper else w["word"]) for w in c]
        k = keys[ci]
        # fixed two-line layout for long phrases (no automatic wrapping, so a popping word never re-flows the line)
        brk, total = 0, len(" ".join(words))
        if len(words) > 1 and total > (11 if short else 34):
            brk = min(range(1, len(words)), key=lambda n: abs(len(" ".join(words[:n])) - total / 2))
        for i, w in enumerate(c):
            s = w["start"]
            e = c[i + 1]["start"] if i + 1 < len(c) else end_chunk
            if e - s < 0.04:
                continue
            parts = []
            for j, t in enumerate(words):
                if j < i:        # already spoken: stays on screen; a key word keeps its colour
                    parts.append(f"{{\\c{kc}}}{t}{{\\r}}" if j == k else t)
                elif j == i:     # the word being spoken pops in
                    if j == k:
                        tilt = -5 if (ci + i) % 2 else 5
                        parts.append(f"{{\\c{kc}\\fscx145\\fscy145\\frz{tilt}\\t(0,{pop_ms + 60},\\fscx112\\fscy112\\frz0)}}{t}{{\\r}}")
                    else:
                        parts.append(f"{{\\c{hl}\\fscx122\\fscy122\\t(0,{pop_ms},\\fscx100\\fscy100)}}{t}{{\\r}}")
                else:            # not spoken yet: invisible, but keeps its place so the line doesn't jump
                    parts.append(f"{{\\alpha&HFF&}}{t}{{\\r}}")
            lead = "{\\fad(70,0)}" if i == 0 else ""
            text = " ".join(parts[:brk]) + "\\N" + " ".join(parts[brk:]) if brk else " ".join(parts)
            events.append(f"Dialogue: 0,{ts_ass(s)},{ts_ass(e)},Default,,0,0,0,,{lead}{text}")
    path.write_text(head + "\n".join(events) + "\n", encoding="utf-8")
    return sum(1 for x in keys if x is not None)


def _pop_font(d: Path) -> str | None:
    """Use the bold display font (Anton, installed by install.sh) if present: copy it next to the
    subtitles so ffmpeg/libass finds it without a system-wide install."""
    name = settings.get("video.captions.pop_font", "Anton")
    src = resolve_path(settings.get("video.captions.pop_font_path") or settings.get("thumbnail.font_path", "./assets/fonts/Anton-Regular.ttf"))
    if not name or not Path(src).is_file():
        return None
    (d / "fonts").mkdir(exist_ok=True)
    dst = d / "fonts" / Path(src).name
    if not dst.exists():
        shutil.copyfile(src, dst)
    return name


def run(video_id: int) -> dict:
    v = db.get_video(video_id)
    cfg = db.get_channel(v["channel_id"]).get("config") or {}
    llm.unload()  # make sure the LLM isn't holding VRAM while Whisper loads
    try:
        words = _whisper_words(v["voice_path"], cfg.get("language", "en"))
        method = "whisper"
    except Exception as e:  # noqa: BLE001
        log.warning("falling back to proportional captions: %s", e)
        words = _proportional_words(v["script"]["sections"], v["timings"])
        method = "proportional"

    short = v["format"] == "short"
    fmt = settings.get(f"video.{v['format']}", {})
    d = video_dir(video_id)
    srt, ass = d / "captions.srt", d / "captions.ass"
    write_srt(chunk(words, 80, 14), srt)
    brand = (cfg.get("brand", {}) or {}).get("primary", "#FFD400")
    style = str(cfg.get("caption_style") or settings.get(f"video.captions.{v['format']}_style", "pop" if short else "plain")).lower()
    burn = style != "none" and (short or bool(settings.get("video.captions.long_burn_in", True)))
    keywords = 0
    if burn:
        w_, h_ = fmt.get("width", 1080 if short else 1920), fmt.get("height", 1920 if short else 1080)
        if style == "pop":
            keywords = write_ass_pop(chunk(words, 18 if short else 30, 3 if short else 5), ass, w_, h_, brand, short,
                                     title=v.get("title") or v.get("topic") or "", font=_pop_font(d))
        else:
            karaoke = style == "karaoke" and short
            write_ass(chunk(words, 16 if karaoke else 80, 3 if karaoke else 14), ass, w_, h_, brand, karaoke)
    db.update_video(video_id, captions_srt=str(srt), captions_ass=str(ass) if burn else None)
    return {"method": method, "words": len(words), "burned_in": burn, "style": style if burn else None, "keywords": keywords}
