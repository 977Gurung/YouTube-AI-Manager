# Architecture

## Components
```mermaid
flowchart LR
    subgraph n8n["n8n (service ytm-n8n, :5678)"]
        direction TB
        W1["01 Daily research"]
        W2["02 Pipeline tick"]
        W3["03 Review alerts"]
        W4["04 Analytics + digest"]
        W5["05 Health watchdog"]
    end
    subgraph api["YT AI Manager (service ytm-api, :8765)"]
        REST["REST API + dashboard"]
        DB[("SQLite<br/>data/ytm.db")]
        WK["Worker<br/>one job at a time"]
        JP["Job process<br/>(produce)"]
        MON["System monitor"]
    end
    YOU(["You<br/>browser / phone"]) --> REST
    n8n -- "HTTP + X-API-Key" --> REST
    REST --- DB
    WK --- DB
    WK -- "spawns, exits after the job" --> JP
    WK --> OLL["Ollama<br/>qwen2.5:7b"]
    JP --> GPU["GPU: SDXL images,<br/>LTX-Video animation,<br/>Whisper, NVENC"]
    JP --> VOICE["Voice: Chatterbox / Piper"]
    WK --> YT["YouTube Data API"]
    MON --> SYS["/proc + nvidia-smi"]
```

## Production steps
```mermaid
flowchart LR
    R["Research"] --> S["Script"] --> V["Voice"] --> M["Visuals<br/>own clips → stock → AI images"]
    M --> A["AI animation<br/>(LTX-Video)"] --> C["Captions<br/>(Whisper)"] --> E["Render<br/>(ffmpeg NVENC)"]
    E --> T["Thumbnail"] --> O["Title, description, tags"] --> U["Upload + schedule"] --> X["Analytics"]
    X -. "next day's ideas" .-> R
```

## Video status
```mermaid
stateDiagram-v2
    [*] --> idea_pending: research
    idea_pending --> idea_approved: you approve
    idea_pending --> rejected
    idea_approved --> scripting
    scripting --> script_review
    script_review --> script_approved: you approve
    script_review --> scripting: regenerate with notes
    script_approved --> producing
    producing --> final_review
    final_review --> approved: you approve
    final_review --> producing: re-produce with notes
    approved --> uploading
    uploading --> scheduled
    scheduled --> published
    scripting --> failed
    producing --> failed
    uploading --> failed
    failed --> scripting: retry
    failed --> producing: retry
    failed --> uploading: retry
```
Gates that are switched off for a channel (`approval_gates`) are passed automatically.

## Design choices for an 8 GB GPU
- **One job at a time.** The worker never runs two GPU jobs together; the LLM is unloaded before images, animation
  or Whisper load.
- **Video production runs in its own process** (`app/jobrun.py`) that exits after each job, so the several GB of
  model weights it loads are returned to the system. The service itself stays at a few hundred MB.
- **AI animation** encodes the text prompt on the CPU first, frees that encoder, then loads the video model with CPU
  offload and tiled decoding.
- **Fail soft.** Missing models, out-of-memory errors or time limits turn scenes into stills, or fall back to Piper
  for the voice, with the reason in the video's History, instead of failing the video.
- **Resumable.** Each step keeps its output; a retry or a restart continues from the last finished step.

## Project layout
```
app/
  server.py        REST API, dashboard, media files       worker.py   job queue (one at a time)
  pipeline.py      steps and status changes                jobrun.py   runs one job in its own process
  cli.py           ./ytm.sh commands                       sysmon.py   System tab samples
  power.py         restart / shut down                     config.py, db.py, llm.py, gpu.py, states.py, util.py
  agents/          research, script, voice, media, imagegen, animate, captions, video, thumbnail, seo, upload, analytics
  dashboard/       index.html, app.js, system.js, style.css, icons, manifest
config/            settings.yaml, channels.example.yaml (+ your channels.yaml, n8n.env)
db/schema.sql      channels, research_items, videos, assets, jobs, events, analytics_snapshots, quota_usage
deploy/            systemd units, sudoers rule for the power buttons
n8n/               the five workflows
scripts/           env, run-api, upgrade, setup-voice, setup-power, voice_worker, check
tests/             unit/API tests, fakes, tiny-model builders, live browser + n8n harness
docs/              these pages
data/              (created) ytm.db, videos/<id>/, cache/, tokens/, n8n/
```
