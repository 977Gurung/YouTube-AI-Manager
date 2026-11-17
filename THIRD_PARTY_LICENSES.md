# Third-party licences

This repository contains only YT AI Manager's own code (MIT, see `LICENSE`). It does **not** include any AI model
weights, voices, fonts or third-party programs: `install.sh`, `./ytm.sh fetch-models` and `./ytm.sh setup-voice`
download them on your machine, and each keeps its own licence. You are responsible for following them,
especially if you monetise videos.

Status column: **checked** = read from the package metadata or licence text during the 1.0.1 review;
**verify** = taken from memory, confirm on the linked page before relying on it.

## AI models and voices

| Component | Used for | Licence | Key conditions | Status |
|---|---|---|---|---|
| [LTX-Video 2B distilled](https://huggingface.co/Lightricks/LTX-Video) (Lightricks) | AI animation | [LTXV Open Weights License](https://scancode-licensedb.aboutcode.org/ltxv-owl-2025-04-17.html) | Commercial use allowed for entities under USD 10M annual revenue (above: separate licence). Machine-generated content you publish must be clearly disclosed. Use restrictions (no deepfakes of real people without consent, no harmful misinformation, etc.) apply to you and to anyone you pass the model to. | checked |
| [SDXL base 1.0](https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0) (Stability AI) | AI images | CreativeML Open RAIL++-M | Commercial use allowed within the licence's use restrictions | verify |
| [SDXL-Lightning](https://huggingface.co/ByteDance/SDXL-Lightning) (ByteDance) | Fast AI images | CreativeML Open RAIL++-M ([source](https://osai-index.eu/model/sdxl-lightning)) | As above | checked |
| [sdxl-vae-fp16-fix](https://huggingface.co/madebyollin/sdxl-vae-fp16-fix) | Image decoder | MIT | — | verify |
| [Qwen2.5-7B-Instruct](https://ollama.com/library/qwen2.5) via Ollama | Research, scripts, SEO | Apache-2.0 | — | verify |
| Whisper "small" via faster-whisper | Captions | MIT | — | verify |
| [Chatterbox](https://github.com/resemble-ai/chatterbox) (Resemble AI) | Expressive voice | MIT | Adds an inaudible watermark to generated audio. Only clone a voice you own or have permission to use. | checked (package) |
| [Piper voices](https://huggingface.co/rhasspy/piper-voices) | Default voice | Per voice | Some voices are non-commercial: read the voice's `MODEL_CARD` | verify per voice |

The app sets YouTube's "altered or synthetic content" flag and notes AI visuals in the description whenever a video
uses AI images or animation (`youtube.disclose_ai_images`), which covers LTX-Video's disclosure condition for YouTube.

## Programs installed by `install.sh`

| Component | Licence | Notes | Status |
|---|---|---|---|
| [n8n](https://n8n.io) | [Sustainable Use License](https://docs.n8n.io/n8n-community-license/community-license/license-faq) (fair-code, not OSI open source) | Free for your own internal use, such as running your channels. Not allowed: selling n8n itself or hosting it as a service for others. | checked (FAQ) |
| [Ollama](https://ollama.com) | MIT | — | checked |
| Node.js 24 (NodeSource) | MIT | — | verify |
| ffmpeg (Ubuntu package) | LGPL-2.1+ / GPL-2.0+ (Ubuntu build) | Called as a separate program | verify |
| NVIDIA driver | Proprietary NVIDIA licence | Installed from Ubuntu | verify |
| [Anton font](https://fonts.google.com/specimen/Anton) | SIL Open Font License 1.1 | Thumbnails and captions | verify |
| Liberation / DejaVu fonts (Ubuntu packages) | SIL OFL 1.1 / Bitstream Vera-style | Fallback fonts | verify |

## Python packages (`requirements*.txt`)

Read from each package's metadata (versions current at the time of the review):

| Package | Licence |
|---|---|
| fastapi, pydantic, PyYAML, RapidFuzz, faster-whisper, chatterbox-tts | MIT |
| pillow | MIT-CMU (HPND) |
| uvicorn, python-dotenv | BSD-3-Clause |
| feedparser | BSD-2-Clause |
| requests, google-api-python-client, google-auth-oauthlib, google-auth-httplib2, diffusers, transformers, accelerate, peft, huggingface_hub, safetensors | Apache-2.0 |
| torch | BSD-3-Clause style with bundled Apache-2.0/MIT/BSL components |
| **piper-tts** (1.3 and later) | **GPL-3.0-or-later** |
| **edge-tts** | **LGPL-3.0** |
| sentencepiece, protobuf, tzdata | Apache-2.0 / BSD-3-Clause / Apache-2.0 (verify) |
| nvidia-cublas-cu12, nvidia-cudnn-cu12 | NVIDIA proprietary redistributable licence |

piper-tts and edge-tts are installed from PyPI and imported at run time; they are not part of this repository.

## Your own content

Music in `assets/music`, clips in `assets/broll`, stock footage (Pexels / Pixabay keys) and voice reference
recordings are yours to license. Stock credits are stored per video and added to its description.
