"""Clipper dashboard. Start with `uv run clipper ui`.

Rendering only: every action lives in clipper/ui/actions.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import streamlit as st

from clipper.models import JobStatus, Platform, PostStatus, ReviewStatus
from clipper.publish.text import post_text
from clipper.sounds.store import search_url
from clipper.ui import actions

st.set_page_config(page_title="Clipper", page_icon="🎬", layout="wide")

STATUS_ICON = {PostStatus.scheduled: "🕒", PostStatus.published: "✅"}
PLATFORM_ICON = {Platform.tiktok: "🎵", Platform.instagram: "📸"}
REVIEW_ICON = {ReviewStatus.pending: "⏳", ReviewStatus.approved: "✅", ReviewStatus.rejected: "🚫"}


# ---------------------------------------------------------------- header


SOUNDS_STALE_AFTER = timedelta(days=3)


def header() -> None:
    left, right = st.columns([3, 2])
    left.title("🎬 Clipper")
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


# ---------------------------------------------------------------- new video


def page_new() -> None:
    st.subheader("Clip a new video")
    with st.form("new_job", clear_on_submit=True):
        url = st.text_input("YouTube link", placeholder="https://www.youtube.com/watch?v=...")
        context = st.text_area(
            "What is this video about? (optional)",
            placeholder="GTA 6 launch day, funny police chase fails",
            help="Claude uses this to write better hooks, captions and hashtags.",
        )
        submitted = st.form_submit_button("Make clips", type="primary")
    if submitted:
        try:
            job_id = actions.submit_job(url, context)
        except actions.ActionError as e:
            st.error(str(e))
            return
        st.session_state.selected_job = job_id
        st.success(f"Started job `{job_id}`. Follow it on the **Jobs** page.")
    st.caption(
        "Only clip videos you're allowed to repost (your own gameplay, or creators who "
        "said yes). The source is credited on every post."
    )


# ---------------------------------------------------------------- jobs


def page_jobs() -> None:
    rows = actions.list_jobs()
    if not rows:
        st.info("No jobs yet. Start one on the **New video** page.")
        return
    ids = [r.id for r in rows]
    labels = {r.id: r.label for r in rows}
    default = st.session_state.get("selected_job")
    index = ids.index(default) if default in ids else 0
    job_id = st.selectbox("Job", ids, index=index, format_func=lambda i: labels[i])
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
        if st.button("Retry job", type="primary", key=f"resume-{job_id}"):
            _do(actions.resume_job, job_id, ok="Retrying")
        st.code(actions.log_tail(job_id, 40) or "(no log)", language=None)
        return

    st.markdown(f"**{job.title or job.source_url}**  ·  {job.channel or ''}")
    if job.context:
        st.caption(f"Your note: {job.context}")
    if not job.clips:
        st.warning("No clips were found in this video.")
    for clip in job.clips:
        clip_card(job.id, clip.id)


@st.fragment(run_every="5s")
def job_progress(job_id: str) -> None:
    row = next((r for r in actions.list_jobs() if r.id == job_id), None)
    if row is not None and actions.is_finished(row):
        st.rerun()  # done: redraw the whole page with the clips
    status = row.status if row else "starting"
    steps = ["starting", "created", "ingested", "analyzed", "captioned", "rendered"]
    done = steps.index(status) if status in steps else 0
    st.progress(done / (len(steps) - 1), text=f"Working... ({status})")
    st.code(actions.log_tail(job_id) or "(waiting for output)", language=None)


def clip_card(job_id: str, clip_id: str) -> None:
    job, clip = actions.load_clip(job_id, clip_id)
    key = f"{job_id}-{clip_id}"
    with st.container(border=True):
        video, form = st.columns([1, 2])
        with video:
            if clip.file and clip.file.exists():
                st.video(str(clip.file))
            else:
                st.warning("Rendered file is missing.")
        with form:
            st.markdown(
                f"**{clip.id}** · {REVIEW_ICON[clip.review]} {clip.review.value} · "
                f"{clip.duration:.0f}s · score {clip.score:.2f}"
            )
            st.caption(f"Hook on screen: **{clip.hook_text or '-'}**  ·  Why: {clip.reason}")
            caption = st.text_area("Caption", clip.caption or "", key=f"cap-{key}", height=80)
            tags = st.text_input("Hashtags", " ".join(clip.hashtags), key=f"tags-{key}")
            if clip.suggested_sound:
                st.caption(
                    f"🎵 Suggested sound: **{clip.suggested_sound}** · "
                    f"[find it on TikTok]({search_url(clip.suggested_sound)})"
                )

            b1, b2, b3 = st.columns(3)
            changed = caption != (clip.caption or "") or tags != " ".join(clip.hashtags)
            if b1.button("Save text", key=f"save-{key}", disabled=not changed):
                _do(actions.save_copy, job_id, clip_id, caption, tags, ok="Saved")
            if b2.button(
                "Approve & schedule",
                key=f"ok-{key}",
                type="primary",
                disabled=clip.review == ReviewStatus.approved,
            ):
                if changed:
                    actions.save_copy(job_id, clip_id, caption, tags)
                _do(actions.approve, job_id, clip_id, ok="Scheduled")
            if b3.button("Reject", key=f"no-{key}", disabled=clip.review == ReviewStatus.rejected):
                _do(actions.reject, job_id, clip_id, ok="Rejected")

            clip_posts = actions.posts_for_clip(job_id, clip_id)
            for post in clip_posts:
                if post.status == PostStatus.published:
                    when = f"posted {post.updated_at:%b %d %H:%M}"
                else:
                    when = f"post at {post.scheduled_at:%b %d %H:%M}"
                icon = STATUS_ICON.get(post.status, "•")
                st.caption(f"{icon} {actions.PLATFORM_LABEL[post.platform]}: {when}")

            locked = any(p.status == PostStatus.published for p in clip_posts)
            with st.expander("✂️ Edit hook / trim (re-renders the clip)"):
                if locked:
                    st.caption("Already posted, so it can't be changed.")
                hook = st.text_input(
                    "Hook on screen (first 3s)", clip.hook_text or "", key=f"hook-{key}"
                )
                s_col, e_col = st.columns(2)
                max_t = float(job.duration or clip.end + 60)
                start = s_col.number_input(
                    "Start (s)", 0.0, max_t, float(clip.start), 0.5, key=f"start-{key}"
                )
                end = e_col.number_input(
                    "End (s)", 0.0, max_t, float(clip.end), 0.5, key=f"end-{key}"
                )
                st.caption(f"New length: {end - start:.1f}s")
                edited = (
                    hook.strip() != (clip.hook_text or "")
                    or abs(start - clip.start) > 0.01
                    or abs(end - clip.end) > 0.01
                )
                if st.button("Re-render", key=f"rerender-{key}", disabled=locked or not edited):
                    with st.spinner("Re-rendering... (10-60s)"):
                        _do(actions.edit_clip, job_id, clip_id, hook, start, end, ok="Re-rendered")


def _do(fn, *args, ok: str, rerun: bool = True) -> None:
    try:
        fn(*args)
    except actions.ActionError as e:
        st.toast(f"❌ {e}")
        return
    st.toast(f"✅ {ok}")
    if rerun:
        st.rerun()


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
        return

    st.subheader(f"🔔 Due now ({len(due)})")
    if due:
        st.caption(
            "**Start posting** copies the caption, shows the clip file and opens the upload "
            "page. Drag the file in, add the suggested sound in the editor, paste, post, then "
            "**Mark as posted**."
        )
    else:
        st.caption("Nothing to post right now. The next ones are below.")
    for p in due:
        post_kit(p)

    st.subheader(f"🕒 Up next ({len(upcoming)})")
    for p in upcoming:
        c1, c2, c3 = st.columns([5, 1, 1])
        _, clip = actions.load_clip(p.job_id, p.clip_id)
        c1.write(
            f"**{p.scheduled_at:%a %b %d %H:%M}** · {PLATFORM_ICON[p.platform]} "
            f"{actions.PLATFORM_LABEL[p.platform]} · {clip.title or p.clip_id}"
        )
        if c2.button("Post now", key=f"early-{p.id}"):
            st.session_state[f"open-{p.id}"] = True
        if c3.button("Cancel", key=f"cancel-{p.id}"):
            actions.cancel_post(p.id)
            st.rerun(scope="fragment")
        if st.session_state.get(f"open-{p.id}"):
            post_kit(p)

    st.subheader(f"✅ Posted ({len(done)})")
    for p in sorted(done, key=lambda p: p.updated_at, reverse=True):
        c1, c2 = st.columns([6, 1])
        _, clip = actions.load_clip(p.job_id, p.clip_id)
        link = f" · [open]({p.url})" if p.url else ""
        c1.write(
            f"{PLATFORM_ICON[p.platform]} {actions.PLATFORM_LABEL[p.platform]} · "
            f"{clip.title or p.clip_id} · {p.updated_at:%b %d %H:%M}{link}"
        )
        if c2.button("Undo", key=f"undo-{p.id}", help="Clicked Mark as posted by mistake?"):
            _do(actions.undo_posted, p.id, ok="Moved back to the queue")


def post_kit(p) -> None:
    job, clip = actions.load_clip(p.job_id, p.clip_id)
    platform = actions.PLATFORM_LABEL[p.platform]
    with st.container(border=True):
        info, video = st.columns([3, 1])
        with info:
            st.markdown(
                f"{PLATFORM_ICON[p.platform]} **{platform}** · {clip.title or clip.id} · "
                f"due {p.scheduled_at:%b %d %H:%M}"
            )
            if st.button(f"🚀 Start posting to {platform}", key=f"start-{p.id}", type="primary"):
                _do(actions.start_posting, p.id, ok="Caption copied · file shown · page opened")
            st.code(post_text(job, clip, p.platform), language=None)  # also has a copy icon
            if clip.suggested_sound:
                st.markdown(
                    f"🎵 Add this sound in the editor: **{clip.suggested_sound}** · "
                    f"[find it on TikTok]({search_url(clip.suggested_sound)})"
                )
            b1, b2, b3 = st.columns(3)
            if b1.button("📋 Copy text", key=f"copy-{p.id}"):
                _do(actions.copy_text, p.id, ok="Copied", rerun=False)
            if b2.button("📂 Show file", key=f"file-{p.id}"):
                _do(actions.reveal_file, p.id, ok="File shown", rerun=False)
            if b3.button(f"↗ Open {platform}", key=f"page-{p.id}"):
                _do(actions.open_upload_page, p.id, ok="Upload page opened", rerun=False)

            url_col, done_col = st.columns([3, 1])
            url = url_col.text_input(
                "Link to the post (optional)", key=f"url-{p.id}", placeholder="https://..."
            )
            if done_col.button("✅ Mark as posted", key=f"posted-{p.id}"):
                _do(actions.mark_posted, p.id, url, ok="Marked as posted")
        with video:
            if clip.file and clip.file.exists():
                st.video(str(clip.file))


# ---------------------------------------------------------------- sounds


def page_sounds() -> None:
    sounds, fetched_at = actions.sounds_overview()
    st.subheader(f"🎵 Trending sounds ({len(sounds)})")
    st.caption(
        "Claude picks one of these for each clip. Add it inside the TikTok or Instagram "
        "editor when you post: we never put music in the file. "
        + (f"Last refresh: {fetched_at:%b %d %H:%M}." if fetched_at else "Never refreshed.")
    )
    if st.button("🔄 Refresh now", type="primary", key="refresh-sounds"):
        with st.spinner("Reading this week's trending lists... (about a minute)"):
            _do(actions.refresh_sounds, ok="Trending sounds updated")
    st.caption("Pin your own picks in `trending_sounds.yaml`; they're always offered first.")
    if sounds:
        st.dataframe(
            [
                {
                    "Sound": s.title,
                    "Artist": s.artist or "",
                    "Platform": s.platform,
                    "Vibe": s.vibe,
                    "Find": search_url(s.label),
                }
                for s in sounds
            ],
            column_config={"Find": st.column_config.LinkColumn("Find", display_text="TikTok")},
            hide_index=True,
            use_container_width=True,
        )


# ---------------------------------------------------------------- main

header()
PAGES = {"New video": page_new, "Jobs": page_jobs, "Queue": page_queue, "Sounds": page_sounds}
page = st.sidebar.radio("Page", list(PAGES), key="page")
if due := len(actions.due_now()):
    st.sidebar.success(f"🔔 {due} post(s) due now on the **Queue** page")
PAGES[page]()
