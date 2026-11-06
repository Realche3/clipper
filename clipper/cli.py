"""CLI entrypoint. Each stage module must expose `run(job: Job, force: bool) -> Job`."""

from __future__ import annotations

import importlib
import logging
import uuid

import typer

from clipper import config
from clipper.models import Job, JobStatus

app = typer.Typer(help="YouTube -> vertical clips -> TikTok/IG")
STAGES = ["ingest", "analyze", "captions", "render"]  # posting happens from the dashboard

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def _run_stage(stage: str, job: Job, force: bool, clip: str | None = None) -> Job:
    module = importlib.import_module(f"clipper.{stage}")
    if not hasattr(module, "run"):
        typer.echo(f"[skip] stage '{stage}' not implemented yet")
        return job
    try:
        job = module.run(job, force=force, **({"clip_id": clip} if clip else {}))
    except Exception as e:  # keep the manifest, record the failure
        job.status = JobStatus.failed
        job.error = f"{stage}: {e}"
        job.save(config.WORKSPACE)
        raise
    job.save(config.WORKSPACE)
    return job


@app.command()
def run(
    url: str,
    context: str = typer.Option(
        None, "--context", "-c", help="What the video is about; steers captions and tags."
    ),
    force: bool = False,
    until: str = "render",
    job_id: str = typer.Option(
        None, "--job-id", help="Use this job id (the dashboard picks it so it can track progress)."
    ),
) -> None:
    """Run the pipeline on a YouTube URL (stops at render, ready for review)."""
    job = Job(id=job_id or uuid.uuid4().hex[:8], source_url=url, context=context)
    job.save(config.WORKSPACE)
    typer.echo(f"job {job.id}")
    for stage in STAGES[: STAGES.index(until) + 1]:
        job = _run_stage(stage, job, force)


@app.command()
def stage(
    name: str,
    job_id: str,
    force: bool = False,
    clip: str = typer.Option(None, "--clip", help="Render only this clip id (render only)."),
) -> None:
    """Run a single stage on an existing job."""
    if name not in STAGES:
        raise typer.BadParameter(f"stage must be one of {STAGES}")
    if clip and name != "render":
        raise typer.BadParameter("--clip only works with the render stage")
    _run_stage(name, Job.load(config.WORKSPACE, job_id), force, clip)


@app.command()
def resume(job_id: str) -> None:
    """Re-run the pipeline on an existing (e.g. failed) job. Finished work is skipped."""
    job = Job.load(config.WORKSPACE, job_id)
    job.error = None
    typer.echo(f"resuming job {job.id}")
    for name in STAGES:
        job = _run_stage(name, job, force=False)


@app.command()
def ui() -> None:
    """Open the dashboard in your browser."""
    import subprocess
    import sys
    from pathlib import Path

    app_file = Path(__file__).parent / "ui" / "app.py"
    subprocess.run(
        [sys.executable, "-m", "streamlit", "run", str(app_file)]
        # localhost only: the dashboard opens files and edits the queue, so keep it off the network
        + ["--server.address", "localhost", "--browser.gatherUsageStats", "false"],
        cwd=config.ROOT,
        check=False,
    )


sounds_app = typer.Typer(help="Trending sounds suggested for each clip.")
app.add_typer(sounds_app, name="sounds")


@sounds_app.command("refresh")
def sounds_refresh() -> None:
    """Fetch the current trending TikTok / Reels sounds from TRENDING_SOURCES."""
    from clipper.sounds.fetch import SoundsError, refresh

    try:
        data = refresh()
    except SoundsError as e:
        typer.echo(f"refresh failed: {e}", err=True)
        raise typer.Exit(1) from e
    for src in data.sources:
        typer.echo(f"  {src.count:>3}  {src.url}" + (f"  ({src.error})" if src.error else ""))
    typer.echo(f"{len(data.sounds)} trending sounds saved to {config.TRENDING_SOUNDS_JSON}")


@sounds_app.command("list")
def sounds_list() -> None:
    """Show the sounds Claude picks from (pinned first)."""
    from clipper.sounds.store import all_sounds, load_fetched

    fetched = load_fetched()
    when = f"{fetched.fetched_at:%b %d %H:%M}" if fetched and fetched.fetched_at else "never"
    sounds = all_sounds()
    typer.echo(f"{len(sounds)} sounds (last refresh: {when})")
    for i, s in enumerate(sounds, start=1):
        typer.echo(f"{i:>4}. {s.label}  [{s.platform}, {s.vibe}]")


if __name__ == "__main__":
    app()
