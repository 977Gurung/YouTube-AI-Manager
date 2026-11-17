"""Upload agent: YouTube Data API v3 with one OAuth token per channel.

Videos are uploaded as private with `publishAt` = the approved slot (YouTube publishes them),
or published immediately if the slot is within 10 minutes. Thumbnail + SRT captions follow.
Quota: videos.insert 1600 units, thumbnails.set 50, captions.insert 400 (default daily quota 10,000).

IMPORTANT: API projects that have not passed the YouTube API Services audit can only upload
PRIVATE videos (they are locked private). See README -> "Before you go live"."""
from __future__ import annotations

import getpass
import json
import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import db
from ..config import data_dir, env, resolve_path, settings

log = logging.getLogger("ytm.upload")
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.force-ssl",
          "https://www.googleapis.com/auth/youtube.readonly"]
UPLOAD_COST, THUMB_COST, CAPTION_COST = 1600, 50, 400


class QuotaDeferred(Exception):
    """Not enough quota today - the video stays 'approved' and is retried after the PT-midnight reset."""


def token_path(slug: str) -> Path:
    d = data_dir() / "tokens"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{slug}.json"


def _secrets() -> Path:
    p = resolve_path(env("GOOGLE_CLIENT_SECRETS", "./secrets/client_secret.json"))
    if not p.exists():
        raise FileNotFoundError(f"OAuth client file not found: {p} (see README: Google Cloud setup)")
    return p


def authorize(slug: str) -> dict:
    """Browser consent. Pick the matching channel/brand account on the Google screen.

    Headless server: the consent URL is printed. Open an SSH tunnel from the PC you browse on
    (ssh -L 8085:localhost:8085 user@server), open the URL there, and Google's redirect to
    http://localhost:8085 travels back through the tunnel to this process."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    ch = db.get_channel_by_slug(slug)
    if not ch:
        raise ValueError(f"unknown channel slug {slug}")
    flow = InstalledAppFlow.from_client_secrets_file(str(_secrets()), SCOPES)
    port = int(settings.get("youtube.oauth_port", 8085))
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    msg = ("\n1) On the PC you browse from, keep this tunnel open:\n"
           f"     ssh -L {port}:localhost:{port} {getpass.getuser()}@<this-server>\n"
           "2) Open this URL in that PC's browser and pick the channel:\n\n{url}\n")
    creds = flow.run_local_server(host="localhost", port=port, open_browser=has_display,
                                  authorization_prompt_message="" if has_display else msg,
                                  prompt="consent", access_type="offline")
    token_path(slug).write_text(creds.to_json(), encoding="utf-8")
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    items = yt.channels().list(part="snippet", mine=True).execute().get("items", [])
    db.record_quota("channels.list", 1)
    if not items:
        raise RuntimeError("This Google account has no YouTube channel")
    yt_id, title = items[0]["id"], items[0]["snippet"]["title"]
    db.ex("UPDATE channels SET youtube_channel_id=?, youtube_title=?, authorized_at=? WHERE id=?",
          (yt_id, title, db.now_iso(), ch["id"]))
    return {"channel": slug, "youtube_channel_id": yt_id, "youtube_title": title}


def service(slug: str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    tp = token_path(slug)
    if not tp.exists():
        raise RuntimeError(f"Channel '{slug}' is not authorised. Run: ./ytm.sh auth {slug}")
    creds = Credentials.from_authorized_user_info(json.loads(tp.read_text(encoding="utf-8")), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            tp.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise RuntimeError(f"Token for '{slug}' is invalid - re-run auth")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _resumable(request) -> dict:
    from googleapiclient.errors import HttpError

    response, retries = None, 0
    while response is None:
        try:
            _status, response = request.next_chunk()
        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504) and retries < 8:
                retries += 1
                time.sleep(min(64, 2 ** retries) + random.random())
                continue
            raise
        except (ConnectionError, TimeoutError, OSError):
            if retries >= 8:
                raise
            retries += 1
            time.sleep(min(64, 2 ** retries))
    return response


def run(video_id: int) -> dict:
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    v = db.get_video(video_id)
    ch = db.get_channel(v["channel_id"])
    cfg = ch.get("config") or {}
    ycfg = {**(settings.get("youtube", {}) or {}), **(cfg.get("youtube", {}) or {})}
    need = UPLOAD_COST + THUMB_COST + (CAPTION_COST if ycfg.get("upload_captions") and v["format"] == "long" else 0)
    if db.quota_remaining() < need:
        raise QuotaDeferred(f"needs {need} units, {db.quota_remaining()} left today")

    yt = service(ch["slug"])
    now = datetime.now(timezone.utc)
    sched = datetime.fromisoformat(v["scheduled_at"]) if v.get("scheduled_at") else None
    status = {"selfDeclaredMadeForKids": bool(ycfg.get("made_for_kids", False)), "embeddable": True,
              "containsSyntheticMedia": bool(ycfg.get("contains_synthetic_media", False))}
    # AI-generated imagery can look like real footage: disclose it (YouTube "altered or synthetic content")
    if ycfg.get("disclose_ai_images", True) and db.one(
            "SELECT 1 AS x FROM assets WHERE video_id=? AND kind='ai_image' LIMIT 1", (video_id,)):
        status["containsSyntheticMedia"] = True
    if sched and sched > now + timedelta(minutes=10):
        status.update(privacyStatus="private", publishAt=sched.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        final_status = "scheduled"
    else:
        status["privacyStatus"] = ycfg.get("immediate_privacy", "public")
        final_status = "published"
    lang = cfg.get("language", "en")
    body = {"snippet": {"title": v["title"], "description": v["description"] or "", "tags": v.get("tags") or [],
                        "categoryId": str((cfg.get("youtube") or {}).get("category_id") or ycfg.get("category_id", "22")),
                        "defaultLanguage": lang, "defaultAudioLanguage": lang},
            "status": status}

    media = MediaFileUpload(v["video_path"], mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    try:
        resp = _resumable(yt.videos().insert(part="snippet,status", body=body, media_body=media))
    except HttpError as e:
        if "quotaExceeded" in str(e) or "uploadLimitExceeded" in str(e):
            db.record_quota("videos.insert(quotaExceeded)", max(0, db.quota_remaining()))
            raise QuotaDeferred(str(e)[:300]) from e
        raise
    db.record_quota("videos.insert", UPLOAD_COST)
    yt_id = resp["id"]
    db.update_video(video_id, youtube_video_id=yt_id)
    notes = []

    if v.get("thumbnail_path") and Path(v["thumbnail_path"]).exists():
        try:
            yt.thumbnails().set(videoId=yt_id, media_body=MediaFileUpload(v["thumbnail_path"], mimetype="image/jpeg")).execute()
            db.record_quota("thumbnails.set", THUMB_COST)
        except HttpError as e:  # custom thumbnails need a phone-verified channel
            code = getattr(getattr(e, "resp", None), "status", None)
            hint = ("YouTube refused the custom thumbnail: the channel must be phone-verified first "
                    "(youtube.com/verify, takes a minute; allow up to 24 h). " if code == 403 else "")
            notes.append(f"thumbnail skipped: {hint}{str(e)[:160]}")
            db.log_event(video_id, "thumbnail_skipped", f"{hint}{str(e)[:200]}", channel_id=v["channel_id"])
    if ycfg.get("upload_captions") and v["format"] == "long" and v.get("captions_srt"):
        try:
            yt.captions().insert(part="snippet", body={"snippet": {"videoId": yt_id, "language": lang, "name": "", "isDraft": False}},
                                 media_body=MediaFileUpload(v["captions_srt"], mimetype="application/octet-stream")).execute()
            db.record_quota("captions.insert", CAPTION_COST)
        except HttpError as e:
            notes.append(f"captions skipped: {str(e)[:160]}")

    db.update_video(video_id, published_at=db.now_iso() if final_status == "published" else None)
    return {"youtube_video_id": yt_id, "status": final_status, "url": f"https://youtu.be/{yt_id}", "notes": notes}
