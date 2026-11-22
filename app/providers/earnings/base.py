from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, List, Tuple


class BaseEarningsProvider(ABC):
    """Abstract base class for market earnings providers."""

    @abstractmethod
    async def get_earnings_events(
        self,
        start_date: datetime,
        end_date: datetime
    ) -> Tuple[List[Any], List[str]]:
        """Fetch earnings events within the specified date window.
        
        Returns:
            Tuple of (list_of_events, list_of_warnings)
        """
        pass
