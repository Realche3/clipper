"""Clipper dashboard. Start with `uv run clipper ui`.

Rendering only: every action lives in clipper/ui/actions.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import streamlit as st

from clipper import config
from clipper.models import JobStatus, Platform, PostStatus, ReviewStatus
from clipper.publish.text import post_text
from clipper.ui import actions

st.set_page_config(page_title="Clipper", page_icon="🎬", layout="wide")

STATUS_ICON = {
    PostStatus.scheduled: "🕒",
    PostStatus.uploading: "⏫",
    PostStatus.uploaded_draft: "📥",
    PostStatus.published: "✅",
    PostStatus.failed: "❌",
    PostStatus.needs_check: "⚠️",
}
REVIEW_ICON = {ReviewStatus.pending: "⏳", ReviewStatus.approved: "✅", ReviewStatus.rejected: "🚫"}


# ---------------------------------------------------------------- header


def header() -> None:
    left, right = st.columns([3, 2])
    left.title("🎬 Clipper")
    with right:
        if config.DRY_RUN:
            st.info("**DRY RUN**: nothing is posted to real accounts.", icon="🧪")
        else:
            st.warning("**LIVE**: approved clips will be posted.", icon="🔴")
        seen = actions.scheduler_last_seen()
        if seen and datetime.now() - seen < timedelta(minutes=3):
            st.caption(f"Scheduler running (last tick {seen:%H:%M:%S})")
        else:
            st.caption(
                "⚠️ Scheduler not running. Start it with `uv run clipper scheduler`."
                if seen is None
                else f"⚠️ Scheduler last seen {seen:%b %d %H:%M}. Is it still running?"
            )


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
                st.caption(f"🎵 Suggested TikTok sound: **{clip.suggested_sound}**")

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
                st.caption(
                    f"{STATUS_ICON[post.status]} {actions.PLATFORM_LABEL[post.platform]}: "
                    f"{post.status.value} · {post.scheduled_at:%b %d %H:%M}"
                    + (f" · {post.error}" if post.error else "")
                )

            locked = any(p.status != PostStatus.scheduled for p in clip_posts)
            with st.expander("✂️ Edit hook / trim (re-renders the clip)"):
                if locked:
                    st.caption("Already uploaded, so it can't be changed.")
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


def _do(fn, *args, ok: str) -> None:
    try:
        fn(*args)
    except actions.ActionError as e:
        st.toast(f"❌ {e}")
        return
    st.toast(f"✅ {ok}")
    st.rerun()


# ---------------------------------------------------------------- queue


def page_queue() -> None:
    all_posts = actions.posts()
    if not all_posts:
        st.info("Nothing queued yet. Approve clips on the **Jobs** page.")
        return

    drafts = [
        p
        for p in all_posts
        if p.platform == Platform.tiktok and p.status == PostStatus.uploaded_draft
    ]
    problems = [p for p in all_posts if p.status in (PostStatus.failed, PostStatus.needs_check)]
    upcoming = [p for p in all_posts if p.status in (PostStatus.scheduled, PostStatus.uploading)]
    done = [p for p in all_posts if p.status == PostStatus.published]

    st.subheader(f"📥 TikTok drafts to finish in the app ({len(drafts)})")
    st.caption("Open TikTok → inbox notification → add the sound → paste the text → post.")
    for p in drafts:
        job, clip = actions.load_clip(p.job_id, p.clip_id)
        with st.container(border=True):
            st.markdown(f"**{clip.title or clip.id}**  ·  uploaded {p.updated_at:%b %d %H:%M}")
            if clip.suggested_sound:
                st.markdown(f"🎵 Sound: **{clip.suggested_sound}**")
            st.code(post_text(job, clip, Platform.tiktok), language=None)  # has a copy button
            url_col, btn_col = st.columns([3, 1])
            url = url_col.text_input(
                "TikTok link (optional)",
                key=f"url-{p.id}",
                placeholder="https://www.tiktok.com/@...",
            )
            if btn_col.button("Mark as posted", key=f"posted-{p.id}", type="primary"):
                _do(actions.mark_posted, p.id, url, ok="Marked as posted")

    if problems:
        st.subheader(f"⚠️ Needs attention ({len(problems)})")
        for p in problems:
            with st.container(border=True):
                where = f"{actions.PLATFORM_LABEL[p.platform]} · {p.job_id}/{p.clip_id}"
                if p.status == PostStatus.needs_check:
                    st.warning(
                        f"⚠️ **{where}**: the upload was interrupted, so it may or may not "
                        f"have posted. Check the account, then pick one.  \n{p.error or ''}"
                    )
                    c1, c2, c3 = st.columns(3)
                    if c1.button("It's live", key=f"live-{p.id}"):
                        _do(actions.mark_posted, p.id, None, ok="Marked as posted")
                    if c2.button("It didn't post: retry", key=f"retry-{p.id}"):
                        _do(actions.retry_post, p.id, ok="Rescheduled")
                    if c3.button("Drop it", key=f"drop-{p.id}"):
                        _do(actions.give_up_post, p.id, ok="Dropped")
                else:
                    st.error(f"❌ **{where}**: gave up after retries.  \n{p.error or ''}")
                    if st.button("Try again", key=f"retry-{p.id}"):
                        _do(actions.retry_post, p.id, ok="Rescheduled")

    st.subheader(f"🕒 Upcoming ({len(upcoming)})")
    for p in upcoming:
        c1, c2 = st.columns([5, 1])
        c1.write(
            f"{STATUS_ICON[p.status]} **{p.scheduled_at:%a %b %d %H:%M}** · "
            f"{actions.PLATFORM_LABEL[p.platform]} · {p.job_id}/{p.clip_id}"
        )
        if p.status == PostStatus.scheduled and c2.button("Cancel", key=f"cancel-{p.id}"):
            actions.cancel_post(p.id)
            st.rerun()

    st.subheader(f"✅ Posted ({len(done)})")
    for p in done:
        link = f" · [open]({p.url})" if p.url else ""
        st.write(
            f"{actions.PLATFORM_LABEL[p.platform]} · {p.job_id}/{p.clip_id} · "
            f"{p.updated_at:%b %d %H:%M}{link}"
        )


# ---------------------------------------------------------------- main

header()
PAGES = {"New video": page_new, "Jobs": page_jobs, "Queue": page_queue}
page = st.sidebar.radio("Page", list(PAGES), key="page")
if problems := actions.problem_count():
    st.sidebar.error(f"⚠️ {problems} post(s) need attention on the **Queue** page")
PAGES[page]()
