# Clipper

YouTube link → vertical clips with captions → TikTok + Instagram Reels. The target is the GTA 6 launch on **Nov 19, 2026**.
Read [CLAUDE.md](CLAUDE.md) first. Claude Code loads it automatically.

## Setup (both devs)
1. Install Python 3.11+, [uv](https://docs.astral.sh/uv/), and FFmpeg.
   On Windows: `winget install Python.Python.3.12 astral-sh.uv Gyan.FFmpeg`
2. `uv sync`
3. `cp .env.example .env` and fill in your keys.
4. `uv run pytest`

## Before you start
- [ ] Get an Anthropic API key.

## Plan
The design, scope, decisions, and the day-by-day split between Dev A (engine) and Dev B (distribution) are in **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**: see section 10 for the timeline.
Every pipeline stage exposes `run(job: Job, force: bool = False) -> Job` in its `__init__.py`.
