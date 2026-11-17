"""Media agent: picks a visual for every script section and writes plan.json - an ordered list
of video clips and still images with exact durations.

Sources, tried in order (media.order, or `visuals:` per channel):
  broll - your own clips in assets/broll, matched by file/folder name words
  stock - Pexels / Pixabay, only when an API key is set
  ai    - images generated on the GPU with SDXL-Lightning (rendered with slow zoom/pan)
Anything still empty becomes a dark placeholder."""
from __future__ import annotations

import json
import logging
import math
import re
from pathlib import Path

import requests
from rapidfuzz import fuzz

from .. import db
from ..config import env, resolve_path, settings
from ..util import cache_dir, download, video_dir
from . import imagegen

log = logging.getLogger("ytm.media")


def _pexels(query: str, portrait: bool) -> list[dict]:
    key = env("PEXELS_API_KEY")
    if not key:
        return []
    r = requests.get("https://api.pexels.com/videos/search", headers={"Authorization": key}, timeout=20,
                     params={"query": query, "orientation": "portrait" if portrait else "landscape", "per_page": 15})
    r.raise_for_status()
    out = []
    for vid in r.json().get("videos", []):
        files = [f for f in vid.get("video_files", []) if f.get("file_type") == "video/mp4" and f.get("width")]
        good = [f for f in files if min(f["width"], f["height"]) >= 720] or files
        if not good:
            continue
        f = min(good, key=lambda f: f["width"] * f["height"])  # smallest file that is still >= 720p
        out.append({"provider": "pexels", "id": str(vid["id"]), "url": f["link"], "duration": vid.get("duration", 0),
                    "author": (vid.get("user") or {}).get("name"), "page": vid.get("url"), "license": "Pexels License"})
    return out


def _pixabay(query: str, portrait: bool) -> list[dict]:
    key = env("PIXABAY_API_KEY")
    if not key:
        return []
    r = requests.get("https://pixabay.com/api/videos/", timeout=20,
                     params={"key": key, "q": query[:100], "per_page": 15, "safesearch": "true"})
    r.raise_for_status()
    out = []
    for hit in r.json().get("hits", []):
        vids = hit.get("videos", {})
        f = next((vids[k] for k in ("medium", "large", "small") if vids.get(k, {}).get("url")), None)
        if not f:
            continue
        if not portrait and f.get("height", 0) > f.get("width", 1):
            continue
        out.append({"provider": "pixabay", "id": str(hit["id"]), "url": f["url"], "duration": hit.get("duration", 0),
                    "author": hit.get("user"), "page": hit.get("pageURL"), "license": "Pixabay Content License"})
    return out


PROVIDERS = {"pexels": _pexels, "pixabay": _pixabay}


def search(query: str, portrait: bool) -> list[dict]:
    results = []
    for name in settings.get("media.providers", ["pexels", "pixabay"]):
        try:
            results += PROVIDERS[name](query, portrait)
        except Exception as e:  # noqa: BLE001
            log.warning("%s search '%s' failed: %s", name, query, e)
        if len(results) >= 6:
            break
    return results


def _fetch(item: dict) -> str | None:
    dest = cache_dir("media") / f"{item['provider']}_{item['id']}.mp4"
    if dest.exists():
        return str(dest)
    try:
        download(item["url"], dest, max_mb=float(settings.get("media.max_clip_mb", 80)))
        return str(dest)
    except Exception as e:  # noqa: BLE001
        log.warning("download failed %s: %s", item["url"], e)
        return None


# --------------------------------------------------------------------------- own b-roll library
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
_STOP = {"a", "an", "the", "of", "and", "in", "on", "at", "with", "to", "for", "clip", "video", "stock", "footage", "hd", "4k"}


def _tokens(text: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", text.lower()) if len(w) > 1 and not w.isdigit() and w not in _STOP}


def broll_library(cfg: dict) -> list[dict]:
    """Your own clips: assets/broll/** (plus a per-channel broll_dir). File and folder names are the
    search words, e.g. assets/broll/city/traffic-night-rain.mp4 -> {city, traffic, night, rain}."""
    dirs = [settings.get("media.broll_dir", "./assets/broll")]
    if cfg.get("broll_dir"):
        dirs.insert(0, cfg["broll_dir"])
    lib, seen = [], set()
    for d in dirs:
        root = resolve_path(d)
        if not root.is_dir():
            continue
        for f in sorted(root.rglob("*")):
            if f.suffix.lower() in VIDEO_EXT and f.is_file() and str(f) not in seen:
                seen.add(str(f))
                words = _tokens(" ".join([f.stem] + [p.name for p in f.relative_to(root).parents if p.name]))
                lib.append({"path": str(f), "words": words, "name": f.stem})
    return lib


def broll_match(lib: list[dict], query: str, used: set[str]) -> dict | None:
    q = _tokens(query)
    if not q or not lib:
        return None
    min_score = float(settings.get("media.broll_min_score", 0.5))
    best, best_score = None, 0.0
    for clip in lib:
        if clip["path"] in used or not clip["words"]:
            continue
        hits = len(q & clip["words"])
        fuzzy = sum(1 for w in q - clip["words"] if any(fuzz.ratio(w, c) >= 85 for c in clip["words"]))
        score = (hits + 0.7 * fuzzy) / len(q)
        if score > best_score:
            best, best_score = clip, score
    return best if best and best_score >= min_score else None


# --------------------------------------------------------------------------- planning
def _order(cfg: dict) -> list[str]:
    order = cfg.get("visuals") or settings.get("media.order", ["broll", "stock", "ai"])
    return [o for o in order if o in ("broll", "stock", "ai")]


def _stock_available() -> bool:
    return any(env(f"{p.upper()}_API_KEY") for p in settings.get("media.providers", ["pexels", "pixabay"]))


def run(video_id: int) -> dict:
    v = db.get_video(video_id)
    ch = db.get_channel(v["channel_id"])
    cfg = ch.get("config") or {}
    fmt = v["format"]
    portrait = fmt == "short"
    clip_len = float(settings.get(f"video.{fmt}.clip_seconds", 4 if portrait else 6))
    ai_len = float(settings.get(f"ai_images.seconds_per_image.{fmt}", 4.5 if portrait else 7))
    fallback_q = (cfg.get("research", {}) or {}).get("fallback_visual") or "abstract background"
    sections, timings = v["script"]["sections"], v["timings"]
    order = _order(cfg)
    use_ai = "ai" in order and imagegen.enabled()
    lib = broll_library(cfg) if "broll" in order else []
    stock_on = "stock" in order and _stock_available()
    used: set[str] = set()
    query_cache: dict[str, list[dict]] = {}
    counts = {"broll": 0, "stock": 0, "ai": 0, "placeholder": 0}
    d = video_dir(video_id)
    db.ex("DELETE FROM assets WHERE video_id=? AND kind IN ('broll','stock_video','ai_image')", (video_id,))  # retry-safe
    plan: list[dict] = []
    ai_error = None
    ai_needed = [0] * len(sections)

    for si, (s, t) in enumerate(zip(sections, timings)):
        dur = max(0.5, t["end"] - t["start"])
        n = max(1, math.ceil(dur / clip_len - 0.25))
        piece = dur / n
        pieces = []
        for _ in range(n):
            clip = None
            reuse = None  # a clip already used in this video - better than a dark placeholder
            for src in order:
                if src == "broll" and lib:
                    m = broll_match(lib, s["visual"], used)
                    if not m and not reuse:
                        again = broll_match(lib, s["visual"], set())
                        reuse = {"kind": "video", "path": again["path"], "source": "broll"} if again else None
                    if m:
                        used.add(m["path"])
                        clip = {"kind": "video", "path": m["path"], "source": "broll"}
                        db.ex("INSERT INTO assets(video_id, kind, path, provider, license, created_at) VALUES (?,?,?,?,?,?)",
                              (video_id, "broll", m["path"], "local", "your own footage", db.now_iso()))
                elif src == "stock" and stock_on:
                    for q in (s["visual"], " ".join(s["visual"].split()[:2]), fallback_q):
                        if q not in query_cache:
                            query_cache[q] = search(q, portrait)
                        cand = next((c for c in query_cache[q] if f"{c['provider']}_{c['id']}" not in used), None)
                        if not cand and not reuse and query_cache[q]:
                            seen = next((c for c in query_cache[q] if (cache_dir("media") / f"{c['provider']}_{c['id']}.mp4").exists()), None)
                            if seen:
                                reuse = {"kind": "video", "path": str(cache_dir("media") / f"{seen['provider']}_{seen['id']}.mp4"), "source": "stock"}
                        if cand and (path := _fetch(cand)):
                            used.add(f"{cand['provider']}_{cand['id']}")
                            clip = {"kind": "video", "path": path, "source": "stock"}
                            db.ex("INSERT INTO assets(video_id, kind, path, provider, provider_id, author, source_url, license, created_at) "
                                  "VALUES (?,?,?,?,?,?,?,?,?)", (video_id, "stock_video", path, cand["provider"], cand["id"],
                                                                 cand.get("author"), cand.get("page"), cand["license"], db.now_iso()))
                            break
                elif src == "ai" and use_ai:
                    clip = {"kind": "ai"}
                if clip:
                    break
            clip = clip or reuse
            pieces.append({"section": t["index"], "duration": piece, **(clip or {"kind": "video", "path": None, "source": "placeholder"})})

        # merge each run of consecutive AI pieces and re-split it at ~ai_len seconds per image
        merged, run_ = [], []
        for p in pieces + [None]:
            if p is not None and p["kind"] == "ai":
                run_.append(p)
                continue
            if run_:
                total = sum(x["duration"] for x in run_)
                k = max(1, round(total / ai_len))
                merged += [{"section": t["index"], "duration": total / k, "kind": "image", "source": "ai"} for _ in range(k)]
                ai_needed[si] += k
                run_ = []
            if p is not None:
                merged.append(p)
        plan += merged

    # generate all AI images in one model load
    if any(ai_needed):
        img_dir = d / "images"
        img_dir.mkdir(exist_ok=True)
        prompts = imagegen.build_prompts(sections, ai_needed, ch, cfg)
        jobs, cursor = [], {i: 0 for i in range(len(sections))}
        for idx, p in enumerate(plan):
            if p.get("source") != "ai":
                continue
            si = p["section"]
            prompt = prompts[si][cursor[si]] if cursor[si] < len(prompts[si]) else prompts[si][-1]
            cursor[si] += 1
            p["path"] = str(img_dir / f"img_{idx:03d}.png")
            p["prompt"] = prompt
            jobs.append({"prompt": prompt, "path": p["path"], "seed": imagegen.seed_for(video_id, idx)})
        try:
            imagegen.generate(jobs, fmt)
            ai_error = next((j["error"] for j in jobs if j.get("error")), None)
        except Exception as e:  # noqa: BLE001 - model missing/not downloaded/no GPU: degrade, don't crash
            ai_error = f"AI images unavailable: {e}"[:400]
            log.error(ai_error)
        for p in plan:
            if p.get("source") == "ai" and not Path(p["path"]).exists():
                p.update(kind="video", path=None, source="placeholder")

    motions = ["in", "right", "out", "left"]
    for i, p in enumerate(plan):
        p["duration"] = round(p["duration"], 3)
        if p["kind"] == "image":
            p["motion"] = motions[i % len(motions)]
            db.ex("INSERT INTO assets(video_id, kind, path, provider, license, meta_json, created_at) VALUES (?,?,?,?,?,?,?)",
                  (video_id, "ai_image", p["path"], "sdxl-lightning", "CreativeML Open RAIL++-M",
                   db.jdump({"prompt": p.get("prompt")}), db.now_iso()))
        counts[p["source"]] += 1

    if counts["placeholder"] == len(plan) and not settings.get("media.allow_all_placeholders", False):
        raise RuntimeError("no visuals found for any section - add clips to assets/broll, set a stock API key, "
                           "or check AI images with ./ytm.sh doctor" + (f" ({ai_error})" if ai_error else ""))
    (d / "plan.json").write_text(json.dumps(plan, indent=1), encoding="utf-8")
    return {**counts, "segments": len(plan), **({"ai_error": ai_error} if ai_error else {})}
