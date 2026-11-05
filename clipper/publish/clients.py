"""The interface every platform client implements, and how the scheduler picks clients."""

from __future__ import annotations

from typing import Protocol

from clipper import config
from clipper.models import Clip, Job, Platform, PostResult


class PublishError(Exception):
    """The client is SURE nothing went out (failed before upload, or the API rejected it).

    retryable=True: transient (network, rate limit, 5xx) -> the scheduler tries again later.
    retryable=False: permanent (bad file, revoked token) -> the post is marked failed.

    If the client can't be sure (e.g. the connection dropped after the final publish call),
    it must raise UploadStateUnknown, or let any other exception escape -> needs_check.
    """

    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


class UploadStateUnknown(Exception):
    """The upload may or may not have gone out. A human must check the account."""


class PublishClient(Protocol):
    platform: Platform

    def publish(self, job: Job, clip: Clip) -> PostResult:
        """Upload the clip. Return a PostResult with status published or uploaded_draft.
        Raise PublishError on known failures."""
        ...


def make_clients(platforms: list[Platform]) -> dict[Platform, PublishClient]:
    """DryRun clients unless DRY_RUN is explicitly false. Fails loudly if a real client
    is missing, so the scheduler never claims posts it can't handle."""
    from clipper.publish.dryrun import DryRunClient

    if config.DRY_RUN:
        return {p: DryRunClient(p) for p in platforms}

    clients: dict[Platform, PublishClient] = {}
    for p in platforms:
        clients[p] = _real_client(p)
    return clients


def _real_client(platform: Platform) -> PublishClient:
    # B5 (TikTok) and B6 (Instagram) plug in here.
    raise NotImplementedError(
        f"no real {platform.value} client yet; keep DRY_RUN=true or remove "
        f"{platform.value} from ENABLED_PLATFORMS"
    )
