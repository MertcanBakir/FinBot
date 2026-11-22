from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from app.models.job_tracking import JobRunOutcome

TResult = TypeVar("TResult")


class JobResultAdapter(ABC, Generic[TResult]):
    """Abstract Strategy interface for transforming concrete job result models into standardized JobRunOutcome."""

    @abstractmethod
    def to_outcome(self, result: TResult) -> JobRunOutcome:
        """Transform job result into a standardized JobRunOutcome."""
        raise NotImplementedError
