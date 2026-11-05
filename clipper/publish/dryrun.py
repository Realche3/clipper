"""DryRunClient: logs what would be posted and touches no account. Used when DRY_RUN=true."""

from __future__ import annotations

import logging

from clipper.models import Clip, Job, Platform, PostResult, PostStatus
from clipper.publish.text import post_text

log = logging.getLogger(__name__)


class DryRunClient:
    def __init__(self, platform: Platform):
        self.platform = platform

    def publish(self, job: Job, clip: Clip) -> PostResult:
        log.info(
            "[DRY RUN] would post %s/%s to %s: file=%s text=%r",
            job.id,
            clip.id,
            self.platform.value,
            clip.file,
            post_text(job, clip, self.platform),
        )
        # Mirror what the real client would return, so the rest of the flow is exercised.
        status = (
            PostStatus.uploaded_draft if self.platform == Platform.tiktok else PostStatus.published
        )
        return PostResult(status=status, remote_id=f"dryrun-{job.id}-{clip.id}")
