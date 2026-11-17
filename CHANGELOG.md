# Changelog

All notable changes to YT AI Manager. Versions follow [Semantic Versioning](https://semver.org):
**MAJOR** = changes that need manual steps or break existing config/data, **MINOR** = new features,
**PATCH** = fixes and small tweaks. The installed version: `./ytm.sh version`, the dashboard header, or `GET /api/health`.

## [1.0.1] - 2026-11-17
Prepared for publishing on GitHub. No change to how the app works.

### Added
- `LICENSE` (MIT), `THIRD_PARTY_LICENSES.md`, `SECURITY.md`, `CONTRIBUTING.md`
- `.gitignore`, `.gitattributes` (Linux line endings for scripts), `.editorconfig`
- `scripts/check.sh` and a GitHub Actions workflow: syntax, config files, version consistency, secret scan, fast tests
- `scripts/upgrade.sh --in-place` for upgrading a git clone after `git pull`
- Documentation split into `docs/` (installation, configuration, usage, architecture with diagrams, API, testing,
  troubleshooting) with screenshots; new README

### Fixed
- n8n workflows had a fixed `Asia/Dubai` workflow timezone; they now ship as `UTC` and `install.sh` sets the
  machine's timezone when it imports them (existing installs are not affected)
- Extra gap in the "Approve all visible ideas" button label

### Changed
- `config/channels.yaml` is now created from `config/channels.example.yaml` on install and kept out of git
- Default timezone is `UTC`; `install.sh` sets the machine's own timezone (existing installs keep theirs)
- Test data uses neutral names; sandbox-only installer harness (`tests/shell`) removed;
  System tab browser check moved to `tests/live/`
- Old diagrams replaced by Mermaid diagrams in `docs/architecture.md`

## [1.0.0] - 2026-11-17
First production release.

### Production pipeline
- Multi-channel, semi-automatic YouTube production for Shorts and long videos on one GPU (RTX 2070, 8 GB)
- Agents: research, script, voice, media, AI images, AI animation, captions, video, thumbnail, SEO, upload, analytics
- Approval gates per channel (idea, script, final video); scheduled publishing in free slots; YouTube quota tracking
- AI images: SDXL + SDXL-Lightning; own clips (assets/broll) and optional stock footage
- AI animation: LTX-Video 2B turns stills into moving clips (Shorts fully, long videos at key scenes)
- Expressive voice: Chatterbox (emotion strength, optional reference recording), falling back to Piper
- Animated "pop" captions with key-word effects; karaoke and plain styles
- Research across several countries (`trends_geo: "AE, US"`)

### Dashboard
- Review, Pipeline, Channels, Analytics, Jobs and System tabs; mobile-friendly, light and dark mode, Add to Home Screen
- System tab: Task Manager-style live CPU, memory, disk, network and GPU graphs, process lists
- Power controls: Restart app, Restart server, Shut down
- Video download buttons; thumbnail refusals explained in the video's History

### Operations
- Ubuntu installer, `scripts/upgrade.sh`, systemd services, n8n 2.x workflows (schedules, alerts, watchdog, digest)
- Low idle memory: video production runs in its own short-lived process
- `./ytm.sh doctor` health check; self-test suite in `tests/`
