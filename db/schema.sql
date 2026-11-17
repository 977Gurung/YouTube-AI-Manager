-- YouTube AI Manager - SQLite schema (WAL mode, foreign keys on).
-- All timestamps are ISO-8601 UTC strings, e.g. 2026-11-03T15:30:00+00:00

CREATE TABLE IF NOT EXISTS channels (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    slug                TEXT NOT NULL UNIQUE,
    name                TEXT NOT NULL,
    niche               TEXT,
    language            TEXT NOT NULL DEFAULT 'en',
    config_json         TEXT NOT NULL DEFAULT '{}',   -- full block from channels.yaml
    youtube_channel_id  TEXT,                          -- filled by `cli auth`
    youtube_title       TEXT,
    authorized_at       TEXT,
    active              INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL,
    updated_at          TEXT
);

-- Raw trend / competitor / feed signals collected by the research agent.
CREATE TABLE IF NOT EXISTS research_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id  INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    run_id      TEXT NOT NULL,
    source      TEXT NOT NULL,          -- google_trends | rss:<host> | reddit:r/x | youtube_trending | competitors | youtube_search
    title       TEXT NOT NULL,
    url         TEXT,
    score       REAL NOT NULL DEFAULT 0,
    meta_json   TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_research_channel ON research_items(channel_id, created_at);

-- One row per video, from idea to published. `status` drives the pipeline:
-- idea_pending -> idea_approved -> scripting -> script_review -> script_approved
-- -> producing -> final_review -> approved -> uploading -> scheduled | published
-- (+ rejected, failed). Approval gates can be switched off per channel.
CREATE TABLE IF NOT EXISTS videos (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id       INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    format           TEXT NOT NULL CHECK (format IN ('short', 'long')),
    status           TEXT NOT NULL,
    resume_status    TEXT,              -- status to return to when a failed stage is retried
    topic            TEXT NOT NULL,
    angle            TEXT,
    hook             TEXT,
    rationale        TEXT,
    idea_score       REAL,
    feedback         TEXT,              -- reviewer notes fed into regeneration
    script_json      TEXT,              -- {format, title_options, thumbnail_text, sections:[{text, visual, chapter}]}
    script_text      TEXT,
    timings_json     TEXT,              -- per-section start/end seconds in the voice track
    voice_path       TEXT,
    captions_srt     TEXT,
    captions_ass     TEXT,
    video_path       TEXT,
    thumbnail_path   TEXT,
    duration_sec     REAL,
    title            TEXT,
    description      TEXT,
    tags_json        TEXT,
    youtube_video_id TEXT,
    scheduled_at     TEXT,
    published_at     TEXT,
    notified_status  TEXT,              -- last review status that n8n was notified about
    error            TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT
);
CREATE INDEX IF NOT EXISTS ix_videos_status  ON videos(status);
CREATE INDEX IF NOT EXISTS ix_videos_channel ON videos(channel_id, status);
CREATE INDEX IF NOT EXISTS ix_videos_sched   ON videos(channel_id, scheduled_at);

-- Every downloaded/generated media file, with licence/attribution.
CREATE TABLE IF NOT EXISTS assets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id    INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,          -- stock_video | voice | music | thumbnail | captions
    path        TEXT NOT NULL,
    provider    TEXT,
    provider_id TEXT,
    author      TEXT,
    source_url  TEXT,
    license     TEXT,
    meta_json   TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_assets_video ON assets(video_id);

-- Work queue processed by the single GPU worker (one job at a time = no VRAM fights).
CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL CHECK (kind IN ('research', 'script', 'produce', 'upload', 'analytics')),
    video_id     INTEGER REFERENCES videos(id) ON DELETE CASCADE,
    channel_id   INTEGER REFERENCES channels(id) ON DELETE CASCADE,
    status       TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    priority     INTEGER NOT NULL DEFAULT 0,
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    result_json  TEXT,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status, priority, id);

-- Audit trail: approvals, rejections, uploads, errors.
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id    INTEGER REFERENCES videos(id) ON DELETE CASCADE,
    channel_id  INTEGER REFERENCES channels(id) ON DELETE CASCADE,
    actor       TEXT NOT NULL DEFAULT 'system',
    action      TEXT NOT NULL,
    detail      TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_video ON events(video_id);

CREATE TABLE IF NOT EXISTS analytics_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id         INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    youtube_video_id TEXT NOT NULL,
    views            INTEGER NOT NULL DEFAULT 0,
    likes            INTEGER NOT NULL DEFAULT 0,
    comments         INTEGER NOT NULL DEFAULT 0,
    captured_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_snap_video ON analytics_snapshots(video_id, captured_at);

-- YouTube Data API quota bookkeeping (quota day = midnight Pacific Time).
CREATE TABLE IF NOT EXISTS quota_usage (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    day        TEXT NOT NULL,
    operation  TEXT NOT NULL,
    units      INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_quota_day ON quota_usage(day);

CREATE VIEW IF NOT EXISTS v_latest_stats AS
SELECT s.*
FROM analytics_snapshots s
WHERE s.id = (SELECT s2.id FROM analytics_snapshots s2
              WHERE s2.video_id = s.video_id
              ORDER BY s2.captured_at DESC, s2.id DESC LIMIT 1);
