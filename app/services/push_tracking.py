"""Send/receipt batches enter central job tracking without personal metrics."""

from app.models.job_tracking import JobRunOutcome, JobRunSpecification
from app.services.email_service import EmailService
from app.services.job_tracking.notification.policy import JobNotificationRule
from app.services.job_tracking.runner import TrackedJobRunner


class PushAdapter:
    def to_outcome(self, result):
        partial = any(
            result.get(status, 0) for status in ("failed", "unknown", "pending")
        )
        return JobRunOutcome(
            status="partial" if partial else "success",
            sync_action="partial_update" if partial else "updated",
            metrics=result,
        )


class PushRenderer:
    def render(self, result):
        return "FinBot Push operasyonu", "Push işleme tamamlandı.", None


def tracked_batch(fn, db_connection_factory=None):
    runner = TrackedJobRunner(
        spec=JobRunSpecification(
            job_name="push_notifications_batch",
            operation_type="notification_dispatch",
            scope="PUSH",
        ),
        adapter=PushAdapter(),
        renderer=PushRenderer(),
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        email_service=EmailService(),
        db_connection_factory=db_connection_factory,
    )
    # Do not send an email every minute on an outage. Delivery state stays in DB.
    return runner.run(fn, dry_run=False, no_email=True)
