"""SQLite access helpers (one connection per thread, autocommit, WAL)."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import ROOT, data_dir, load_channels, settings

_local = threading.local()

# column -> decoded key added to returned dicts
JSON_FIELDS = {
    "script_json": "script",
    "timings_json": "timings",
    "tags_json": "tags",
    "config_json": "config",
    "meta_json": "meta",
    "result_json": "result",
}
_ALIASES = {"script": "script_json", "timings": "timings_json", "tags": "tags_json"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def now_iso() -> str:
    return now_utc().isoformat()


def db_path():
    return data_dir() / "ytm.db"


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(db_path(), timeout=30, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=30000")
        _local.conn = c
    return c


def _decode(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for col, key in JSON_FIELDS.items():
        if col in d:
            raw = d[col]
            try:
                d[key] = json.loads(raw) if raw else None
            except (TypeError, ValueError):
                d[key] = None
    return d


def q(sql: str, params=()) -> list[dict]:
    return [_decode(r) for r in conn().execute(sql, params).fetchall()]


def one(sql: str, params=()) -> dict | None:
    return _decode(conn().execute(sql, params).fetchone())


def ex(sql: str, params=()) -> int:
    return conn().execute(sql, params).lastrowid


def jdump(v) -> str:
    return json.dumps(v, ensure_ascii=False)


# --------------------------------------------------------------------------- setup
def init_db() -> int:
    conn().executescript((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
    return sync_channels()


def sync_channels() -> int:
    """Upsert channels.yaml into the channels table; deactivate removed ones."""
    seen = []
    for ch in load_channels():
        slug = ch["slug"]
        seen.append(slug)
        row = one("SELECT id FROM channels WHERE slug=?", (slug,))
        vals = (ch["name"], ch.get("niche", ""), ch.get("language", "en"), jdump(ch), 1 if ch.get("active", True) else 0, now_iso())
        if row:
            ex("UPDATE channels SET name=?, niche=?, language=?, config_json=?, active=?, updated_at=? WHERE id=?", (*vals, row["id"]))
        else:
            ex("INSERT INTO channels(name, niche, language, config_json, active, updated_at, slug, created_at) VALUES (?,?,?,?,?,?,?,?)",
               (*vals, slug, now_iso()))
    if seen:
        ex(f"UPDATE channels SET active=0 WHERE slug NOT IN ({','.join('?' * len(seen))})", seen)
    return len(seen)


# --------------------------------------------------------------------------- entities
def get_channel(cid: int) -> dict | None:
    return one("SELECT * FROM channels WHERE id=?", (cid,))


def get_channel_by_slug(slug: str) -> dict | None:
    return one("SELECT * FROM channels WHERE slug=?", (slug,))


def get_video(vid: int) -> dict | None:
    return one(
        "SELECT v.*, c.slug AS channel_slug, c.name AS channel_name FROM videos v "
        "JOIN channels c ON c.id = v.channel_id WHERE v.id=?", (vid,))


def update_video(vid: int, **fields) -> None:
    if not fields:
        return
    sets, vals = [], []
    for k, v in fields.items():
        col = _ALIASES.get(k, k)
        if not col.replace("_", "").isalnum():
            raise ValueError(f"bad column {col}")
        if col.endswith("_json") and v is not None and not isinstance(v, str):
            v = jdump(v)
        sets.append(f"{col}=?")
        vals.append(v)
    sets.append("updated_at=?")
    vals.extend([now_iso(), vid])
    ex(f"UPDATE videos SET {', '.join(sets)} WHERE id=?", vals)


def log_event(video_id=None, action: str = "", detail: str = "", actor: str = "system", channel_id=None) -> None:
    ex("INSERT INTO events(video_id, channel_id, actor, action, detail, created_at) VALUES (?,?,?,?,?,?)",
       (video_id, channel_id, actor, action, (detail or "")[:4000], now_iso()))


# --------------------------------------------------------------------------- quota
_PT = ZoneInfo("America/Los_Angeles")


def quota_day() -> str:
    return datetime.now(_PT).strftime("%Y-%m-%d")


def quota_used() -> int:
    return int(one("SELECT COALESCE(SUM(units),0) AS u FROM quota_usage WHERE day=?", (quota_day(),))["u"])


def quota_remaining() -> int:
    return int(settings.get("youtube.daily_quota", 10000)) - quota_used()


def record_quota(operation: str, units: int) -> None:
    ex("INSERT INTO quota_usage(day, operation, units, created_at) VALUES (?,?,?,?)",
       (quota_day(), operation, int(units), now_iso()))
