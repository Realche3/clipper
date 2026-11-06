# Clipper: Architecture, Decisions & Scope

Status: **v1 design. Nov 6 update: Phase 1 posting is manual-assisted (D18), trending sounds are fetched (D19), and Phase 2 is a SaaS (§13).** If you want to change a decision, open a PR that edits this file first.

Dates: today is Nov 4, 2026, **GTA 6 launches Nov 19**, and code freezes Nov 17.

---

## 1. Goal and success criteria

Turn one long gaming video into **5 ready-to-post vertical clips in under 15 minutes** on a normal laptop. Then get them onto TikTok and Instagram Reels with as little manual work as the platforms allow.

The MVP is done when all of these hold:
1. Paste a YouTube URL and get 5 clips: 1080x1920, 15–60s, burned-in subtitles, and a hook line on screen.
2. Every clip has a title, a caption, and hashtags.
3. A human approves or rejects clips in a dashboard and can tweak the caption, hook, or trim.
4. Approved clips get a suggested posting time and a **post kit**: one click copies the caption, shows the file and opens the TikTok or Instagram upload page. A human finishes the post (D18).
5. A clip is never queued twice on the same platform, and the dashboard shows what's been posted.

## 2. Scope

### In scope (MVP, by Nov 12)
| Area | What we do |
|---|---|
| Input | YouTube URL. One video per job, ≤ 90 min. |
| Highlight detection | Audio loudness, scene cuts, and speech reactions give candidate moments. Claude picks the final clips and sets their start and end. |
| Render | 9:16 blurred-background layout, word-by-word subtitles, hook text for the first 3s, loudness normalized. |
| Copy | Claude writes the hook, title, caption, and hashtags, and picks a suggested sound from the fetched trending list (§5.7). |
| Review | Streamlit dashboard: submit a URL, watch progress, preview clips, edit, approve or reject, re-render one clip. |
| Publish | **Manual-assisted (`POST_MODE=manual`)**: a post kit per clip and platform (copy caption, show file, open upload page, suggested sound, mark as posted). No platform APIs. |
| Scheduling | Per-platform queue with spacing, posting window and daily cap. In manual mode the times are reminders: the Queue page shows what's due now. |
| Trending sounds | `clipper sounds refresh` fetches 100+ currently trending TikTok and Reels songs from public weekly roundups. Claude extracts them, and the dashboard can refresh the list. |

### Stretch (only after the MVP works end to end)
- Facecam layout (manual crop preset per channel)
- Royalty-free music mix (`MUSIC_MODE=library`)
- YouTube Shorts upload
- **Auto-posting (`POST_MODE=auto`)** through the Upload-Post API: one client for TikTok and Instagram that plugs into the existing scheduler. The scheduler, retries and `needs_check` are already built for it.
- Phone share-sheet button (Web Share API) to send a clip straight into the TikTok or Instagram editor. Needs the dashboard on HTTPS, e.g. via a tunnel.
- Claude vision ranking (send frames from each candidate)
- Running on a VPS so the laptop can sleep

### Out of scope (we're not building these)
Multiple accounts per platform · user logins or auth (Phase 2) · analytics and the view-count feedback loop · browser automation that fills in upload pages (against platform terms, and it risks bans) · a timeline editor · AI face tracking · microservices, Docker, Redis, Celery · cloud hosting for the MVP.

## 3. Platform reality (this shapes the design)

| Constraint | Consequence for us |
|---|---|
| No posting API can attach in-app trending sounds. Burning copyrighted music into the file gets the video muted or the account struck. | Default audio is **the original game audio**. The human adds the trending sound inside the TikTok or Instagram editor while posting. |
| Our own TikTok and Meta API apps need reviews that take days, and an unaudited TikTok app can only post privately. | **Phase 1 uses no posting APIs** (D18). Posting is manual-assisted, which also lets the human use in-app trending sounds. |
| A website can't push a video into the TikTok or Instagram apps or upload pages. Only phones can, through the share sheet, and only on HTTPS. | Desktop post kit: copy caption, reveal the file in Explorer/Finder, open the upload page, drag the file in. The phone share sheet is a stretch item. |
| There's no official trending-audio API. TikTok's Creative Center chart needs a signed browser session. Public weekly roundups (Metricool, SocialPilot, HeyOrca) are plain HTML. | Fetch the roundups and have Claude extract `{title, artist, platform, vibe}`. The source list is configurable and is merged with the hand-kept `trending_sounds.yaml`. |
| Platforms push down reposted, unchanged content ("unoriginal"). | Prefer our own gameplay or permitted sources. Always change the clip (crop, subtitles, hook) and credit the source. |
| YouTube sometimes blocks yt-dlp ("confirm you're not a bot"). | Support `YTDLP_COOKIES_FROM_BROWSER`, and keep yt-dlp updated. |

## 4. System overview

```
                ┌──────────────── Streamlit dashboard (clipper ui) ────────────────┐
                │ submit URL · progress · preview · edit · approve · queue view    │
                └───────┬───────────────────────────────▲────────────────┬─────────┘
                        │ spawns subprocess             │ reads          │ enqueue approved
                        ▼                               │                ▼
 ┌───────────────────── Pipeline worker (clipper run) ──┴───┐    ┌──────────────────┐
 │ ingest → analyze → captions → render                     │    │ SQLite post queue│
 │   each stage: run(job, force) -> job                     │    │  workspace/      │
 │   state: workspace/jobs/<id>/manifest.json               │    │  clipper.db      │
 └──────────────────────────────────────────────────────────┘    └────────▲─────────┘
                                                                          │ claims due posts
                                              ┌───────────────────────────┴─────────┐
                                              │ Scheduler (clipper scheduler)       │
                                              │ loop every 60s → TikTok / IG client │
                                              └─────────────────────────────────────┘
```

It runs as three local processes from one Python package, with no servers to deploy. The scheduler runs on the **posting machine**, which must stay awake during the posting window.

## 5. Pipeline stages

Stage order: **ingest → analyze → captions → render**. Captions runs before render because the hook text gets burned into the video.

### 5.1 ingest (Dev A)
- Input: a YouTube URL. Uses `yt-dlp -f "bv*[height<=1080][ext=mp4][vcodec^=avc1]+ba[ext=m4a]/bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]"` and merges to `source.mp4`. H.264 comes first because plain `ext=mp4` can be AV1, which decodes slowly on laptops.
- Writes to the manifest: `title`, `description`, `channel`, `credit`, `duration`, `source_file`. Rejects sources longer than `MAX_SOURCE_MINUTES`.
- Also extracts `audio.wav` (16 kHz mono) once. Transcription and the loudness analysis both reuse it.

### 5.2 analyze (Dev A)
1. **Transcript:** faster-whisper (`WHISPER_MODEL=small`, int8, CPU or CUDA, `vad_filter=True`, `word_timestamps=True`), saved to `transcript.json` (the `Transcript` model).
2. **Signals, one value per second:**
   - `loud`: momentary loudness from `audio.wav` (numpy RMS in dB), z-scored.
   - `cuts`: scene-cut count in a 5s window (PySceneDetect `ContentDetector` on a 360p downscale).
   - `speech`: words per second plus a reaction-keyword hit list ("no way", "bro", "what", laughter, shouting) from the transcript.
   - `score = 0.5*loud + 0.3*cuts + 0.2*speech`, smoothed with a 3s moving average.
3. **Candidates:** take the top 15 peaks, at least 20s apart. Window = peak − 8s → peak + 20s, snapped to the nearest pause in the transcript, then clamped to `[CLIP_MIN, CLIP_MAX]`.
4. **Claude ranker:** send the video title, each candidate's times, signal stats, and transcript snippet. It returns JSON for the best `CLIPS_PER_VIDEO` clips: refined `start`/`end` (inside the window), `score`, and `reason`. Validate it with pydantic. **Fallback:** if the LLM fails, take the top N by score.
5. Write `clips[]` to the manifest. Status becomes `analyzed`.

### 5.3 captions (Dev B)
- One Claude call per job (all clips together, so the captions don't repeat each other). Inputs, most trusted first: the poster's `context` note given at submit time, the source title, channel and description, then each clip's reason and transcript snippet. **Nothing is hardcoded to GTA**, so Claude infers the niche from these inputs.
- Output per clip: `hook_text` (≤ 6 words, shown on screen for the first 3s), `title`, `caption` (≤ 150 chars, no hashtags), and `hashtags` (3–5, niche-specific, plus the optional account-wide `BASE_HASHTAGS`).
- `suggested_sound`: a match from `trending_sounds.yaml` by `vibe`, which Claude also picks (hype, funny, chill, or dramatic).
- Status becomes `captioned`.

### 5.4 render (Dev A)
- One ffmpeg call per clip: `-ss start -to end -i source.mp4`, re-encoded so the cut is frame-accurate.
- Blur layout filter:
  `[0:v]split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:2[bg];[b]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2`
- Subtitles: build an `.ass` file from the word timestamps (2–4 words per line, current word highlighted, bold, centered in the lower third). The hook goes on the top third for 0–3s. Burn it with `subtitles=clip.ass`. **On Windows, run ffmpeg with `cwd` set to the clip folder and pass a relative `.ass` filename.** Absolute Windows paths break the filter.
- Audio: `loudnorm=I=-14:TP=-1.5`. Output: `libx264 -preset veryfast -crf 20 -r 30 -pix_fmt yuv420p`, `aac 128k`, `-movflags +faststart`.
- Supports `--clip <id>` so the dashboard can re-render one clip after an edit. Status becomes `rendered`, which means ready for review.

### 5.5 review (Dev B, dashboard)
- Clips start as `review=pending`. The dashboard shows each video with editable hook, caption, hashtags, start, and end.
- Editing the hook, start, or end re-renders that clip. Approve sets `review=approved` and adds one queue row per enabled platform.
- `AUTO_APPROVE=true` skips the review step. It's off by default because a bad clip can hurt the account.

### 5.6 publish and schedule (Dev B)

> **Phase 1 = `POST_MODE=manual` (D18).** Approving a clip still books a slot per platform, but the slot is a *reminder*. The Queue page lists posts that are **due now**, each with a post kit:
> - **Start posting**: copies the platform-formatted text (`publish/text.py`) to the clipboard, reveals the clip file in Explorer/Finder, and opens the upload page (TikTok Studio upload / Instagram).
> - The suggested sound, with a link that searches for it.
> - **Mark as posted** (optional link): `scheduled` / `uploaded_draft` / `needs_check` → `published`.
>
> `clipper scheduler` refuses to start in manual mode, so nothing is ever "posted" by the DryRun client by mistake. The scheduler, retries and clients below stay for `POST_MODE=auto` (Stretch / Phase 2).

- **Queue (`workspace/clipper.db`, table `posts`):** id, job_id, clip_id, platform, status, scheduled_at, attempts, remote_id, url, error, updated_at. UNIQUE(job_id, clip_id, platform), which is the double-post guard.
- **Slotting:** the next free slot per platform, at least `POST_INTERVAL_MINUTES` after the previous one, inside `POSTING_WINDOW` (e.g. 10:00–23:30 local), under `DAILY_CAP_<PLATFORM>`. Otherwise it moves to the next day.
- **Scheduler loop (every 60s):** claim due rows (`scheduled → uploading`, in one transaction), call the client, then set `published` / `uploaded_draft` or `failed`.
  - Retry with backoff (5, 15, then 45 min): up to 3 retries, 4 tries in total. A retry that falls outside the posting window moves to the next window start.
  - A row stuck in `uploading` for more than 15 min becomes `needs_check`. **We never retry blindly**, because that's how double posts happen.
- **Clients:** they share one interface, `publish(job, clip) -> PostResult` (`publish/clients.py`). A client raises `PublishError(retryable=...)` only when it is **sure nothing went out**: retryable gets retried, otherwise the post is `failed`. Any other exception, including `UploadStateUnknown`, sends the post to `needs_check`.
  - `UploadPostClient` (auto mode, Stretch): one client for TikTok and Instagram through the Upload-Post API.
  - `DryRunClient`: logs only. It's used whenever `DRY_RUN=true`, which is **the default**.
- **Instagram caption:** `caption + "\n\n" + hashtags + "\n🎥 " + credit`.
- **TikTok:** the dashboard shows the caption and hashtags with a copy button, plus the suggested sound.

### 5.7 trending sounds (Dev B)
- `clipper sounds refresh`, or the **Refresh** button on the dashboard, downloads each page in `TRENDING_SOURCES` (weekly roundups of trending TikTok and Reels audio). It strips the HTML to text and makes one `ask_json` call per page to extract `[{title, artist, platform, vibe}]`.
- Results are merged and deduped by (title, artist). Hand-pinned entries from `trending_sounds.yaml` come first. They're saved to `workspace/trending_sounds.json` with `fetched_at`.
- A source that fails is logged and skipped, and the last good list is kept. The target is 100+ sounds.
- **Captions:** Claude gets the current list (name, artist, platform, vibe) and picks the best fit for each clip. Fallback: a vibe-based pick, as before.
- The dashboard shows the suggestion with a search link (`tiktok.com/search?q=<title> <artist>`), so finding it in the app takes one tap.
- We only ever store **song names**, never audio.

## 6. Data and state

| Data | Where | Writer |
|---|---|---|
| Job, clip times, captions, review state | `workspace/jobs/<id>/manifest.json` (`Job` model) | The pipeline while running. The dashboard only after the job is `rendered`. |
| Transcript | `workspace/jobs/<id>/transcript.json` (`Transcript` model) | analyze |
| Media | `source.mp4`, `audio.wav`, `clips/<clip_id>.mp4` and `.ass` | ingest, render |
| Post queue | `workspace/clipper.db` (`Post` model) | dashboard (enqueue), scheduler (status) |
| Secrets | `.env`, `secrets/tokens.json` | human, auth commands |
| Logs | `workspace/jobs/<id>/job.log`, `workspace/scheduler.log` | each process |

Rules:
- Only one process writes a manifest at a time. Which one depends on the job status in the table above.
- Write manifests atomically: write a temp file, then `os.replace`.
- Manifest = what a clip *is*. Queue = what happened to it *on a platform*. That's why there are no post fields on the manifest.

## 7. Decision log

| # | Decision | Why | Rejected alternatives |
|---|---|---|---|
| D1 | Python 3.11 + uv, one package | Best video and ML libraries. uv makes setup identical on Windows and Mac. | Node (weak ML), Go |
| D2 | A local monolith with 3 processes (ui / worker / scheduler) | 2 weeks, 2 people. Nothing to deploy. | Microservices, Docker, a cloud backend |
| D3 | Stages communicate only through `manifest.json` | Both devs can work in parallel against fixtures. Easy to debug, and stages can resume. | Shared DB for everything, in-memory pipeline |
| D4 | SQLite only for the post queue | Needs atomic claims and uniqueness. The scheduler and dashboard write at the same time. | JSON queue (race conditions), Redis |
| D5 | No job queue. The dashboard spawns `clipper run` as a subprocess, one job at a time. | Whisper and ffmpeg already max out the CPU, so parallel jobs don't help. | Celery, RQ, Redis |
| D6 | Highlights = signal scoring, then Claude ranking | Gameplay often has little speech, so text-only LLM picking misses action. The signals keep LLM input small and cheap. | Transcript-only LLM (AutoClip style), vision on every frame (slow, expensive) |
| D7 | faster-whisper `small` int8 on CPU | About 4–8 min for 20 min of audio on a laptop. Works without a GPU. | OpenAI Whisper API (cost, upload time), YouTube auto-captions (bad timings). Kept as a fallback. |
| D8 | ffmpeg via subprocess, ASS subtitles via libass | Fastest, full control, styled word highlighting | moviepy (slow), Remotion |
| D9 | Captions before render | The hook text gets burned into the video. | Render twice |
| D10 | Claude `claude-opus-5-5` at `medium` effort, through `llm.ask_json`: structured output, 1 retry, server-side refusal fallback, then a non-LLM fallback | Best judgment on taste. About 2 calls per job, costing cents. Switch models with `CLAUDE_MODEL`. | Local LLM (setup time, quality) |
| D11 | Human review gate by default | One bad or offensive clip costs more than the minutes saved. | Full auto (available as `AUTO_APPROVE`) |
| D12 | Original game audio by default. Trending sounds added in the TikTok app. | The APIs can't attach in-app sounds, and copyrighted audio gets muted or struck. | Burning songs into the file |
| D13 | ~~TikTok inbox/draft upload~~, replaced by D18 | — | — |
| D14 | ~~Instagram resumable local upload~~, replaced by D18 | — | — |
| D15 | Streamlit dashboard | Vibe-codes fast and has a built-in video player. It's only a viewer and editor over the files and the DB. | React/Tauri (too slow to build), CLI-only review |
| D16 | `DRY_RUN=true` by default, with a DryRun client | Vibe-coding must never post to the real accounts by accident. | — |
| D17 | Folder ownership per dev, and the shared contract changes only by agreement | Prevents merge conflicts and silent breakage between two AI-assisted devs | — |
| D18 | **Phase 1 posting is manual-assisted** (`POST_MODE=manual`): a post kit, no platform APIs | No app reviews or OAuth, it works for any account today, and it allows in-app trending sounds. About 1 min per post. | Own TikTok/Meta apps (days of review, TikTok private-only until audit), Upload-Post API ($24/mo, kept as auto mode later), browser automation (against platform terms) |
| D19 | Trending sounds come from public weekly roundups, extracted by Claude | No official API exists. Roundups are plain HTML and cover both platforms. Claude copes with changing page layouts. | TikTok Creative Center scraping (signed session), paid scrapers (Apify), a hand-kept list only (too small) |

## 8. Module layout

```
clipper/
  models.py        # SHARED contract: Job, Clip, Transcript, Post (+ atomic manifest save)
  config.py        # SHARED settings from .env
  llm.py           # SHARED Claude call -> pydantic model (Dev B writes on day 1)
  cli.py           # SHARED typer entry: run, stage, ui, scheduler, auth
  ingest/          # Dev A
  analyze/         # Dev A: transcribe.py, signals.py, candidates.py, rank.py
  render/          # Dev A: layout.py, subtitles.py (ASS), encode.py
  captions/        # Dev B: generate.py, sounds.py
  publish/         # Dev B: queue.py, slots.py, scheduler.py, clients.py, dryrun.py, text.py, (uploadpost.py later)
  sounds/          # Dev B: fetch.py (roundup pages -> Claude -> trending_sounds.json)
  ui/              # Dev B: Streamlit app.py
tests/             # pure-function tests + fixture manifests, no network
```

## 9. Performance budget (20-min 1080p source, laptop CPU)

| Step | Target |
|---|---|
| Download | ≤ 2 min |
| Transcribe | ≤ 7 min (switch to `base` if it's slower) |
| Signals + candidates | ≤ 2 min |
| LLM ranking + captions | ≤ 1 min |
| Render 5 clips | ≤ 3 min |
| **Total** | **≤ 15 min** |

## 10. Timeline (the task-level breakdown is in [TASKS.md](TASKS.md))

| Dates | Dev A (Engine) | Dev B (Distribution) |
|---|---|---|
| **Nov 4–5** | Submit API apps (both devs) · ingest + audio extract | `llm.py` · captions on the fixture |
| **Nov 6–8** | transcribe · signals · candidates | TikTok inbox client · IG Reels client · DryRun client · auth commands |
| **Nov 9–11** | Claude ranker · blur render · ASS subtitles + hook | queue + slots + scheduler · Streamlit dashboard v1 |
| **Nov 12** | **M1: end to end on a real video, posting to private or test accounts** | |
| Nov 13–16 | Quality tuning, `--clip` re-render, speed | Review edits → re-render, copy buttons, error views. **Live rehearsal: post real pre-launch content.** |
| **Nov 17** | **Code freeze.** Bug fixes only after this. | |
| **Nov 19** | **Launch: run the tool** (see section 11) | |
| After launch | Stretch list from section 2 | |

## 11. Launch-day runbook (Nov 19)
1. Start `clipper ui`, then refresh the trending sounds (dashboard button or `clipper sounds refresh`).
2. Feed in sources as they appear. Review, then approve the best 2–3 clips per video.
3. When the Queue page shows a post as **due now**: Start posting → drag the file into the upload page → add the suggested sound → paste → post → Mark as posted.

## 12. Risks

| Risk | Mitigation |
|---|---|
| Roundup pages change or go stale | Several sources, Claude extraction instead of fixed selectors, the last good list is kept, and pinned songs come from `trending_sounds.yaml` |
| Manual posting is slow on launch day | One-click post kit, about 1 min per post, and a daily cap keeps the volume sane |
| YouTube blocks downloads | Cookies from the browser, `uv lock --upgrade-package yt-dlp` |
| Whisper too slow | `base` model, CUDA if available, YouTube subtitles as a fallback |
| Accounts flagged for reposting | Own or permitted sources, real changes to each clip, credit lines, a daily cap |
| Double posts | UNIQUE constraint, a claim before upload, and `needs_check` instead of blind retries |
| Laptop sleeps and the schedule stops | Disable sleep. Moving to a VPS is on the stretch list. |
| The two devs break each other's code | The contract-change rule (D17), fixture tests in CI on every PR |

## 13. Phase 2: SaaS (after launch, not designed yet)
Goal: other creators use Clipper for their own accounts. Notes for when we get there:
- **Hosting:** a web app plus a job queue and workers, with per-user storage for sources and clips. Processing costs (Claude, transcription, rendering) are per user.
- **Auth:** user accounts. Every job, clip and post is scoped to a user.
- **Posting:** keep the manual post kit for free users, and add auto-posting through a posting API (Upload-Post "profiles" map 1:1 to our users), or our own audited TikTok/Meta apps.
- **Phone first:** a share-sheet button (Web Share API) once the app is on HTTPS.
- **Content rights:** terms of use, and users confirm they have the right to clip each source.
