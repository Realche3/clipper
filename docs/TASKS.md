# Tasks

**Dev A (friend): Engine.** Download, highlight detection, rendering.
**Dev B (repo owner): Distribution.** Captions, dashboard, posting, accounts.

How to work on a task:
1. `git checkout main && git pull`
2. `git checkout -b <branch from the table>`
3. Tell Claude: **"Do task A3 from docs/TASKS.md"**. Claude reads CLAUDE.md and ARCHITECTURE.md on its own.
4. Make ruff and pytest pass, then open a PR and **merge it yourself**. Tick the box in the same PR. If it touches a shared file (`models.py`, `config.py`, `llm.py`, `cli.py`, `pyproject.toml`, the docs), get a 👍 from the other dev first.

Each task's section in [ARCHITECTURE.md](ARCHITECTURE.md) has the full spec. ⛔ marks a task that must merge before the task that depends on it.

---

## Day 0: today (Nov 4)
- ~~Dev B: create TikTok and Meta developer apps~~ (not needed: Phase 1 posting is manual-assisted, D18)
- [ ] **Dev B:** get an Anthropic API key and share it with Dev A privately (not in git)
- [ ] **Both:** install Python 3.11+, uv, and FFmpeg. Clone, `uv sync`, `uv run pytest` should be green.

---

## Dev A: Engine

| # | Task | Branch | Target | Depends on |
|---|---|---|---|---|
| A1 | Ingest | `a/ingest` | Nov 5 | — |
| A2 | Transcribe | `a/transcribe` | Nov 6 | A1 |
| A3 | Signals + candidates | `a/signals` | Nov 8 | A1, A2 |
| A4 | Claude ranker | `a/rank` | Nov 9 | A3, ⛔ B1 |
| A5 | Render: blur layout + encode | `a/render` | Nov 10 | A1 |
| A6 | Subtitles + hook overlay | `a/subtitles` | Nov 11 | A5 |
| A7 | Speed + quality pass | `a/tuning` | Nov 13–16 | M1 |

- [x] **A1 Ingest** (`clipper/ingest`): yt-dlp download (≤1080p mp4, H.264 preferred) to `source.mp4`. YouTube URLs only. Extract `audio.wav` (16 kHz mono). Fill in `title`, `description`, `channel`, `credit`, `duration` (from yt-dlp metadata). Reject sources longer than `MAX_SOURCE_MINUTES`. Support `YTDLP_COOKIES_FROM_BROWSER`.
  *Done when:* `clipper run <url> --until ingest` leaves `status=ingested`. A test uses a 5s video generated with ffmpeg `testsrc` (no network).
- [x] **A2 Transcribe** (`clipper/analyze/transcribe.py`): faster-whisper with word timestamps and VAD, saved to `transcript.json` as the `Transcript` model. Skip if the file exists, unless `--force`.
- [x] **A3 Signals + candidates** (`analyze/signals.py`, `analyze/candidates.py`): per-second loudness, cut density, and speech score, then combined score, peak picking, and windows snapped to pauses. **Pure functions with unit tests** on synthetic arrays.
- [x] **A4 Ranker** (`analyze/rank.py`): send the candidates to `clipper.llm.ask_json`. Claude returns the final `CLIPS_PER_VIDEO` clips with start, end, score, and reason. Fall back to the top N by score. Leaves `status=analyzed`.
- [x] **A5 Render** (`clipper/render`): ffmpeg cut, blur layout, loudnorm, and H.264 1080x1920 at 30fps. Writes `clips/<id>.mp4` and sets `clip.file`. Add `--clip <id>` to `clipper stage` (a small change to the shared `cli.py`, so tell Dev B). Leaves `status=rendered`.
- [x] **A6 Subtitles + hook** (`render/subtitles.py`): build the `.ass` file from words, 2–4 words per line with the current word highlighted. `hook_text` sits in the top third for 0–3s. Use the Windows `cwd=` trick. Pure ASS generator with unit tests.
- [ ] **A7 Tuning:** a 20-min video must finish in ≤ 15 min (fall back to whisper `base` if not), check that clips start on the action, and tune the scoring weights.

---

## Dev B: Distribution

| # | Task | Branch | Target | Depends on |
|---|---|---|---|---|
| B1 | Shared `llm.py` ⛔ | `b/llm` | **Nov 5** | — |
| B2 | Captions | `b/captions` | Nov 6 | B1 |
| B3 | Post queue + slots | `b/queue` | Nov 7 | — |
| B4 | DryRun client + scheduler | `b/scheduler` | Nov 8 | B3 |
| B5 | Post kit (manual-assisted posting) | `b/post-kit` | Nov 8 | B4, B8 |
| B6 | Trending sounds fetch + Claude pick | `b/sounds` | Nov 9 | B2 |
| B7 | Dashboard v1 | `b/ui` | Nov 11 | B3 |
| B8 | Dashboard v2: edit → re-render | `b/ui-edit` | Nov 13–14 | B7, A5 |

- [x] **B1 `clipper/llm.py`** *(shared, merge first so A4 can use it)*: `ask_json(system: str, prompt: str, schema: type[BaseModel], max_tokens=...) -> BaseModel`. It uses the Anthropic SDK and `config.CLAUDE_MODEL`, validates with pydantic, retries once, then raises `LLMError`. Tests mock the client. Tell Dev A when it's merged.
- [x] **B2 Captions** (`clipper/captions`): one Claude call per job. Fills in `hook_text`, `title`, `caption`, `hashtags` (+ `BASE_HASHTAGS`), `vibe`, and `suggested_sound` from `trending_sounds.yaml`. Fallback: the video title plus base hashtags. Leaves `status=captioned`. Test it on `tests/fixtures/sample_manifest.json`.
- [x] **B3 Queue + slots** (`publish/queue.py`, `publish/slots.py`): SQLite `posts` table with `UNIQUE(job_id, clip_id, platform)`. `enqueue(job, clip)` adds one row per enabled platform. `next_slot()` respects the interval, posting window, and daily cap. **Unit tests for the slotting.**
- [x] **B4 Scheduler** (`publish/scheduler.py`, `publish/dryrun.py`): a loop every 60s that claims due rows with `scheduled→uploading`, calls the client, and records the result. Retries at 5, 15, and 45 min. A row stuck for more than 15 min becomes `needs_check`. Use `DryRunClient` whenever `DRY_RUN=true`. Add the `clipper scheduler` command.
- [ ] **B5 Post kit** (`clipper/ui`, `publish/`; ARCHITECTURE.md §5.6): `POST_MODE=manual` (default) in config. The Queue page gets a **Due now** section. Each post has a **Start posting** button that copies the platform text to the clipboard, reveals the clip in Explorer/Finder and opens the upload page, plus the suggested sound with a search link and **Mark as posted** (also from `scheduled`). `clipper scheduler` refuses to start in manual mode.
  *Done when:* approve a clip, it shows as due at its slot, one click prepares everything, and Mark as posted moves it to Posted.
- [ ] **B6 Trending sounds** (`clipper/sounds`; ARCHITECTURE.md §5.7): `clipper sounds refresh` fetches each page in `TRENDING_SOURCES`, has Claude extract `{title, artist, platform, vibe}`, and merges with `trending_sounds.yaml` into `workspace/trending_sounds.json`. Captions passes the list to Claude to pick a sound per clip. The dashboard gets a Sounds page with a Refresh button and the date of the last update.
  *Done when:* a real refresh returns 100+ sounds, and a new job's clips get suggestions from that list.
- [x] **B7 Dashboard v1** (`clipper/ui/app.py`, `clipper ui` command):
  - Paste a URL plus an optional **context note** ("what is this video about?") to spawn `clipper run --context ...` as a subprocess. Show the job list with status.
  - Clip grid with a video player, approve/reject, and editable caption and hashtags.
  - Approving calls `enqueue`. The queue view shows each post's status.
  - TikTok drafts get a **copy caption** button and the suggested sound.
- [x] **B8 Dashboard v2:** editing the hook, start, or end runs `clipper stage render <job> --clip <id> --force`. Add views for errors, failed posts, and `needs_check`. Add a manual "mark as posted" button for TikTok drafts.

---

## Together
- [ ] **M1 (Nov 12): end to end.** A real video becomes clips, gets approved, and posts with `DRY_RUN=false` to **private or test** posts.
- [ ] **Rehearsal (Nov 13–16):** post real pre-launch GTA 6 content on the live accounts. Fix whatever hurts.
- [ ] **Code freeze (Nov 17):** bug-fix PRs only after this.
- [ ] **Launch (Nov 19):** follow the runbook in ARCHITECTURE.md §11.
