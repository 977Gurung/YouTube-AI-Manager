"""Analytics agent: snapshots public stats (views/likes/comments) for uploaded videos.
1 quota unit per 50 videos. Results feed back into research prompts ("how past videos performed")."""
from __future__ import annotations

import logging

import requests

from .. import db
from ..config import env
from . import upload

log = logging.getLogger("ytm.analytics")


def _fetch_with_key(ids: list[str]) -> list[dict]:
    r = requests.get("https://www.googleapis.com/youtube/v3/videos", timeout=20,
                     params={"part": "statistics,status", "id": ",".join(ids), "key": env("YOUTUBE_API_KEY")})
    r.raise_for_status()
    return r.json().get("items", [])


def _fetch_with_oauth(slug: str, ids: list[str]) -> list[dict]:
    return upload.service(slug).videos().list(part="statistics,status", id=",".join(ids)).execute().get("items", [])


def sync() -> dict:
    rows = db.q("SELECT v.id, v.youtube_video_id, v.status, c.slug FROM videos v JOIN channels c ON c.id=v.channel_id "
                "WHERE v.youtube_video_id IS NOT NULL AND v.status IN ('scheduled','published')")
    by_yt = {r["youtube_video_id"]: r for r in rows}
    ids = list(by_yt)
    saved, missing, went_live = 0, [], 0
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        try:
            items = _fetch_with_key(batch) if env("YOUTUBE_API_KEY") else _fetch_with_oauth(by_yt[batch[0]]["slug"], batch)
        except Exception as e:  # noqa: BLE001
            log.warning("stats fetch failed: %s", e)
            continue
        db.record_quota("analytics.videos.list", 1)
        found = set()
        for it in items:
            found.add(it["id"])
            row = by_yt[it["id"]]
            st = it.get("statistics", {})
            db.ex("INSERT INTO analytics_snapshots(video_id, youtube_video_id, views, likes, comments, captured_at) VALUES (?,?,?,?,?,?)",
                  (row["id"], it["id"], int(st.get("viewCount", 0)), int(st.get("likeCount", 0)),
                   int(st.get("commentCount", 0)), db.now_iso()))
            saved += 1
            if row["status"] == "scheduled" and it.get("status", {}).get("privacyStatus") == "public":
                db.update_video(row["id"], status="published", published_at=db.now_iso())
                went_live += 1
        missing += [x for x in batch if x not in found]
    return {"snapshots": saved, "now_public": went_live, "not_visible": missing}


def summary() -> dict:
    per_channel = db.q(
        "SELECT c.slug, c.name, COUNT(s.id) AS videos, COALESCE(SUM(s.views),0) AS views, "
        "COALESCE(SUM(s.likes),0) AS likes, COALESCE(SUM(s.comments),0) AS comments "
        "FROM channels c LEFT JOIN videos v ON v.channel_id=c.id LEFT JOIN v_latest_stats s ON s.video_id=v.id "
        "WHERE c.active=1 GROUP BY c.id ORDER BY views DESC")
    top = db.q(
        "SELECT v.id, v.title, v.format, v.youtube_video_id, c.slug AS channel, s.views, s.likes, s.comments, v.published_at, v.scheduled_at "
        "FROM videos v JOIN channels c ON c.id=v.channel_id JOIN v_latest_stats s ON s.video_id=v.id "
        "ORDER BY s.views DESC LIMIT 50")
    return {"channels": per_channel, "videos": top}
