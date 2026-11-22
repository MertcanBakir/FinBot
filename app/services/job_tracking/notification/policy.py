from enum import Enum
from typing import Literal

from pydantic import BaseModel


class JobNotificationRule(str, Enum):
    """Notification trigger rule for a specific job category."""

    ALERT_ON_PARTIAL_OR_FAILED = "ALERT_ON_PARTIAL_OR_FAILED"
    ALWAYS_ON_FINISH = "ALWAYS_ON_FINISH"


class NotificationDecision(BaseModel):
    """Decision outcome of the NotificationPolicy evaluation."""

    should_send: bool
    initial_notification_status: Literal[
        "not_required",
        "pending",
        "disabled",
        "skipped_dry_run",
        "not_configured",
        "failed",
    ]
    reason: str


class NotificationPolicy:
    """Central notification policy governing email delivery decisions across all job types."""

    @staticmethod
    def evaluate(
        rule: JobNotificationRule,
        status: str,
        dry_run: bool = False,
        no_email: bool = False,
        is_email_configured: bool = True,
    ) -> NotificationDecision:
        """Evaluate whether an email should be sent and determine the initial notification status."""
        if dry_run:
            return NotificationDecision(
                should_send=False,
                initial_notification_status="skipped_dry_run",
                reason="Execution was run in dry-run mode (--dry-run).",
            )

        if no_email:
            return NotificationDecision(
                should_send=False,
                initial_notification_status="disabled",
                reason="Email delivery disabled via CLI flag (--no-email).",
            )

        # Check if job outcome requires notification
        warrants_notification: bool
        if rule == JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED:
            warrants_notification = status in ("partial", "failed")
        elif rule == JobNotificationRule.ALWAYS_ON_FINISH:
            warrants_notification = status in ("success", "partial", "failed")
        else:
            warrants_notification = False

        if not warrants_notification:
            return NotificationDecision(
                should_send=False,
                initial_notification_status="not_required",
                reason=f"Status '{status}' does not warrant notification under rule '{rule.value}'.",
            )

        if not is_email_configured:
            return NotificationDecision(
                should_send=False,
                initial_notification_status="not_configured",
                reason="Email service credentials/configuration missing.",
            )

        return NotificationDecision(
            should_send=True,
            initial_notification_status="pending",
            reason=f"Notification warranted for status '{status}' under rule '{rule.value}'.",
        )
