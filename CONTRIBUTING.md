# Contributing

Thanks for helping. A few rules keep the project easy to run on a single home server.

1. **Run the checks** before you push: `./scripts/check.sh` (about a minute). For changes to agents, the pipeline
   or the API, also run the full suite (about 30 minutes, CPU only, no real YouTube or GPU needed):
   ```bash
   .venv/bin/pip install -r requirements-dev.txt
   YTM_API_KEY= .venv/bin/python -m pytest tests -q -p no:cacheprovider --ignore=tests/live
   ```
2. **Version every change** ([Semantic Versioning](https://semver.org)): PATCH for fixes, MINOR for features, MAJOR
   when users must do something by hand. Update `VERSION`, `app/__init__.py` and add an entry at the top of
   `CHANGELOG.md`; `./scripts/check.sh` fails if they disagree.
3. **No secrets, no personal data** in code, tests or screenshots. Use the fake services in `tests/fakes.py`.
4. **Keep it GPU-light.** The reference machine is an RTX 2070 with 8 GB VRAM; new models must fit next to nothing
   else and must fail soft (fall back) rather than stop a video.
5. **Shell scripts use LF line endings** (enforced by `.gitattributes`).
