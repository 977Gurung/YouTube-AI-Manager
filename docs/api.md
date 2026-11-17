# API

All endpoints except `/` need the header `X-API-Key: <YTM_API_KEY>`. Interactive docs: `http://<server>:8765/docs`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Dashboard |
| GET | `/api/health` | Health, version, GPU, worker, quota |
| GET | `/api/stats` | Counts per status and job state |
| GET | `/api/system?since=<t>` | System monitor samples (CPU, memory, disk, network, GPU, processes) |
| GET / POST | `/api/system/power` | Power status / `{"action": "restart-app" \| "reboot" \| "shutdown", "confirm": <same>, "force": false}` |
| GET | `/api/channels` | Channels and link status |
| POST | `/api/channels/sync` | Reload `config/channels.yaml` |
| GET | `/api/videos?status=&channel=&limit=` | List videos |
| GET / PATCH | `/api/videos/{id}` | Video details / edit script sections, title, description, tags, schedule |
| POST | `/api/videos/{id}/approve` | Approve the current gate |
| POST | `/api/videos/{id}/reject` | `{"action": "reject" \| "redo", "notes": "..."}` |
| POST | `/api/videos/{id}/retry` | Retry a failed step |
| GET | `/api/videos/{id}/next-slot` | Next free publishing slot |
| GET | `/media/{id}/{file}?key=` | Video files (final.mp4, thumbnail.jpg) |
| POST | `/api/research/run` | `{"channel": "<slug>"}` or `{}` for all |
| POST | `/api/pipeline/tick` | Move the pipeline forward now |
| GET | `/api/reviews/pending` | Items waiting for review (used by alerts) |
| POST | `/api/analytics/sync` | Fetch YouTube stats now |
| GET | `/api/analytics/summary`, `/api/analytics/digest` | Stats for the dashboard / daily digest text |
| GET | `/api/jobs` | Job queue and results |
| POST | `/api/jobs/{id}/cancel` | Cancel a queued job |
