"""Canonical job names for FinBot calendar refresh jobs.

This module serves as the shared contract between calendar refresh jobs and
their corresponding database state readers, preventing name divergence and
circular dependencies.
"""

from typing import Final

JOB_NAME_EARNINGS_REFRESH: Final[str] = "earnings_refresh_job"
JOB_NAME_ECONOMIC_CALENDAR_REFRESH: Final[str] = "economic_calendar_refresh_job"

