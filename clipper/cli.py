"""CLI entrypoint. Each stage module must expose `run(job: Job, force: bool) -> Job`."""

from __future__ import annotations

import importlib
import logging
import uuid

import typer

from clipper import config
from clipper.models import Job, JobStatus

app = typer.Typer(help="YouTube -> vertical clips -> TikTok/IG")
STAGES = ["ingest", "analyze", "captions", "render"]  # publish runs via the scheduler

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
def ui() -> None:
    """Open the dashboard in your browser."""
    import subprocess
    import sys
    from pathlib import Path

    app_file = Path(__file__).parent / "ui" / "app.py"
    subprocess.run(
        [sys.executable, "-m", "streamlit", "run", str(app_file)]
        # localhost only: the dashboard can approve posts, so don't expose it on the network
        + ["--server.address", "localhost", "--browser.gatherUsageStats", "false"],
        cwd=config.ROOT,
        check=False,
    )


@app.command("scheduler")
def scheduler_cmd(
    once: bool = typer.Option(False, "--once", help="Run a single tick and exit."),
) -> None:
    """Post queued clips when they're due. Uses the dry-run client unless DRY_RUN=false."""
    from clipper.publish import queue
    from clipper.publish import scheduler as sched
    from clipper.publish.clients import make_clients

    config.WORKSPACE.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(config.WORKSPACE / "scheduler.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)

    try:
        clients = make_clients(queue.enabled_platforms())
    except (NotImplementedError, queue.EnqueueError) as e:
        typer.echo(f"can't start scheduler: {e}", err=True)
        raise typer.Exit(1) from e

    conn = queue.connect()
    if once:
        typer.echo(f"processed {sched.run_once(conn, clients)} post(s)")
        return
    try:
        sched.run_forever(conn, clients)
    except KeyboardInterrupt:
        typer.echo("scheduler stopped")


if __name__ == "__main__":
    app()
