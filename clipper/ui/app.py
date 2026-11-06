"""Clipper dashboard. Start with `uv run clipper ui`.

Rendering only: every action lives in clipper/ui/actions.py. Theme: .streamlit/config.toml.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

import streamlit as st

from clipper.models import JobStatus, Platform, PostStatus, ReviewStatus
from clipper.publish.text import post_text
from clipper.sounds.store import search_url
from clipper.ui import actions

st.set_page_config(page_title="Clipper", page_icon="🎬", layout="wide")

PAGES = {"Home": "🏠", "New video": "✨", "Jobs": "🎞️", "Queue": "📤", "Sounds": "🎵"}
PLATFORM_ICON = {Platform.tiktok: "🎵", Platform.instagram: "📸"}
VIBE_ICON = {"hype": "🔥", "funny": "😂", "chill": "🌊", "dramatic": "🎭"}
REVIEW_BADGE = {
    ReviewStatus.pending: ":orange-badge[⏳ To review]",
    ReviewStatus.approved: ":green-badge[✅ Approved]",
    ReviewStatus.rejected: ":gray-badge[🚫 Rejected]",
}
JOB_BADGE = {
    "starting": ":blue-badge[Starting]",
    "created": ":blue-badge[Queued]",
    "ingested": ":blue-badge[Downloaded]",
    "analyzed": ":violet-badge[Clips picked]",
    "captioned": ":violet-badge[Captions written]",
    "rendered": ":green-badge[Ready]",
    "failed": ":red-badge[Failed]",
}
PIPELINE_STEPS = [
    ("created", "Starting"),
    ("ingested", "Downloading the video"),
    ("analyzed", "Transcribing and finding the best moments"),
    ("captioned", "Writing hooks, captions and picking sounds"),
    ("rendered", "Rendering vertical clips"),
]
SOUNDS_STALE_AFTER = timedelta(days=3)

CSS = """
<style>
.block-container {padding-top: 1.2rem; max-width: 1400px;}
h1.clipper-title {
  display: inline-block; font-size: 2.1rem; font-weight: 800; margin: 0 0 .2rem 0;
  background: linear-gradient(90deg, #25F4EE 0%, #FE2C55 60%, #FF8A3D 100%);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
}
.clipper-sub {color: #9A9AA8; margin-bottom: .6rem;}
[data-testid="stVideo"] video {border-radius: 14px;}
[data-testid="stVerticalBlockBorderWrapper"]:has(> div > [data-testid="stVerticalBlock"]) {
  transition: border-color .15s ease;
}
[data-testid="stVerticalBlockBorderWrapper"]:hover {border-color: #FE2C5566;}
.hook {font-size: 1.05rem; font-weight: 700; line-height: 1.3; margin: .2rem 0 .4rem;}
.step {color: #9A9AA8; font-size: .9rem;}
.step b {color: #F2F2F5;}
</style>
"""


# ---------------------------------------------------------------- shared bits


def _do(fn, *args, ok: str, rerun: bool = True, celebrate: bool = False) -> None:
    try:
        fn(*args)
    except actions.ActionError as e:
        st.toast(f"❌ {e}")
        return
    st.toast(f"✅ {ok}")
    if celebrate:
        st.session_state["celebrate"] = True  # shown after the rerun
    if rerun:
        st.rerun()


def _short(text: str, n: int = 110) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def go(page: str, job_id: str | None = None) -> None:
    """Button callback: switch page (runs before the nav widget is drawn)."""
    st.session_state["page"] = page
    if job_id:
        st.session_state["selected_job"] = job_id


def header() -> None:
    st.markdown(CSS, unsafe_allow_html=True)
    left, right = st.columns([3, 2], vertical_alignment="center")
    with left:
        st.markdown('<h1 class="clipper-title">🎬 Clipper</h1>', unsafe_allow_html=True)
        st.markdown(
            '<div class="clipper-sub">YouTube link in · scroll-stopping clips out</div>',
            unsafe_allow_html=True,
        )
    with right:
        sounds, fetched_at = actions.sounds_overview()
        if fetched_at is None:
            st.warning("No trending sounds yet: open **Sounds** and click Refresh.", icon="🎵")
        elif datetime.now() - fetched_at > SOUNDS_STALE_AFTER:
            st.warning(
                f"Trending sounds are from {fetched_at:%b %d}. Refresh them on **Sounds**.",
                icon="🎵",
            )
        else:
            st.caption(f"🎵 {len(sounds)} trending sounds · updated {fetched_at:%b %d %H:%M}")


def sidebar() -> None:
    with st.sidebar:
        st.markdown("### 🎬 Clipper")
        if due := len(actions.due_now()):
            st.success(f"🔔 {due} post(s) due now on the **Queue** page")
            st.button("Go post them", on_click=go, args=("Queue",), key="side-queue")
        today, week = actions.claude_spend()
        # "\$" so Streamlit doesn't read $...$ as a math formula
        st.caption(f"💸 Claude API: ~\\${today:.2f} today · ~\\${week:.2f} last 7 days")
        st.caption("Runs only on this computer.")


# ---------------------------------------------------------------- home


def page_home() -> None:
    o = actions.overview()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("🎞️ Clips to review", o.to_review, border=True)
    c2.metric("🔔 Due to post", o.due, border=True)
    c3.metric("✅ Posted (7 days)", o.posted_week, border=True)
    c4.metric("💸 Claude (7 days)", f"${o.spend_week:.2f}", border=True)

    st.markdown("#### What's next")
    todo = []
    if o.due:
        todo.append(
            ("🔔", f"**{o.due} post(s) are due.** Post them while the slot is fresh.", "Queue")
        )
    if o.to_review:
        todo.append(("👀", f"**{o.to_review} clip(s) waiting for review.**", "Jobs"))
    if o.running:
        todo.append(("⏳", f"**{o.running} video(s) processing.** Grab a coffee.", "Jobs"))
    if o.sounds_stale:
        todo.append(("🎵", "**Trending sounds are out of date.**", "Sounds"))
    if not todo:
        todo.append(("✨", "**All caught up.** Drop a new video in.", "New video"))
    for i, (icon, text, page) in enumerate(todo):
        with st.container(border=True):
            a, b = st.columns([5, 1], vertical_alignment="center")
            a.markdown(f"{icon}  {text}")
            b.button(f"Open {page}", key=f"todo-{i}", on_click=go, args=(page,), width="stretch")

    st.markdown("#### Recent videos")
    rows = actions.list_jobs()[:6]
    if not rows:
        st.caption("Nothing yet.")
    cols = st.columns(3)
    for i, row in enumerate(rows):
        with cols[i % 3], st.container(border=True):
            title = (row.job.title if row.job else None) or "Starting..."
            st.markdown(f"**{title[:60]}**")
            clips = len(row.job.clips) if row.job else 0
            st.markdown(
                f"{JOB_BADGE.get(row.status, row.status)} · {clips} clips · "
                f"{row.started_at:%b %d %H:%M}"
            )
            st.button("Open", key=f"open-{row.id}", on_click=go, args=("Jobs", row.id))


# ---------------------------------------------------------------- new video


def page_new() -> None:
    st.markdown("#### ✨ Clip a new video")
    with st.container(border=True):
        with st.form("new_job", clear_on_submit=True, border=False):
            url = st.text_input("YouTube link", placeholder="https://www.youtube.com/watch?v=...")
            context = st.text_area(
                "What is this video about? (optional, but it makes captions much better)",
                placeholder="GTA 6 launch day, funny police chase fails",
                height=90,
            )
            submitted = st.form_submit_button(
                "🚀 Make clips", type="primary", use_container_width=True
            )
    if submitted:
        try:
            job_id = actions.submit_job(url, context)
        except actions.ActionError as e:
            st.error(str(e))
            return
        st.session_state.selected_job = job_id
        st.success(f"Started job `{job_id}`. It takes a few minutes.")
        st.button("Watch it work →", on_click=go, args=("Jobs", job_id), type="primary")
    st.markdown(
        '<div class="step">What happens: <b>download</b> → <b>transcribe</b> → <b>find '
        "highlights</b> → <b>Claude writes hooks & picks sounds</b> → <b>render 9:16 clips</b>"
        "</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        "Only clip videos you're allowed to repost (your own gameplay, or creators who said "
        "yes). The source is credited on every post."
    )


# ---------------------------------------------------------------- jobs


def page_jobs() -> None:
    rows = actions.list_jobs()
    if not rows:
        st.info("No jobs yet.")
        st.button("✨ Clip your first video", on_click=go, args=("New video",), type="primary")
        return
    ids = [r.id for r in rows]
    labels = {r.id: r.label for r in rows}
    default = st.session_state.get("selected_job")
    index = ids.index(default) if default in ids else 0
    job_id = st.selectbox("Video", ids, index=index, format_func=lambda i: labels[i])
    st.session_state.selected_job = job_id
    row = next(r for r in rows if r.id == job_id)

    if not actions.is_finished(row):
        job_progress(job_id)
        return
    job = row.job
    if job.status == JobStatus.failed:
        st.error(f"This job failed: {job.error}")
        st.caption(
            "Fix the cause (see the log), then retry. Finished steps are skipped, so nothing "
            "is downloaded or transcribed twice."
        )
        if st.button("🔁 Retry job", type="primary", key=f"resume-{job_id}"):
            _do(actions.resume_job, job_id, ok="Retrying")
        st.code(actions.log_tail(job_id, 40) or "(no log)", language=None)
        return

    head, filt = st.columns([3, 2], vertical_alignment="bottom")
    with head:
        st.markdown(f"#### {job.title or job.source_url}")
        st.caption(
            f"{job.channel or ''} · {len(job.clips)} clips"
            + (f" · your note: {_short(job.context)}" if job.context else "")
        )
    with filt:
        show = st.segmented_control(
            "Show",
            ["All", "To review", "Approved"],
            default="All",
            key=f"filter-{job_id}",
            label_visibility="collapsed",
        )
    if not job.clips:
        st.warning("No clips were found in this video.")
        return
    wanted = {
        "To review": {ReviewStatus.pending},
        "Approved": {ReviewStatus.approved},
    }.get(show or "All", set(ReviewStatus))
    clips = [c for c in job.clips if c.review in wanted]
    if not clips:
        st.caption("Nothing here.")
    cols = st.columns(3)
    for i, clip in enumerate(clips):
        with cols[i % 3]:
            clip_card(job.id, clip.id)


@st.fragment(run_every="5s")
def job_progress(job_id: str) -> None:
    row = next((r for r in actions.list_jobs() if r.id == job_id), None)
    if row is not None and actions.is_finished(row):
        st.rerun()  # done: redraw the whole page with the clips
    status = row.status if row else "starting"
    order = [s for s, _ in PIPELINE_STEPS]
    reached = order.index(status) if status in order else -1
    with st.status("Making clips...", state="running", expanded=True):
        for i, (_, label) in enumerate(PIPELINE_STEPS):
            if i <= reached:
                st.markdown(f"✅ {label}")
            elif i == reached + 1:
                st.markdown(f"⏳ **{label}...**")
            else:
                st.markdown(f"<span class='step'>○ {label}</span>", unsafe_allow_html=True)
    with st.expander("Live log"):
        st.code(actions.log_tail(job_id) or "(waiting for output)", language=None)


def clip_card(job_id: str, clip_id: str) -> None:
    job, clip = actions.load_clip(job_id, clip_id)
    key = f"{job_id}-{clip_id}"
    clip_posts = actions.posts_for_clip(job_id, clip_id)
    locked = any(p.status == PostStatus.published for p in clip_posts)
    with st.container(border=True):
        st.markdown(
            f"{REVIEW_BADGE[clip.review]} :gray-badge[⏱ {clip.duration:.0f}s] "
            f":gray-badge[⭐ {clip.score:.2f}]"
            + (f" :blue-badge[{VIBE_ICON.get(clip.vibe, '')} {clip.vibe}]" if clip.vibe else "")
        )
        if clip.file and clip.file.exists():
            st.video(str(clip.file))
        else:
            st.warning("Rendered file is missing.")
        st.markdown(f'<div class="hook">“{clip.hook_text or "—"}”</div>', unsafe_allow_html=True)
        st.caption(f"Why: {clip.reason}")

        caption = st.text_area("Caption", clip.caption or "", key=f"cap-{key}", height=90)
        tags = st.text_input("Hashtags", " ".join(clip.hashtags), key=f"tags-{key}")
        if clip.suggested_sound:
            st.markdown(
                f"🎵 **{clip.suggested_sound}** · [find it]({search_url(clip.suggested_sound)})"
            )

        changed = caption != (clip.caption or "") or tags != " ".join(clip.hashtags)
        b1, b2 = st.columns(2)
        if b1.button(
            "✅ Approve",
            key=f"ok-{key}",
            type="primary",
            disabled=clip.review == ReviewStatus.approved,
            width="stretch",
        ):
            if changed:
                actions.save_copy(job_id, clip_id, caption, tags)
            _do(actions.approve, job_id, clip_id, ok="Approved and scheduled")
        if b2.button(
            "🚫 Reject",
            key=f"no-{key}",
            disabled=clip.review == ReviewStatus.rejected,
            width="stretch",
        ):
            _do(actions.reject, job_id, clip_id, ok="Rejected")
        b3, b4 = st.columns(2)
        if b3.button("💾 Save text", key=f"save-{key}", disabled=not changed, width="stretch"):
            _do(actions.save_copy, job_id, clip_id, caption, tags, ok="Saved")
        with b4.popover("✂️ Edit clip", use_container_width=True):
            edit_form(job, clip, key, locked)

        for post in clip_posts:
            if post.status == PostStatus.published:
                when = f"posted {post.updated_at:%b %d %H:%M}"
            else:
                when = f"post at {post.scheduled_at:%a %H:%M}"
            st.caption(
                f"{PLATFORM_ICON[post.platform]} {actions.PLATFORM_LABEL[post.platform]}: {when}"
            )


def edit_form(job, clip, key: str, locked: bool) -> None:
    if locked:
        st.caption("Already posted, so it can't be changed.")
    hook = st.text_input("Hook on screen (first 3s)", clip.hook_text or "", key=f"hook-{key}")
    max_t = float(job.duration or clip.end + 60)
    start, end = st.columns(2)
    start = start.number_input("Start (s)", 0.0, max_t, float(clip.start), 0.5, key=f"start-{key}")
    end = end.number_input("End (s)", 0.0, max_t, float(clip.end), 0.5, key=f"end-{key}")
    st.caption(f"New length: {end - start:.1f}s")
    edited = (
        hook.strip() != (clip.hook_text or "")
        or abs(start - clip.start) > 0.01
        or abs(end - clip.end) > 0.01
    )
    if st.button(
        "🎬 Re-render",
        key=f"rerender-{key}",
        disabled=locked or not edited,
        type="primary",
        width="stretch",
    ):
        with st.spinner("Re-rendering... (10-60s)"):
            _do(actions.edit_clip, job.id, clip.id, hook, start, end, ok="Re-rendered")


# ---------------------------------------------------------------- queue (post kit)


def page_queue() -> None:
    queue_view()


@st.fragment(run_every="60s")  # posts move into "Due now" without a manual refresh
def queue_view() -> None:
    now = datetime.now()
    due = actions.due_now(now)
    upcoming = actions.up_next(now)
    done = actions.posts(PostStatus.published)
    if not (due or upcoming or done):
        st.info("Nothing queued yet. Approve clips on the **Jobs** page.")
        st.button("Review clips", on_click=go, args=("Jobs",), type="primary")
        return

    tab_due, tab_next, tab_done = st.tabs(
        [f"🔔 Due now ({len(due)})", f"🕒 Up next ({len(upcoming)})", f"✅ Posted ({len(done)})"]
    )
    with tab_due:
        if due:
            st.markdown(
                '<div class="step"><b>1</b> Start posting → <b>2</b> drag the file into the '
                "upload page → <b>3</b> add the sound → <b>4</b> paste the caption → "
                "<b>5</b> post → <b>6</b> Mark as posted</div>",
                unsafe_allow_html=True,
            )
        else:
            st.caption("Nothing to post right now. 😌 The next ones are in **Up next**.")
        for p in due:
            post_kit(p)

    with tab_next:
        by_day = defaultdict(list)
        for p in upcoming:
            by_day[p.scheduled_at.date()].append(p)
        if not upcoming:
            st.caption("Nothing scheduled.")
        for day, posts in by_day.items():
            label = "Today" if day == now.date() else f"{day:%A %b %d}"
            st.markdown(f"##### {label}")
            for p in posts:
                _, clip = actions.load_clip(p.job_id, p.clip_id)
                with st.container(border=True):
                    c1, c2, c3 = st.columns([5, 1, 1], vertical_alignment="center")
                    c1.markdown(
                        f"**{p.scheduled_at:%H:%M}** · {PLATFORM_ICON[p.platform]} "
                        f"{actions.PLATFORM_LABEL[p.platform]} · {clip.title or p.clip_id}"
                    )
                    if c2.button("Post now", key=f"early-{p.id}", width="stretch"):
                        st.session_state[f"open-{p.id}"] = True
                    if c3.button("Cancel", key=f"cancel-{p.id}", width="stretch"):
                        actions.cancel_post(p.id)
                        st.rerun(scope="fragment")
                if st.session_state.get(f"open-{p.id}"):
                    post_kit(p)

    with tab_done:
        if not done:
            st.caption("Nothing posted yet.")
        for p in sorted(done, key=lambda p: p.updated_at, reverse=True):
            _, clip = actions.load_clip(p.job_id, p.clip_id)
            c1, c2 = st.columns([6, 1], vertical_alignment="center")
            link = f" · [open ↗]({p.url})" if p.url else ""
            c1.markdown(
                f"{PLATFORM_ICON[p.platform]} **{clip.title or p.clip_id}** · "
                f"{actions.PLATFORM_LABEL[p.platform]} · {p.updated_at:%b %d %H:%M}{link}"
            )
            if c2.button("Undo", key=f"undo-{p.id}", help="Clicked Mark as posted by mistake?"):
                _do(actions.undo_posted, p.id, ok="Moved back to the queue")


def post_kit(p) -> None:
    job, clip = actions.load_clip(p.job_id, p.clip_id)
    platform = actions.PLATFORM_LABEL[p.platform]
    with st.container(border=True):
        video, info = st.columns([1, 2])
        with video:
            if clip.file and clip.file.exists():
                st.video(str(clip.file))
        with info:
            st.markdown(
                f"#### {PLATFORM_ICON[p.platform]} {platform} · {clip.title or clip.id}\n"
                f":gray-badge[due {p.scheduled_at:%b %d %H:%M}]"
            )
            if st.button(
                f"🚀 Start posting to {platform}",
                key=f"start-{p.id}",
                type="primary",
                width="stretch",
            ):
                _do(actions.start_posting, p.id, ok="Caption copied · file shown · page opened")
            st.code(post_text(job, clip, p.platform), language=None, wrap_lines=True)
            if clip.suggested_sound:
                st.markdown(
                    f"🎵 Add this sound in the editor: **{clip.suggested_sound}** · "
                    f"[find it on TikTok]({search_url(clip.suggested_sound)})"
                )
            b1, b2, b3 = st.columns(3)
            if b1.button("📋 Copy text", key=f"copy-{p.id}", width="stretch"):
                _do(actions.copy_text, p.id, ok="Copied", rerun=False)
            if b2.button("📂 Show file", key=f"file-{p.id}", width="stretch"):
                _do(actions.reveal_file, p.id, ok="File shown", rerun=False)
            if b3.button(f"↗ Open {platform}", key=f"page-{p.id}", width="stretch"):
                _do(actions.open_upload_page, p.id, ok="Upload page opened", rerun=False)

            url_col, done_col = st.columns([3, 2], vertical_alignment="bottom")
            url = url_col.text_input(
                "Link to the post (optional)", key=f"url-{p.id}", placeholder="https://..."
            )
            if done_col.button("✅ Mark as posted", key=f"posted-{p.id}", width="stretch"):
                _do(actions.mark_posted, p.id, url, ok="Posted! 🎉", celebrate=True)


# ---------------------------------------------------------------- sounds


def page_sounds() -> None:
    sounds, fetched_at = actions.sounds_overview()
    top, btn = st.columns([4, 1], vertical_alignment="center")
    with top:
        st.subheader(f"🎵 Trending sounds ({len(sounds)})")
        st.caption(
            "Claude picks one of these for each clip. Add it inside the TikTok or Instagram "
            "editor when you post: we never put music in the file. "
            + (f"Last refresh: {fetched_at:%b %d %H:%M}." if fetched_at else "Never refreshed.")
        )
    with btn:
        if st.button("🔄 Refresh now", type="primary", key="refresh-sounds", width="stretch"):
            with st.spinner("Reading this week's trending lists... (about 15s)"):
                _do(actions.refresh_sounds, ok="Trending sounds updated")
    if not sounds:
        st.info("No sounds yet. Click **Refresh now**.")
        return

    f1, f2, f3 = st.columns([3, 4, 3])
    platforms = f1.pills(
        "Platform", ["tiktok", "instagram", "both"], selection_mode="multi", key="snd-platform"
    )
    vibes = f2.pills(
        "Vibe",
        list(VIBE_ICON),
        selection_mode="multi",
        format_func=lambda v: f"{VIBE_ICON[v]} {v}",
        key="snd-vibe",
    )
    query = f3.text_input("Search", placeholder="song or artist", key="snd-q").strip().lower()
    shown = [
        s
        for s in sounds
        if (not platforms or s.platform in platforms)
        and (not vibes or s.vibe in vibes)
        and (not query or query in s.label.lower())
    ]
    st.caption(f"Showing {len(shown)} of {len(sounds)}")
    st.dataframe(
        [
            {
                "Sound": s.title,
                "Artist": s.artist or "",
                "Platform": s.platform,
                "Vibe": f"{VIBE_ICON.get(s.vibe, '')} {s.vibe}",
                "Pinned": "📌" if s.source == "pinned" else "",
                "Find": search_url(s.label),
            }
            for s in shown
        ],
        column_config={"Find": st.column_config.LinkColumn("Find", display_text="Search ↗")},
        hide_index=True,
        use_container_width=True,
        height=min(38 * (len(shown) + 1) + 2, 640),
    )
    st.caption("📌 Pin your own picks in `trending_sounds.yaml`; they're always offered first.")


# ---------------------------------------------------------------- main

RENDER = {
    "Home": page_home,
    "New video": page_new,
    "Jobs": page_jobs,
    "Queue": page_queue,
    "Sounds": page_sounds,
}

if "page" not in st.session_state:  # first load: allow bookmarks like ?page=Queue
    wanted = st.query_params.get("page", "Home")
    st.session_state["page"] = wanted if wanted in PAGES else "Home"
header()
sidebar()
page = st.segmented_control(
    "Page",
    list(PAGES),
    format_func=lambda p: f"{PAGES[p]} {p}",
    key="page",
    label_visibility="collapsed",
)
if st.session_state.pop("celebrate", False):
    st.balloons()
RENDER[page or "Home"]()
