from app.services.job_tracking.adapters.base import JobResultAdapter
from app.services.job_tracking.adapters.bist_adapter import BistPricesResultAdapter
from app.services.job_tracking.adapters.crypto_adapter import (
    BinanceCryptoPricesResultAdapter,
)
from app.services.job_tracking.adapters.earnings_adapter import (
    EarningsRefreshResultAdapter,
)
from app.services.job_tracking.adapters.economic_calendar_adapter import (
    EconomicCalendarRefreshResultAdapter,
)
from app.services.job_tracking.adapters.fx_adapter import FxPriceResultAdapter
from app.services.job_tracking.adapters.market_summary_adapter import (
    MarketSummaryResultAdapter,
)
from app.services.job_tracking.adapters.nasdaq_adapter import NasdaqPricesResultAdapter
from app.services.job_tracking.adapters.portfolio_snapshot_adapter import (
    PortfolioSnapshotResultAdapter,
)
from app.services.job_tracking.adapters.tefas_adapter import TefasPricesResultAdapter

__all__ = [
    "JobResultAdapter",
    "NasdaqPricesResultAdapter",
    "BistPricesResultAdapter",
    "BinanceCryptoPricesResultAdapter",
    "TefasPricesResultAdapter",
    "FxPriceResultAdapter",
    "EconomicCalendarRefreshResultAdapter",
    "EarningsRefreshResultAdapter",
    "PortfolioSnapshotResultAdapter",
    "MarketSummaryResultAdapter",
]
