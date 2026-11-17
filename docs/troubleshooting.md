# Troubleshooting

Start with `./ytm.sh doctor`: it checks every part and prints the fix for anything missing.

| Symptom | Fix |
|---|---|
| `nvidia-smi` fails after the driver install | Reboot; with Secure Boot on, enroll the MOK key at boot (or disable Secure Boot) |
| `NVENC unavailable` | Install the full `nvidia-driver-XXX` (not `-headless`), then `./ytm.sh doctor` |
| Scripts fail with `bash\r: No such file or directory` | The files got Windows line endings. Clone with git (`.gitattributes` fixes it) or run `sed -i 's/\r$//' *.sh scripts/*.sh` |
| Captions are `proportional` in the job result | Whisper failed: check `./ytm.sh logs api` for `libcudnn` / `libcublas`; re-run `.venv/bin/pip install -r requirements-gpu.txt` |
| Dark placeholder scenes | `./ytm.sh doctor` → *Visuals*: run `./ytm.sh fetch-models`, add clips to `assets/broll`, or set a stock key; then **Re-produce** |
| Out of GPU memory | Something else uses VRAM (`nvidia-smi`); or use `lightning_file: sdxl_lightning_4step_lora.safetensors` with `steps: 4` |
| Scenes not animated | The video's History says why; `./ytm.sh doctor` → *AI animation*; `./ytm.sh fetch-models` needs 40 GB free |
| Voice is Piper instead of Chatterbox | History shows `voice_fallback` with the reason; `./ytm.sh doctor | grep -i voice`; `./ytm.sh setup-voice` |
| `auth` hangs | The SSH tunnel for port 8085 must be open on the PC whose browser you use |
| `invalid_grant` on upload | OAuth app still in Testing (7-day tokens): publish it and run `./ytm.sh auth <slug>` again |
| Thumbnail not applied | Verify the channel by phone (youtube.com/verify); YouTube Shorts mostly ignore custom thumbnails |
| Upload deferred | Daily quota used; it continues after midnight Pacific time |
| n8n asks for email and password | Create the owner account on first visit. Forgot it: `sudo systemctl stop ytm-n8n; N8N_USER_FOLDER=$HOME/yt-ai-manager/data/n8n n8n user-management:reset; sudo systemctl start ytm-n8n` |
| n8n login loop over plain HTTP | Set `N8N_SECURE_COOKIE=false` in `config/n8n.env`, `./ytm.sh restart` |
| Dashboard not reachable from another device | `app.host: 0.0.0.0` in settings.yaml and `N8N_LISTEN_ADDRESS=0.0.0.0` in n8n.env, `./ytm.sh restart`; check `sudo ss -ltnp | grep 8765` |
| Power buttons greyed out | `./scripts/setup-power.sh` (asks for your sudo password once) |
| High memory while idle | Should be a few hundred MB for the app after a video; check the System tab's process list. `worker.isolate` must include `produce` |
| Research finds nothing | Jobs tab → `source_errors`; feeds and Reddit can rate-limit, so add more feeds |
