from datetime import date, datetime
from typing import List, Literal, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.market import EconomicEvent
from app.services.economic_event_identity import (
    economic_occurrence_key,
    economic_provider_key,
)

EconomicCalendarStatus = Literal["success", "partial", "failed"]
EconomicProviderStatus = Literal["success", "partial", "failed"]


class EconomicProviderResult(BaseModel):
    provider_id: str
    provider_name: str
    status: EconomicProviderStatus
    structure_validated: bool = False

    event_count: int = 0
    carried_forward_event_count: int = 0
    observed_candidate_count: int = 0
    candidate_count: int = 0
    malformed_count: int = 0

    warnings: List[str] = Field(default_factory=list)
    error: Optional[str] = None


class RollingEconomicCalendar(BaseModel):
    schema_version: Literal["2.0"] = "2.0"
    generated_at: str
    coverage_start: str
    coverage_end: str
    timezone: Literal["Europe/Istanbul"] = "Europe/Istanbul"
    status: EconomicCalendarStatus

    provider_results: List[EconomicProviderResult] = Field(default_factory=list)
    events: List[EconomicEvent] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)

    @field_validator("coverage_start", "coverage_end")
    @classmethod
    def validate_date_format(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except Exception as e:
            raise ValueError(
                f"Invalid date format for '{v}', expected YYYY-MM-DD: {e}"
            ) from e
        return v

    @model_validator(mode="after")
    def validate_coverage_range_and_events(self) -> "RollingEconomicCalendar":
        d_start = date.fromisoformat(self.coverage_start)
        d_end = date.fromisoformat(self.coverage_end)

        delta = (d_end - d_start).days
        if delta != 90:
            raise ValueError(
                f"Coverage window must be exactly 90 days (91 days inclusive). Got {delta} days between {self.coverage_start} and {self.coverage_end}."
            )

        tz = ZoneInfo(self.timezone)
        seen_occurrences = set()
        for ev in self.events:
            try:
                dt_ev = datetime.fromisoformat(ev.scheduled_at_istanbul)
                if dt_ev.tzinfo is None:
                    dt_ev = dt_ev.replace(tzinfo=tz)
                else:
                    dt_ev = dt_ev.astimezone(tz)
            except Exception as e:
                raise ValueError(
                    "Invalid scheduled_at_istanbul datetime "
                    f"'{ev.scheduled_at_istanbul}': {e}"
                ) from e

            ev_date = dt_ev.date()
            if ev_date < d_start or ev_date > d_end:
                raise ValueError(
                    f"Event '{ev.name}' ({ev.scheduled_at_istanbul}) falls outside coverage range [{self.coverage_start}, {self.coverage_end}]."
                )

            # Strict occurrence deduplication
            occ_key = economic_occurrence_key(ev)
            if occ_key in seen_occurrences:
                raise ValueError(f"Duplicate event found in calendar: {ev.name} (occurrence key: {occ_key})")
            seen_occurrences.add(occ_key)

        # Validate provider count consistency with calendar event list
        if self.provider_results:
            total_expected_events = 0
            for pr in self.provider_results:
                expected_pr_count = pr.event_count + pr.carried_forward_event_count
                total_expected_events += expected_pr_count
                actual_pr_count = sum(
                    1 for e in self.events
                    if economic_provider_key(e) in (pr.provider_id, pr.provider_name)
                )
                if actual_pr_count != expected_pr_count:
                    raise ValueError(
                        f"Provider '{pr.provider_id}' count mismatch: {actual_pr_count} events in calendar vs {expected_pr_count} reported (fresh={pr.event_count}, carried={pr.carried_forward_event_count})."
                    )

            if len(self.events) != total_expected_events:
                raise ValueError(
                    f"Total event count ({len(self.events)}) does not match sum of provider reported counts ({total_expected_events})."
                )

        return self
