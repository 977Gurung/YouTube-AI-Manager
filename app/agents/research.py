"""Research agent: collect trend signals, then let the local LLM turn them into ranked video ideas.

Sources (all free; YouTube API calls are quota-tracked):
  - Google Trends daily trending RSS (per geo)
  - Any RSS/Atom feeds you list (news sites, blogs, subreddits via RSS)
  - YouTube "mostPopular" chart for the channel's category (1 quota unit)
  - Competitor channels: latest uploads via public RSS + view velocity (1 unit / 50 videos)
  - Optional YouTube keyword search (100 units per keyword - off by default)
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import feedparser
import requests
from rapidfuzz import fuzz

from .. import db, llm
from ..config import env, settings
from ..states import IDEA_APPROVED, IDEA_PENDING, gates_for

log = logging.getLogger("ytm.research")
UA = {"User-Agent": "Mozilla/5.0 (compatible; YT-AI-Manager/1.0; research)"}
YT = "https://www.googleapis.com/youtube/v3"


# --------------------------------------------------------------------------- sources
def _hours_since(iso: str) -> float:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return max(1.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)
    except (ValueError, AttributeError):
        return 24.0


def _rss(url: str, source: str, limit: int) -> list[dict]:
    resp = requests.get(url, headers=UA, timeout=20)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    out = []
    for i, e in enumerate(feed.entries[:limit]):
        title = (e.get("title") or "").strip()
        if not title:
            continue
        meta = {}
        if e.get("ht_approx_traffic"):
            meta["traffic"] = e.get("ht_approx_traffic")
        out.append({"source": source, "title": title, "url": e.get("link"), "score": float(limit - i), "meta": meta})
    return out


def google_trends(geo: str, limit: int) -> list[dict]:
    return _rss(f"https://trends.google.com/trending/rss?geo={geo}", f"google_trends:{geo}", limit)


def reddit(sub: str, limit: int) -> list[dict]:
    return _rss(f"https://www.reddit.com/r/{sub}/top/.rss?t=day", f"reddit:r/{sub}", limit)


def _yt_get(path: str, params: dict, units: int) -> dict | None:
    key = env("YOUTUBE_API_KEY")
    if not key:
        return None
    reserve = int(settings.get("youtube.quota_reserve", 2000))
    if db.quota_remaining() < units + reserve:
        log.warning("YouTube quota low - skipping research call %s", path)
        return None
    r = requests.get(f"{YT}/{path}", params={**params, "key": key}, timeout=20)
    db.record_quota(f"research.{path}", units)
    r.raise_for_status()
    return r.json()


def _video_signals(items: list[dict], source: str) -> list[dict]:
    out = []
    for it in items:
        sn, st = it.get("snippet", {}), it.get("statistics", {})
        views = int(st.get("viewCount", 0) or 0)
        hrs = _hours_since(sn.get("publishedAt", ""))
        out.append({"source": source, "title": sn.get("title", ""), "url": f"https://youtu.be/{it.get('id')}",
                    "score": round(views / hrs, 1),
                    "meta": {"views": views, "hours_old": round(hrs, 1), "channel": sn.get("channelTitle")}})
    return out


def _stats(ids: list[str]) -> list[dict]:
    items = []
    for i in range(0, len(ids), 50):
        data = _yt_get("videos", {"part": "snippet,statistics", "id": ",".join(ids[i:i + 50])}, 1)
        if data:
            items += data.get("items", [])
    return items


def youtube_trending(region: str, category: str | None, limit: int) -> list[dict]:
    params = {"part": "snippet,statistics", "chart": "mostPopular", "regionCode": region, "maxResults": min(limit, 50)}
    if category:
        params["videoCategoryId"] = category
    data = _yt_get("videos", params, 1)
    return _video_signals(data.get("items", []), "youtube_trending") if data else []


def competitors(channel_ids: list[str], limit: int) -> list[dict]:
    ids: list[str] = []
    for cid in channel_ids:
        try:
            r = requests.get("https://www.youtube.com/feeds/videos.xml", params={"channel_id": cid}, headers=UA, timeout=20)
            r.raise_for_status()
            ids += [e.get("yt_videoid") for e in feedparser.parse(r.content).entries[:10] if e.get("yt_videoid")]
        except requests.RequestException as e:
            log.warning("competitor feed %s failed: %s", cid, e)
    sig = _video_signals(_stats(ids), "competitors") if ids else []
    return sorted(sig, key=lambda s: s["score"], reverse=True)[:limit]


def youtube_search(keywords: list[str], limit: int) -> list[dict]:
    after = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ids: list[str] = []
    for kw in keywords[: int(settings.get("research.max_search_keywords", 2))]:
        data = _yt_get("search", {"part": "id", "q": kw, "type": "video", "order": "viewCount",
                                  "publishedAfter": after, "maxResults": 15}, 100)
        if data:
            ids += [it["id"]["videoId"] for it in data.get("items", []) if it.get("id", {}).get("videoId")]
    sig = _video_signals(_stats(ids), "youtube_search") if ids else []
    return sorted(sig, key=lambda s: s["score"], reverse=True)[:limit]


def _codes(value, default="US") -> list[str]:
    """Country codes from "AE", "AE, US" or [AE, US] -> ["AE", "US"] (two-letter ISO codes; UAE = AE)."""
    items = value if isinstance(value, (list, tuple)) else str(value or "").replace(";", ",").split(",")
    out = []
    for x in items:
        c = {"UAE": "AE", "UK": "GB"}.get(str(x).strip().upper(), str(x).strip().upper())
        if c and c not in out:
            out.append(c)
    return out or [default]


def collect_signals(cfg: dict) -> tuple[list[dict], dict]:
    rcfg = cfg.get("research", {}) or {}
    limit = int(settings.get("research.max_signals_per_source", 20))
    max_total = int(settings.get("research.max_signals_total", 45))
    geos = _codes(rcfg.get("trends_geo", settings.get("research.trends_geo", "US")))
    # YouTube trending: the channel's own region, else the channel's trends_geo, else the global default
    regions = _codes(rcfg.get("region") or (geos if rcfg.get("trends_geo") else settings.get("research.region", "US")))

    tasks = []
    if rcfg.get("google_trends", True):
        for g in geos:
            tasks.append((f"google_trends {g}", lambda g=g: google_trends(g, limit)))
    for url in rcfg.get("rss", []) or []:
        tasks.append((f"rss {url}", lambda u=url: _rss(u, "rss:" + (urlparse(u).hostname or "feed"), limit)))
    for sub in rcfg.get("subreddits", []) or []:
        tasks.append((f"reddit {sub}", lambda s=sub: reddit(s, limit)))
    for r in regions:
        tasks.append((f"youtube_trending {r}", lambda r=r: youtube_trending(r, rcfg.get("youtube_category"), limit)))
    if rcfg.get("competitors"):
        tasks.append(("competitors", lambda: competitors(rcfg["competitors"], limit)))
    if settings.get("research.youtube_search_enabled", False) and rcfg.get("keywords"):
        tasks.append(("youtube_search", lambda: youtube_search(rcfg["keywords"], limit)))

    buckets, errors = [], {}
    for name, fn in tasks:
        try:
            res = sorted(fn(), key=lambda s: s["score"], reverse=True)
            if res:
                buckets.append(res)
        except Exception as e:  # noqa: BLE001 - a dead source must never stop research
            errors[name] = str(e)[:200]
            log.warning("research source %s failed: %s", name, e)

    merged, i = [], 0  # round-robin so one noisy source can't dominate
    while len(merged) < max_total and any(i < len(b) for b in buckets):
        for b in buckets:
            if i < len(b) and len(merged) < max_total:
                merged.append(b[i])
        i += 1
    return merged, errors


# --------------------------------------------------------------------------- idea generation
def _past_performance(channel_id: int) -> str:
    rows = db.q(
        "SELECT v.title, v.format, s.views FROM videos v JOIN v_latest_stats s ON s.video_id = v.id "
        "WHERE v.channel_id=? ORDER BY s.views DESC", (channel_id,))
    if not rows:
        return "No published videos yet."
    pick = rows[:5] + (rows[-3:] if len(rows) > 8 else [])
    return "\n".join(f"- [{r['format']}] {r['title']} -> {r['views']} views" for r in pick)


def _generate_ideas(ch: dict, cfg: dict, signals: list[dict], n: int, recent: list[str]) -> list[dict]:
    formats = cfg.get("formats", ["short", "long"])
    mix = cfg.get("mix", {})
    sig_lines = "\n".join(
        f"{i + 1}. [{s['source']}] {s['title']}" + (f" ({json.dumps(s['meta'], ensure_ascii=False)})" if s.get("meta") else "")
        for i, s in enumerate(signals)) or "(no live signals today - propose strong evergreen ideas)"
    system = (f'You are the content strategist for the YouTube channel "{ch["name"]}". '
              f'Niche: {ch["niche"]}. Audience: {cfg.get("audience", "general")}. '
              f'Language: {cfg.get("language", "en")}. Respond with JSON only.')
    user = f"""Today's signals (trends, competitor uploads, feeds):
{sig_lines}

How our past videos performed:
{_past_performance(ch["id"])}

Topics already covered or queued (do NOT repeat or closely paraphrase):
{chr(10).join('- ' + t for t in recent[:60]) or '- none'}

Propose exactly {n} video ideas for this channel.
- Use only signals that genuinely fit the niche; ignore unrelated celebrity/sport/politics items.
- Evergreen ideas are fine when signals are weak.
- Allowed formats: {formats}. Preferred mix (short:long): {mix or 'any'}.
- Never invent breaking-news facts; news-driven ideas must stay within what the signal title states.
- "score" is your 1-10 estimate of performance for THIS audience.

Return JSON: {{"ideas": [{{"topic": "...", "angle": "the unique take", "format": "short|long", "hook": "first spoken sentence", "why": "which signals support it", "score": 7}}]}}"""
    data = llm.chat_json(system, user, temperature=0.8)
    ideas = data.get("ideas") or []
    return [i for i in ideas if isinstance(i, dict)]


def run(channel_id: int) -> dict:
    ch = db.get_channel(channel_id)
    if not ch:
        raise ValueError(f"channel {channel_id} not found")
    cfg = ch.get("config") or {}
    pending = db.one("SELECT COUNT(*) AS n FROM videos WHERE channel_id=? AND status=?", (channel_id, IDEA_PENDING))["n"]
    max_pending = int(settings.get("research.max_pending_ideas", 15))
    if pending >= max_pending:
        return {"skipped": "idea backlog full - review pending ideas first", "pending": pending}

    signals, errors = collect_signals(cfg)
    run_id = uuid.uuid4().hex[:12]
    for s in signals:
        db.ex("INSERT INTO research_items(channel_id, run_id, source, title, url, score, meta_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
              (channel_id, run_id, s["source"], s["title"][:500], s.get("url"), s["score"], db.jdump(s.get("meta") or {}), db.now_iso()))

    recent = [r["topic"] for r in db.q("SELECT topic FROM videos WHERE channel_id=? ORDER BY id DESC LIMIT 300", (channel_id,))]
    n = max(1, min(int(settings.get("research.ideas_per_run", 6)), max_pending - pending))
    ideas = _generate_ideas(ch, cfg, signals, n, recent)
    llm.unload()

    thr = int(settings.get("research.dedupe_threshold", 82))
    formats = cfg.get("formats", ["short", "long"])
    status = IDEA_PENDING if "idea" in gates_for(cfg) else IDEA_APPROVED
    created = []
    # models often return more (or fewer) ideas than asked: keep the best n so the backlog cap holds
    def _score(i):
        try:
            return float(i.get("score", 5))
        except (TypeError, ValueError):
            return 5.0
    for idea in sorted(ideas, key=_score, reverse=True):
        if len(created) >= n:
            break
        topic = str(idea.get("topic", "")).strip()[:200]
        if not topic or any(fuzz.token_sort_ratio(topic.lower(), t.lower()) >= thr for t in recent):
            continue
        fmt = idea.get("format") if idea.get("format") in formats else formats[0]
        try:
            score = float(idea.get("score", 5))
        except (TypeError, ValueError):
            score = 5.0
        vid = db.ex(
            "INSERT INTO videos(channel_id, format, status, topic, angle, hook, rationale, idea_score, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (channel_id, fmt, status, topic, str(idea.get("angle", ""))[:500], str(idea.get("hook", ""))[:500],
             str(idea.get("why", ""))[:1000], score, db.now_iso(), db.now_iso()))
        db.log_event(vid, "idea_created", f"run {run_id}", channel_id=channel_id)
        recent.append(topic)
        created.append(vid)
    return {"run_id": run_id, "signals": len(signals), "source_errors": errors, "ideas": created}
