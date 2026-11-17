"""Script agent: turns an approved idea into a narration script split into sections,
each with a stock-footage search query. Shorts = one pass; long-form = outline + per-chapter passes
(keeps each request small enough for a 7-8B model on an 8 GB card)."""
from __future__ import annotations

import json
import logging
import re

from .. import db, llm
from ..config import settings
from ..util import clean_spoken

log = logging.getLogger("ytm.script")
WPM = 150

RULES = """Rules for every "text" field:
- Written to be read aloud by a narrator: short sentences, conversational, no filler.
- No markdown, emojis, hashtags, stage directions or speaker labels.
- Do not state facts or statistics you are not sure of; prefer explanation over invented numbers.
Rules for every "visual" field:
- 2 to 4 words describing concrete, filmable stock footage (e.g. "city traffic night", "hands typing keyboard").
- No brand names, logos, real people or on-screen text."""


def _system(ch: dict, cfg: dict) -> str:
    return (f'You are the head scriptwriter for the YouTube channel "{ch["name"]}". Niche: {ch["niche"]}. '
            f'Audience: {cfg.get("audience", "general viewers")}. Tone: {cfg.get("tone", "clear and engaging")}. '
            f'Write everything in language "{cfg.get("language", "en")}". Respond with JSON only.')


def _idea_block(v: dict) -> str:
    s = f"Topic: {v['topic']}\nAngle: {v.get('angle') or '-'}\nHook idea: {v.get('hook') or '-'}"
    if v.get("feedback"):
        s += f"\nReviewer feedback on the previous draft (you MUST address it): {v['feedback']}"
    return s


def _clean_sections(raw, chapter: str | None = None) -> list[dict]:
    out = []
    for s in raw or []:
        if not isinstance(s, dict):
            continue
        text = clean_spoken(str(s.get("text", "")))
        if not text:
            continue
        visual = re.sub(r"[^\w\s-]", "", str(s.get("visual", "")))[:60].strip() or "abstract background"
        out.append({"text": text, "visual": visual, "chapter": chapter})
    return out


def words(sections: list[dict]) -> int:
    return sum(len(s["text"].split()) for s in sections)


def _short(v: dict, ch: dict, cfg: dict) -> dict:
    target = int(cfg.get("short_words", settings.get("video.short.words_target", 140)))
    system = _system(ch, cfg)
    user = f"""{_idea_block(v)}

Write a YouTube Shorts script of about {target} words (hard maximum {int(target * 1.15)}).
Structure: 4-7 sections. Section 1 is the hook - it must grab attention in the first two seconds.
The last section is a punchy payoff that loops back to the opening or asks a quick question.
{RULES}
Return JSON: {{"title_options": ["...", "...", "..."], "thumbnail_text": "max 4 words", "sections": [{{"text": "...", "visual": "..."}}]}}"""
    data = llm.chat_json(system, user)
    sections = _clean_sections(data.get("sections"))
    if not sections:
        raise ValueError("LLM returned no script sections")
    if words(sections) > target * 1.3:
        fix = llm.chat_json(system, f"{user}\n\nYour draft was {words(sections)} words - too long:\n"
                                    f"{json.dumps({'sections': sections}, ensure_ascii=False)}\n"
                                    f"Rewrite it to at most {target} words in the same JSON format.")
        if _clean_sections(fix.get("sections")):
            sections = _clean_sections(fix.get("sections"))
    return {"format": "short", "title_options": data.get("title_options") or [v["topic"]],
            "thumbnail_text": str(data.get("thumbnail_text") or "")[:40], "sections": sections}


def _long(v: dict, ch: dict, cfg: dict) -> dict:
    minutes = float(cfg.get("long_minutes", settings.get("video.long.minutes_target", 8)))
    total_words = int(minutes * WPM)
    system = _system(ch, cfg)
    outline = llm.chat_json(system, f"""{_idea_block(v)}

Plan a {minutes:g}-minute YouTube video (~{total_words} spoken words).
Return JSON: {{"title_options": ["...", "...", "..."], "thumbnail_text": "max 4 words",
"intro": {{"text": "hook + promise of the video, max 60 words", "visual": "..."}},
"chapters": [{{"title": "short chapter title", "key_points": ["...", "..."]}}],
"outro": {{"text": "recap + call to subscribe, max 50 words", "visual": "..."}}}}
Use 4-8 chapters that build on each other.
{RULES}""")
    chapters = [c for c in outline.get("chapters") or [] if isinstance(c, dict) and c.get("title")]
    if not chapters:
        raise ValueError("LLM returned no chapters")
    per_chapter = max(80, (total_words - 110) // len(chapters))

    sections = _clean_sections([outline.get("intro")], chapter="Intro")
    previous = sections[-1]["text"] if sections else ""
    plan = "\n".join(f"{i + 1}. {c['title']}" for i, c in enumerate(chapters))
    for i, c in enumerate(chapters):
        data = llm.chat_json(system, f"""Video: {v['topic']} ({v.get('angle') or ''})
Full chapter plan:
{plan}

Write chapter {i + 1}: "{c['title']}" - about {per_chapter} words.
Key points: {json.dumps(c.get('key_points', []), ensure_ascii=False)}
The previous section ended with: "{previous[-300:]}" - continue naturally, don't re-introduce the video.
Split it into 3-8 sections of 30-80 words, each with its own visual.
{RULES}
Return JSON: {{"sections": [{{"text": "...", "visual": "..."}}]}}""")
        chunk = _clean_sections(data.get("sections"), chapter=str(c["title"])[:80])
        if not chunk:
            raise ValueError(f"LLM returned nothing for chapter {i + 1}")
        sections += chunk
        previous = chunk[-1]["text"]
    sections += _clean_sections([outline.get("outro")], chapter="Wrap-up")
    return {"format": "long", "title_options": outline.get("title_options") or [v["topic"]],
            "thumbnail_text": str(outline.get("thumbnail_text") or "")[:40], "sections": sections}


def run(video_id: int) -> dict:
    v = db.get_video(video_id)
    ch = db.get_channel(v["channel_id"])
    cfg = ch.get("config") or {}
    script = _short(v, ch, cfg) if v["format"] == "short" else _long(v, ch, cfg)
    llm.unload()
    script["title_options"] = [str(t)[:100] for t in script["title_options"] if t][:5] or [v["topic"]]
    db.update_video(video_id, script=script, script_text="\n\n".join(s["text"] for s in script["sections"]),
                    title=script["title_options"][0])
    return {"sections": len(script["sections"]), "words": words(script["sections"])}
