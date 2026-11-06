# Clipper — CLAUDE.md

Two people build this repo by vibe-coding with Claude Code. Read this whole file before you change anything.
**The design, scope, and decisions live in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Read the section for the module you're touching. Don't build anything listed under "Out of scope" or "Stretch" until the MVP works end to end.** If a task needs a different design, stop and propose an edit to ARCHITECTURE.md instead of improvising.
**Work is split into tasks in [docs/TASKS.md](docs/TASKS.md).** When asked to "do task X", follow that task's spec and its *Done when* line. Stay inside its folders and tick its checkbox in the same PR. Dev A is the friend (engine). Dev B is the repo owner (distribution).

## What we're building
A tool that takes a YouTube link, cuts it into several vertical short clips (9:16, 15–60s), writes a caption and hashtags for each clip, suggests a trending sound, and makes posting them to TikTok and Instagram Reels nearly one click. It targets GTA 6 gaming content, and **the deadline is the GTA 6 launch (Nov 19, 2026)**. **Phase 1** = the two of us run it locally for our own accounts. **Phase 2** = a SaaS for other creators, after launch (ARCHITECTURE.md §13). Shipping beats polish. Only build what the pipeline needs.

## Pipeline (each stage is a separate module)
```
URL/file ─► ingest ─► analyze ─► captions ─► render ─► review (UI) ─► post queue ─► post kit (UI)
            yt-dlp    whisper +   Claude:     ffmpeg    approve/edit    SQLite        copy · show file
                      signals +   hook,       9:16 +                                  · open upload page
                      Claude rank caption,    subtitles                               · mark as posted
                                  sound pick
```
The processes are `clipper ui` (Streamlit dashboard) and the `clipper run` pipeline it spawns. `clipper sounds refresh` updates the trending sounds list.
Stages pass data **only through `workspace/jobs/<job_id>/manifest.json`**. That file follows the `Job` model in `clipper/models.py`. Each stage reads the manifest, does its work, fills in its own fields, updates `status`, and writes the manifest back. No stage imports another stage's internals.

```
workspace/jobs/<job_id>/
  manifest.json      # the Job model, the single source of truth
  source.mp4, audio.wav  # ingest
  transcript.json    # analyze (Transcript model)
  clips/<clip_id>.mp4 / .ass  # render
workspace/clipper.db # post queue (Post model). Only clipper/publish touches it.
```

## Ownership (to avoid merge conflicts)
| Area | Owner | Folders |
|---|---|---|
| Engine: download, highlight detection, rendering | **Dev A** | `clipper/ingest`, `clipper/analyze`, `clipper/render` |
| Distribution: captions, dashboard, posting, scheduling | **Dev B** | `clipper/captions`, `clipper/ui`, `clipper/publish` |
| Shared contract | **both must agree** | `clipper/models.py`, `clipper/config.py`, `clipper/llm.py`, `clipper/cli.py`, `pyproject.toml`, `docs/ARCHITECTURE.md` |

- Stay inside your own folders. If you need a change in a shared file, keep it to a small PR of its own and tell the other person.
- **Changing `clipper/models.py` breaks the other person's work.** Only add optional fields. Never rename or remove a field without agreeing first. Update `tests/fixtures/sample_manifest.json` in the same PR.
- Dev B doesn't need to wait for Dev A. Build and test against `tests/fixtures/sample_manifest.json`, `sample_transcript.json`, and any short mp4.
- **Nothing posts automatically in Phase 1.** Don't add code that uploads to TikTok or Instagram, or a background posting loop, without an ARCHITECTURE.md change first (D18).

## Git workflow
- `main` must always run. **Never commit directly to `main`.**
- Branch names: `a/<feature>` or `b/<feature>` (e.g. `a/highlight-detection`, `b/tiktok-upload`).
- One feature per branch, kept small. Open a PR, and **the author merges their own PR** once ruff and pytest pass. No approval is needed.
- Exception: a PR that touches a shared file (see Ownership) needs a 👍 comment from the other person before it's merged.
- Before opening a PR, run `git pull origin main` (rebase or merge), then `uv run ruff check . && uv run pytest`.
- Commit messages use the imperative mood: "Add TikTok draft upload".
- Never commit `.env`, cookies, tokens, `workspace/` contents, or video/audio files. `.gitignore` covers these, so don't override it.

## Stack and commands
- Python 3.11+, managed with **uv**. FFmpeg must be on PATH.
- `uv sync`: install dependencies
- `uv run clipper run <url_or_file>`: pipeline up to render (ready for review)
- `uv run clipper stage <ingest|analyze|captions|render> <job_id> [--force]`: run one stage
- `uv run clipper ui`: the dashboard (submit, review, post kit, sounds)
- `uv run clipper sounds refresh` / `uv run clipper sounds list`: update / show the trending sounds
- `uv run clipper resume <job_id>`: retry a failed job, skipping finished steps
- `uv run pytest`: tests. `uv run ruff check . --fix && uv run ruff format .`: lint and format
- Libraries: `yt-dlp` (download), `faster-whisper` (transcription), `scenedetect` (scene cuts), `ffmpeg` via `subprocess` (all video work, no moviepy), `anthropic` (LLM), `httpx` (fetching trending-sound pages), `pyperclip` (post kit clipboard), `typer` (CLI), `pydantic` (models), `streamlit` (dashboard), `sqlite3` (post queue, stdlib).
- LLM: use the Anthropic SDK with the model from `config.py` (default `claude-opus-5-5`). **Always call Claude through `clipper.llm.ask_json`** (structured output plus pydantic validation, retry, refusal fallback). Never create your own client.

## Code conventions
- Type hints everywhere. Use `pathlib.Path`, never string paths, because the code must run on both Windows and macOS/Linux.
- Run ffmpeg with `subprocess.run([...], check=True)` and a list of arguments, never `shell=True`.
- Read every setting from `clipper/config.py`, which loads `.env`. No hardcoded keys, paths, or account names.
- Stages must be **re-runnable**. If an output file already exists, skip that work unless `--force` is passed.
- Use the `logging` module, not `print`, except for CLI output.
- Tests go in `tests/`. Use small fixtures. Don't download real videos in tests.

## Hard rules from the design (full detail is in docs/ARCHITECTURE.md)
- **Never burn copyrighted or "trending" songs into the video file.** The default audio is the original game audio. Trending sounds get added by hand in the TikTok app, and captions only *suggests* one from `trending_sounds.yaml`.
- **Phase 1 posting is manual-assisted (D18).** There are no TikTok or Instagram APIs. The dashboard's post kit prepares everything and a human posts. Never add browser automation that fills in upload pages. Auto-posting (Upload-Post API) is a stretch item.
- The `UNIQUE(job_id, clip_id, platform)` constraint in the post queue must stay: it stops a clip from being queued twice on one platform.
- Only post clips with `review == approved`, unless `AUTO_APPROVE=true`.
- Every LLM call validates its output with a pydantic model and has a non-LLM fallback, so the pipeline never dies because of Claude output.
- On Windows, ffmpeg's `subtitles=` filter breaks on absolute paths. Run ffmpeg with `cwd=` set to the clip folder and pass a relative `.ass` filename.
- Only process sources we're allowed to repost, and keep `credit` filled in for anything that isn't our own gameplay.

## Definition of done for a PR
1. Ruff is clean and pytest passes.
2. The stage still works on the sample manifest.
3. The PR description says what changed and how to test it in about three lines.
