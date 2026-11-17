"""Command line tools.
  python -m app.cli init-db            create/upgrade the database and load channels.yaml
  python -m app.cli auth <slug>        OAuth-link a YouTube channel (opens browser)
  python -m app.cli doctor             check Ollama, GPU, NVENC, ffmpeg, keys, tokens
  python -m app.cli research <slug>    run research for one channel now (foreground)
  python -m app.cli run <video_id> <script|produce|upload>   run one stage now (foreground)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from . import db, pipeline
from .config import data_dir, env, settings


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def run_quiet(cmd) -> str:
    import subprocess
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def doctor() -> int:
    from .gpu import nvenc_ok, nvidia_smi
    from .llm import health
    import shutil

    from . import __version__
    print(f"YT AI Manager {__version__}")
    checks = []
    def add(name, ok, info=""):
        checks.append((name, ok, info))

    g = nvidia_smi()
    add("NVIDIA GPU", g.get("ok"), f"{g.get('name')} {g.get('mem_total_mb')} MB" if g.get("ok") else g.get("error"))
    add("ffmpeg", bool(shutil.which("ffmpeg")), shutil.which("ffmpeg") or "not on PATH")
    add("NVENC encoder", nvenc_ok(), "h264_nvenc" if nvenc_ok() else "will use libx264 (slower)")
    o = health()
    add("Ollama", o.get("ok"), o.get("error", ""))
    add(f"LLM model {o.get('model')}", o.get("model_present"), "" if o.get("model_present") else f"run: ollama pull {o.get('model')}")
    try:
        import faster_whisper  # noqa: F401
        add("faster-whisper", True)
    except Exception as e:  # noqa: BLE001
        add("faster-whisper", False, str(e)[:120])
    from .agents import voice as voice_agent
    vs = voice_agent.chatterbox_status()
    used = [c["slug"] for c in db.q("SELECT slug, config_json FROM channels WHERE active=1")
            if ((json.loads(c["config_json"] or "{}").get("voice") or {}).get("provider") == "chatterbox")]
    add("Voice: expressive (Chatterbox)", vs["ready"],
        (f"ready - used by {', '.join(used)}" if used else "ready - enable per channel: voice.provider: chatterbox") if vs["ready"]
        else ("NOT installed but a channel uses it (falls back to Piper): ./ytm.sh setup-voice" if used
              else "not installed (optional, more natural voice): ./ytm.sh setup-voice"))
    add("YTM_API_KEY", bool(env("YTM_API_KEY")), "protects the API/dashboard")
    import requests
    try:
        n8n_ok = requests.get("http://127.0.0.1:5678/healthz", timeout=3).status_code == 200
    except requests.RequestException:
        n8n_ok = False
    node = shutil.which("node")
    node_v = (run_quiet([node, "-v"]) if node else "") or "not installed"
    add("n8n (schedules + alerts)", n8n_ok, "running on :5678" if n8n_ok else
        f"not reachable on :5678 - check ./ytm.sh logs n8n (n8n 2.x needs Node.js 24+, this machine has {node_v})")
    add("YOUTUBE_API_KEY", bool(env("YOUTUBE_API_KEY")), "research + analytics (optional but recommended)")
    from .agents import imagegen
    from .agents.media import broll_library
    broll = len(broll_library({}))
    stock = [k for k in ("PEXELS_API_KEY", "PIXABAY_API_KEY") if env(k)]
    ai = imagegen.status()
    if not ai["enabled"]:
        ai_ok, ai_info = False, "disabled in settings.yaml (ai_images.enabled)"
    elif not ai.get("libs"):
        ai_ok, ai_info = False, "libraries missing: .venv/bin/pip install -r requirements-ai.txt"
    elif not ai.get("downloaded"):
        ai_ok, ai_info = False, "model not downloaded yet: ./ytm.sh fetch-models (~7.5 GB)"
    else:
        ai_ok, ai_info = True, "SDXL-Lightning ready" + ("" if ai.get("cuda") else " (CPU only - very slow)")
    add("Visuals: own clips (assets/broll)", broll > 0, f"{broll} clip(s)" if broll else "none yet (optional)")
    add("Visuals: stock API keys", bool(stock), ", ".join(stock) if stock else "none (optional)")
    add("Visuals: AI images", ai_ok, ai_info)
    from .agents import animate
    av = animate.status()
    if not av["enabled"]:
        av_ok, av_info = False, "off in settings.yaml (ai_video.enabled) - videos use moving-camera stills"
    elif not av.get("libs"):
        av_ok, av_info = False, av.get("error") or "libraries missing: .venv/bin/pip install -r requirements-ai.txt"
    elif not av.get("downloaded"):
        av_ok, av_info = False, "model not downloaded yet: ./ytm.sh fetch-models (~25 GB) - until then videos use stills"
    else:
        av_ok, av_info = True, f"LTX-Video ready, {av.get('mode')}" + ("" if av.get("cuda") else " (CPU only - far too slow)")
    add("Visuals: AI animation", av_ok, av_info)
    add("Visuals: at least one source", broll > 0 or bool(stock) or ai_ok, "videos need one of the three above")
    from .agents.upload import _secrets
    try:
        add("OAuth client_secret.json", bool(_secrets()))
    except FileNotFoundError as e:
        add("OAuth client_secret.json", False, str(e))
    for ch in db.q("SELECT slug FROM channels WHERE active=1"):
        add(f"channel token: {ch['slug']}", (data_dir() / "tokens" / f"{ch['slug']}.json").exists(),
            f"run: ./ytm.sh auth {ch['slug']}")
    width = max(len(c[0]) for c in checks) + 2
    for name, ok, info in checks:
        print(f"  [{'OK' if ok else '!!'}] {name.ljust(width)} {info or ''}")
    return 0 if all(c[1] for c in checks[:5]) else 1


def status() -> int:
    import requests

    url = f"http://127.0.0.1:{settings.get('app.port', 8765)}/api/health"
    try:
        h = requests.get(url, headers={"X-API-Key": env("YTM_API_KEY") or ""}, timeout=10).json()
    except Exception as e:  # noqa: BLE001
        print(f"API not reachable at {url}: {e}")
        return 1
    g, q = h.get("gpu", {}), h.get("quota", {})
    job = (h.get("current_job") or {}).get("kind", "idle")
    print(f"healthy={h.get('ok')} worker={h.get('worker_alive')} job={job} nvenc={h.get('nvenc')} "
          f"disk_free={h.get('disk_free_gb')}GB quota_left={q.get('remaining')} failed={h.get('failed_videos')}")
    if g.get("ok"):
        print(f"gpu: {g['name']} {g['mem_used_mb']}/{g['mem_total_mb']} MB, {g['util_pct']}% util, {g['temp_c']}C")
    else:
        print(f"gpu: {g.get('error')}")
    print(f"ollama: {'ok' if h.get('ollama', {}).get('ok') else 'DOWN'} model={h.get('ollama', {}).get('model')} "
          f"present={h.get('ollama', {}).get('model_present')}")
    return 0 if h.get("ok") else 1


def main(argv=None) -> int:
    logging.basicConfig(level=settings.get("app.log_level", "INFO"), format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    sub.add_parser("sync-channels")
    sub.add_parser("doctor")
    a = sub.add_parser("auth"); a.add_argument("slug")
    r = sub.add_parser("research"); r.add_argument("slug")
    s = sub.add_parser("run"); s.add_argument("video_id", type=int); s.add_argument("stage", choices=["script", "produce", "upload"])
    sub.add_parser("tick")
    sub.add_parser("status")
    sub.add_parser("fetch-models")
    vsm = sub.add_parser("voice-sample"); vsm.add_argument("slug"); vsm.add_argument("text", nargs="?")
    args = p.parse_args(argv)

    db.init_db()
    if args.cmd in ("init-db", "sync-channels"):
        print(f"Database ready at {db.db_path()} - {db.sync_channels()} channel(s) loaded")
    elif args.cmd == "doctor":
        return doctor()
    elif args.cmd == "auth":
        from .agents.upload import authorize
        _print(authorize(args.slug))
    elif args.cmd == "research":
        ch = db.get_channel_by_slug(args.slug)
        if not ch:
            print(f"unknown channel {args.slug}")
            return 1
        _print(pipeline.run_job({"kind": "research", "channel_id": ch["id"], "video_id": None}))
    elif args.cmd == "run":
        job = {"kind": args.stage, "video_id": args.video_id, "channel_id": None}
        try:
            _print(pipeline.run_job(job))
        except Exception as e:
            pipeline.on_job_failed(job, e)
            raise
    elif args.cmd == "fetch-models":
        from .agents import imagegen
        print("Downloading SDXL base + Lightning + fp16 VAE (~7.5 GB) into models/hf ...")
        _print(imagegen.download())
        from .agents import animate
        if animate.enabled():
            import shutil
            free = shutil.disk_usage(str(data_dir())).free / 2**30
            if free < 40:
                print(f"Skipping the AI animation model: needs ~40 GB free, only {free:.0f} GB left.")
            else:
                print("Downloading the AI animation model LTX-Video (~25 GB, once) and testing it on this GPU ...")
                try:
                    _print(animate.download())
                except Exception as e:  # noqa: BLE001 - images still work without it
                    print(f"!! AI animation model not ready: {e}\n   Videos keep using moving-camera stills. Retry: ./ytm.sh fetch-models")
    elif args.cmd == "voice-sample":
        from .agents import voice as voice_agent
        try:
            _print(voice_agent.sample(args.slug, args.text))
        except Exception as e:  # noqa: BLE001
            print(f"voice sample failed: {e}")
            return 1
    elif args.cmd == "status":
        return status()
    elif args.cmd == "tick":
        _print(pipeline.tick())
    return 0


if __name__ == "__main__":
    sys.exit(main())
