from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

MarketSummaryStatus = Literal["success", "partial", "failed", "skipped"]
MarketSummaryReportType = Literal["morning", "closing"]
MarketSummaryEmailStatus = Literal[
    "sent",
    "failed",
    "skipped_not_configured",
    "skipped_dry_run",
    "skipped_no_email",
]


class MarketSummaryJobResult(BaseModel):
    """Result model for morning and closing market summary jobs."""

    status: MarketSummaryStatus
    report_type: MarketSummaryReportType
    started_at: str
    finished_at: Optional[str] = None
    duration_seconds: float = 0.0

    warnings: List[str] = Field(default_factory=list)
    error: Optional[str] = None

    email_status: Optional[MarketSummaryEmailStatus] = None
    email_error: Optional[str] = None

    metrics: Dict[str, Any] = Field(default_factory=dict)

    subject: Optional[str] = None
    body_text: Optional[str] = None
    body_html: Optional[str] = None

    dry_run: bool = False
    no_email: bool = False


def determine_cli_exit_code(result: MarketSummaryJobResult) -> int:
    """Determine deterministic CLI exit code for market summary jobs.

    Rules:
    - If job status is 'failed' -> exit 1
    - In --dry-run or --no-email mode:
      - Returns 0 if job status is 'success', 'partial', or 'skipped'
      - Returns 1 if job status is 'failed'
    - In normal production mode (dry_run=False, no_email=False):
      - Returns 0 only when email_status is strictly 'sent' (including for 'partial' reports)
      - Returns 1 for any other email status ('failed', 'skipped_not_configured', None, etc.)
    """
    if result.status == "failed":
        return 1

    if result.dry_run or result.no_email:
        return 0 if result.status in ("success", "partial", "skipped") else 1

    return 0 if result.email_status == "sent" else 1
