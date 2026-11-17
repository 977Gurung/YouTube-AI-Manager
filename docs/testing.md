# Testing

| Check | Time | Needs | Command |
|---|---|---|---|
| Static checks + fast tests (also run by GitHub Actions) | ~1 min | `requirements.txt`, `requirements-dev.txt` | `./scripts/check.sh` |
| Full unit and API suite | ~30 min | + `requirements-ai.txt`, ffmpeg | see below |
| Live end-to-end run | ~15 min | + Playwright Chromium, n8n | `python tests/live/run_live.py <workdir> [n8n_bin_dir]` |

Full suite (CPU only; no GPU, YouTube account or model downloads needed):
```bash
.venv/bin/pip install -r requirements-dev.txt
YTM_API_KEY= .venv/bin/python -m pytest tests -q -p no:cacheprovider --ignore=tests/live
```
It builds tiny random-weight image and video models (`tests/tools/`), fakes every outside service
(`tests/fakes.py`: LLM, YouTube, Google, Reddit, RSS, stock sites, voice) and uses its own temporary database, so
your real data is never touched.

The live run starts a real API server, drives the dashboard in Chromium (desktop and phone sizes), produces videos
through the real worker, and runs the five n8n workflows against it. `tests/live/check_system_tab.py` checks the
System tab against a server with a simulated GPU (`tests/live/fake-nvidia-smi`).

`./scripts/check.sh` also fails if a secret file (`.env`, OAuth client, tokens, your `channels.yaml`) is tracked by
git or a key-shaped string appears in the code.
