# Installation

**In this guide:** [Requirements](#requirements) · [Prepare the machine](#1-prepare-the-machine) ·
[Install](#2-install) · [Google Cloud](#3-google-cloud-one-time-about-10-minutes) ·
[Channels](#4-add-and-link-your-channels) · [Open the dashboard](#5-open-the-dashboard) · [Upgrading](#upgrading)

## Requirements

| | Minimum | Recommended |
|---|---|---|
| OS | Ubuntu 22.04 LTS | Ubuntu 24.04 LTS (Desktop or Server) |
| GPU | NVIDIA, 8 GB VRAM, NVENC (GTX 16xx / RTX 20xx or newer) | RTX 2070 or better |
| RAM | 16 GB (AI animation off) | 32 GB (AI animation's text encoder needs ~20 GB free for a minute) |
| Disk | 40 GB free | 100 GB free: models ~45 GB, environments ~10 GB, ~0.5 GB per long video |
| Network | Internet for install, research, uploads | Fixed LAN IP / DHCP reservation |

Accounts: a Google account with a YouTube channel, and a Google Cloud project (free) for the YouTube Data API.
Optional: Pexels / Pixabay keys (stock footage), ElevenLabs (paid voice), a Discord/Slack/Teams webhook (alerts).

## 1. Prepare the machine
- Install **Ubuntu Desktop 24.04 LTS** or **Ubuntu Server 24.04 LTS** (22.04 also works). Desktop is easier and lets
  you use the dashboard on the machine itself; its desktop session costs about 0.3–0.7 GB of VRAM.
- Desktop installer: tick **Install third-party software for graphics and Wi-Fi hardware** (installs the NVIDIA
  driver). On Desktop, `install.sh` also installs OpenSSH and **disables suspend** (pass `--allow-sleep` to keep it).
- Laptops: set the lid to do nothing when closed (`HandleLidSwitch=ignore` in `/etc/systemd/logind.conf`).
- **Secure Boot:** easiest to disable in the BIOS. If you keep it, the driver install asks for a password and you
  choose *Enroll MOK* on the next boot.

## 2. Install
Get the code onto the server, either from a release zip or with git:
```bash
# A) release zip (copy it to the server first, e.g. scp yt-ai-manager-v1.0.1.zip user@server:~/)
unzip yt-ai-manager-v1.0.1.zip && cd yt-ai-manager

# B) git
git clone https://github.com/977Gurung/YouTube-AI-Manager.git yt-ai-manager && cd yt-ai-manager
```
Then, as your normal user (it uses `sudo` when needed):
```bash
chmod +x install.sh && ./install.sh
```
If no NVIDIA driver is present it installs the recommended one and asks you to **reboot and run it again**.

What it does (re-runnable; finished steps are skipped):
apt packages (Python, ffmpeg with NVENC + libass, fonts) → NVIDIA driver → NVENC test → Ollama → Python
environment (`.venv`) with CUDA 12 libraries → `.env` with a random `YTM_API_KEY` → `config/channels.yaml` from the
template → timezone from the machine → LLM (`qwen2.5:7b-instruct`), Piper voice, thumbnail font → database →
Node.js 24 + n8n + the 5 workflows (published) → `ytm-api` and `ytm-n8n` systemd services → dashboard power
permission → `./ytm.sh doctor`.

| Option | Effect |
|---|---|
| `--lan` | Dashboard and n8n reachable from your LAN (firewall limited to your subnet) |
| `--no-ai-images` | Skip PyTorch/diffusers and the AI image model |
| `--llm-model llama3.1:8b` | Use another Ollama model |
| `--piper-voice en_US-lessac-medium` | Use another Piper voice |
| `--allow-sleep` | Desktop: keep automatic suspend |
| `--skip-driver`, `--skip-models`, `--skip-n8n`, `--cpu-only`, `--no-services` | Skip those steps |

> Use the full **`nvidia-driver-XXX`** package (what `ubuntu-drivers install` picks). The `-headless` / `-server`
> variants lack the video encoder, so rendering falls back to the CPU.

**Optional large downloads** (run once, can take an hour):
```bash
./ytm.sh fetch-models         # AI images (SDXL, ~7.5 GB) + AI animation (LTX-Video, ~25 GB, needs 40 GB free)
./ytm.sh setup-voice --enable # expressive voice (Chatterbox, ~6 GB) and switch your channels to it
```

## 3. Google Cloud (one time, about 10 minutes)
1. [console.cloud.google.com](https://console.cloud.google.com) → new project → enable **YouTube Data API v3**.
2. **Credentials → Create credentials → API key** → put it in `.env` as `YOUTUBE_API_KEY` (research + analytics).
3. **OAuth consent screen**: External, add your Google account as a test user, then **Publish app → In production**.
   While "Testing", refresh tokens expire every 7 days and uploads stop. Unverified is fine for personal use (you
   click through a warning).
4. **Credentials → OAuth client ID → Desktop app** → download the JSON and copy it to the server as
   `secrets/client_secret.json`:
   ```bash
   scp client_secret_*.json user@server:~/yt-ai-manager/secrets/client_secret.json
   ```
5. Optional stock keys (Pexels, Pixabay) in `.env`. Then `./ytm.sh restart`.

## 4. Add and link your channels
Edit `config/channels.yaml` (see [Configuration](configuration.md#channelsyaml)), load it and link each channel:
```bash
./ytm.sh sync-channels
./ytm.sh auth <slug>
```
At the Ubuntu Desktop, the browser opens and you pick the channel. Over SSH it prints a tunnel command to run
**on your PC** (`ssh -L 8085:localhost:8085 user@server`) and a Google URL to open in your PC's browser. The token is
saved in `data/tokens/<slug>.json`. Repeat per channel, then `./ytm.sh doctor`.

Custom thumbnails need a phone-verified channel ([youtube.com/verify](https://www.youtube.com/verify)).

## 5. Open the dashboard
| Where you are | Dashboard | n8n |
|---|---|---|
| On the Ubuntu Desktop | http://localhost:8765 | http://localhost:5678 |
| Another PC (default, secure) | run `ssh -N -L 8765:localhost:8765 -L 5678:localhost:5678 user@server`, then http://localhost:8765 | http://localhost:5678 |
| LAN mode (`--lan`) | http://SERVER-IP:8765 | http://SERVER-IP:5678 |
| Anywhere, privately | Tailscale (see below) | |

The dashboard asks once for the API key (`./ytm.sh key`). In n8n, create the owner account on first visit; the 5
workflows are already published.

**Remote access with Tailscale** (no router changes): install Tailscale on the server
(`curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`) and on your phone/PC with the same
account, set `app.host: 0.0.0.0` in `config/settings.yaml` and `N8N_LISTEN_ADDRESS=0.0.0.0` in `config/n8n.env`,
`./ytm.sh restart`, then open `http://<server's 100.x address>:8765`. If ufw is on:
`sudo ufw allow in on tailscale0`. Never port-forward the dashboard on your router.

## Upgrading
From a release zip:
```bash
./scripts/upgrade.sh ~/yt-ai-manager-vX.Y.Z.zip
```
From a git clone:
```bash
git pull && ./scripts/upgrade.sh --in-place
```
Kept: `.env`, `config/channels.yaml`, `config/n8n.env`, `data/`, `secrets/`, `models/`, `assets/`, `logs/`, the
environments. `config/settings.yaml` is replaced by the new defaults (old copy: `settings.yaml.bak`; the LLM model,
timezone and host carry over) unless you pass `--keep-settings`. The script shows the old → new version and the
release notes, warns before a downgrade, updates packages, migrates the database, restarts the services and runs
`doctor`.
