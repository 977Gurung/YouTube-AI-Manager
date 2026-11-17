"""SEO agent: title, description, tags and hashtags. Adds chapters (from real voice timings),
#Shorts, attribution and an AI-narration note. Enforces YouTube field limits."""
from __future__ import annotations

import logging
import re

from .. import db, llm
from ..util import ts_chapter

log = logging.getLogger("ytm.seo")


def _chapters(sections: list[dict], timings: list[dict], total: float) -> str:
    marks = []
    for s, t in zip(sections, timings):
        name = s.get("chapter")
        if name and (not marks or marks[-1][1] != name):
            marks.append([t["start"], name])
    # YouTube rules: first at 0:00, >= 3 chapters, each >= 10 s -> merge short ones
    merged = []
    for start, name in marks:
        if merged and start - merged[-1][0] < 10:
            merged[-1][1] = name            # previous one too short: let this chapter absorb it
            continue
        merged.append([start, name])
    if merged and total - merged[-1][0] < 10:
        merged.pop()                        # last chapter too short: fold into the one before
    if len(merged) < 3:
        return ""
    merged[0][0] = 0
    return "\n".join(f"{ts_chapter(s)} {n}" for s, n in merged)


def _clean_title(t: str) -> str:
    return re.sub(r"[<>]", "", re.sub(r"\s+", " ", t or "")).strip()[:100]


def _fit_tags(tags: list) -> list[str]:
    out, total = [], 0
    for t in tags:
        t = re.sub(r"[<>,\"]", "", str(t)).strip()
        if not t or t.lower() in (x.lower() for x in out):
            continue
        cost = len(t) + (2 if " " in t else 0) + (1 if out else 0)
        if total + cost > 480:
            break
        out.append(t)
        total += cost
    return out


def run(video_id: int) -> dict:
    v = db.get_video(video_id)
    ch = db.get_channel(v["channel_id"])
    cfg = ch.get("config") or {}
    script = v["script"]
    short = v["format"] == "short"
    summary = " ".join(s["text"] for s in script["sections"])[:2500]
    data = llm.chat_json(
        f'You are a YouTube SEO specialist for "{ch["name"]}" (niche: {ch["niche"]}). '
        f'Write in language "{cfg.get("language", "en")}". Respond with JSON only.',
        f"""Format: {'YouTube Short' if short else 'long-form video'}
Candidate titles: {script.get('title_options')}
Script: {summary}

Return JSON: {{"title": "max 70 chars, curiosity + main keyword, no clickbait lies, no ALL CAPS",
"description": "{'2-3 short lines' if short else '2 short paragraphs: hook + what viewers learn'}, keyword-rich, no links",
"tags": ["8-15 search phrases"], "hashtags": ["3 hashtags without spaces"]}}""", temperature=0.5)
    llm.unload()

    title = _clean_title(data.get("title") or v.get("title") or v["topic"])
    hashtags = [("#" + re.sub(r"[^\w]", "", str(h).lstrip("#")))[:40] for h in (data.get("hashtags") or [])][:3]
    hashtags = [h for h in hashtags if len(h) > 1]
    if short and "#Shorts" not in hashtags:
        hashtags = ["#Shorts"] + hashtags[:2]

    parts = [str(data.get("description") or "").strip()]
    if not short and (ch_text := _chapters(script["sections"], v["timings"] or [], float(v["duration_sec"] or 0))):
        parts.append("Chapters:\n" + ch_text)
    footer = cfg.get("description_footer")
    if footer:
        parts.append(footer.strip())
    providers = sorted({r["provider"] for r in db.q("SELECT DISTINCT provider FROM assets WHERE video_id=? AND kind='stock_video'", (video_id,)) if r["provider"]})
    credits = []
    if providers:
        credits.append("Stock footage: " + ", ".join(p.capitalize() for p in providers))
    if db.one("SELECT 1 AS x FROM assets WHERE video_id=? AND kind='ai_image' LIMIT 1", (video_id,)):
        credits.append("Visuals: AI-generated images.")
    credits.append("Narration: AI-generated voice.")
    parts.append("\n".join(credits))
    parts.append(" ".join(hashtags))
    description = re.sub(r"[<>]", "", "\n\n".join(p for p in parts if p))
    while len(description.encode("utf-8")) > 4900:
        description = description[:-200]

    tags = _fit_tags((data.get("tags") or []) + [v["topic"]])
    db.update_video(video_id, title=title, description=description, tags=tags)
    return {"title": title, "tags": len(tags)}
