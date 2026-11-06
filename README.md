# 🎬 Clipper

Paste a YouTube link and get 5 vertical clips (9:16) for TikTok and Instagram Reels. Each clip comes with a hook on screen, subtitles, a caption, hashtags and a suggested trending sound. You review the clips in a dashboard and post them in about a minute each.

Everything runs on your own computer. The only cost is the Claude API, about **$0.07 per video**.

---

## 1. Install (once)

**Windows:** open a terminal and run:

```
winget install Git.Git astral-sh.uv Gyan.FFmpeg
```

**Mac:** run `brew install git uv ffmpeg`.

Close the terminal and open a new one, then:

```
git clone https://github.com/Realche3/clipper.git
cd clipper
uv sync
```

`uv sync` installs Python and everything else the tool needs.

## 2. Add your Claude API key (once)

1. Get an API key at [console.anthropic.com](https://console.anthropic.com). A Claude.ai subscription is a separate product and won't work here.
2. Copy `.env.example` to a new file called `.env` in the same folder.
3. Open `.env` and paste your key after `ANTHROPIC_API_KEY=`.

`.env` is never uploaded to GitHub. Don't share your key.

## 3. Start the dashboard

```
uv run clipper ui
```

It opens in your browser. The dashboard only runs on your computer and nobody else can reach it. Leave the terminal open while you use it.

---

## How to use it

### First time: get trending sounds
Go to **🎵 Sounds** → **Refresh now** (about 15 seconds). This loads around 90 trending TikTok and Reels songs. Claude picks one for each clip. Refresh every couple of days, and the dashboard warns you when the list gets old.

### Make clips
1. **✨ New video** → paste a YouTube link.
2. Optionally write a short note about the video, for example *"GTA 6 launch day, police chase fails"*. It makes the captions much better.
3. Click **🚀 Make clips**. A 10-minute video takes about 5 minutes. The first run also downloads the speech model, so it takes a bit longer.

### Review
On **🎞️ Jobs**, each clip shows the video, its hook, caption, hashtags and suggested sound.
- **✅ Approve** to schedule the clip for TikTok and Instagram, or **🚫 Reject** it.
- You can edit the caption and hashtags right in the card.
- **📱** previews how the post will look. **🎵** swaps the sound. **📂** shows the file.
- **✂️ Edit clip** changes the hook text or the start and end times. The clip re-renders in under a minute.
- **⚡ Bulk** approves or rejects every clip of a video at once.

### Post
Approved clips get a suggested posting time, spread out through the day. When a post's time comes, it shows on **📤 Queue → Due now**:

1. Click **🚀 Start posting**. That copies the caption, shows the clip file and opens the TikTok or Instagram upload page.
2. Drag the file into the upload page.
3. Add the suggested sound in the TikTok or Instagram editor.
4. Paste the caption with Ctrl+V and post.
5. Back in the dashboard, click **✅ Mark as posted**. You can paste the post link if you like. 🎈

Want to post something early? Use **Up next → Post now**. Marked one by mistake? Use **Posted → Undo**.

### Rules
- **Only clip videos you're allowed to repost**, like your own gameplay or creators who said yes. Reposting other people's videos can get your account flagged. The source is credited on every post.
- **Never put music in the video yourself.** Always add the sound inside the app, or the post gets muted.

---

## Settings (`.env`)

You can leave everything at its default. These are the ones worth knowing:

| Setting | What it does | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Your Claude key (required) | — |
| `BASE_HASHTAGS` | Tags added to **every** post, e.g. `gta6,gaming`. Leave empty if you post different games. | empty |
| `CLIPS_PER_VIDEO` | How many clips per video | `5` |
| `CLIP_MIN_SECONDS` / `CLIP_MAX_SECONDS` | Clip length | `15` / `60` |
| `POSTING_WINDOW` | Hours when posts are scheduled | `10:00-23:30` |
| `POST_INTERVAL_MINUTES` | Minimum gap between two posts on one platform | `90` |
| `DAILY_CAP_TIKTOK` / `DAILY_CAP_INSTAGRAM` | Max posts per day | `6` / `5` |
| `WHISPER_MODEL` | Speech model. Use `base` if processing is too slow. | `small` |
| `CLAUDE_MODEL_RANK` / `_CAPTIONS` / `_SOUNDS` | Which Claude model does each job | Opus / Opus / Haiku |

Restart `uv run clipper ui` after changing `.env`.

## Useful commands

| Command | What it does |
|---|---|
| `uv run clipper ui` | Open the dashboard |
| `uv run clipper run <youtube-link> --context "note"` | Make clips without the dashboard |
| `uv run clipper resume <job-id>` | Retry a failed video (finished steps are skipped) |
| `uv run clipper sounds refresh` | Update the trending sounds |
| `uv run clipper usage` | See what Claude has cost |

## Troubleshooting

| Problem | Fix |
|---|---|
| "Claude auth failed" | Check `ANTHROPIC_API_KEY` in `.env`, then restart the dashboard. |
| YouTube says "confirm you're not a bot" | Add `YTDLP_COOKIES_FROM_BROWSER=chrome` to `.env` (use your browser's name). |
| A video failed | Open it on **Jobs**, read the log, and click **🔁 Retry job**. |
| Processing is slow | Set `WHISPER_MODEL=base` in `.env`. |
| `uv sync` says a file is in use | Close the dashboard terminal first, then run it again. |
| No sound suggestions | **🎵 Sounds → Refresh now**. |

---

## For developers

- Read [CLAUDE.md](CLAUDE.md) first. Claude Code loads it automatically.
- The design and decisions are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), and the tasks are in [docs/TASKS.md](docs/TASKS.md).
- Work on a branch (`a/...` or `b/...`), run `uv run ruff check . && uv run pytest`, then open a PR. A PR that touches shared files needs the other dev's 👍.
