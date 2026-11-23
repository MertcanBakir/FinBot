from abc import ABC, abstractmethod
from typing import Generic, Optional, Tuple, TypeVar

from app.models.bist_prices import BistPricesJobResult
from app.models.crypto_prices import BinanceCryptoPricesJobResult
from app.models.earnings_refresh import EarningsRefreshJobResult
from app.models.economic_calendar_refresh import EconomicCalendarRefreshJobResult
from app.models.fx_prices import FxPriceJobResult
from app.models.nasdaq_prices import NasdaqPricesJobResult
from app.models.portfolio_snapshots import PortfolioSnapshotJobResult
from app.models.tefas_prices import TefasPricesJobResult
from app.services.bist_prices_report import (
    format_bist_prices_email_html,
    format_bist_prices_email_text,
    format_bist_prices_subject,
)
from app.services.crypto_prices_report import (
    format_crypto_prices_email_html,
    format_crypto_prices_email_text,
    format_crypto_prices_subject,
)
from app.services.earnings_refresh_report import (
    format_earnings_refresh_email_html,
    format_earnings_refresh_email_text,
    format_earnings_refresh_subject,
)
from app.services.economic_calendar_refresh_report import (
    format_economic_calendar_refresh_email_html,
    format_economic_calendar_refresh_email_text,
    format_economic_calendar_refresh_subject,
)
from app.services.nasdaq_prices_report import (
    format_nasdaq_prices_email_html,
    format_nasdaq_prices_email_text,
    format_nasdaq_prices_subject,
)
from app.services.portfolio_snapshots_report import (
    format_portfolio_snapshots_email_html,
    format_portfolio_snapshots_email_text,
    format_portfolio_snapshots_subject,
)
from app.services.tefas_prices_report import (
    format_tefas_prices_email_html,
    format_tefas_prices_email_text,
    format_tefas_prices_subject,
)

TResult = TypeVar("TResult")


class JobEmailRenderer(ABC, Generic[TResult]):
    """Abstract strategy for rendering rich email subjects and bodies for a specific job result."""

    @abstractmethod
    def render(self, result: TResult) -> Tuple[str, str, Optional[str]]:
        """Return (subject, body_text, body_html)."""
        raise NotImplementedError


class NasdaqPricesEmailRenderer(JobEmailRenderer[NasdaqPricesJobResult]):
    def render(self, result: NasdaqPricesJobResult) -> Tuple[str, str, Optional[str]]:
        subject = format_nasdaq_prices_subject(result)
        body_text = format_nasdaq_prices_email_text(result)
        body_html = format_nasdaq_prices_email_html(result)
        return subject, body_text, body_html


class BistPricesEmailRenderer(JobEmailRenderer[BistPricesJobResult]):
    def render(self, result: BistPricesJobResult) -> Tuple[str, str, Optional[str]]:
        subject = format_bist_prices_subject(result)
        body_text = format_bist_prices_email_text(result)
        body_html = format_bist_prices_email_html(result)
        return subject, body_text, body_html


class BinanceCryptoPricesEmailRenderer(JobEmailRenderer[BinanceCryptoPricesJobResult]):
    def render(self, result: BinanceCryptoPricesJobResult) -> Tuple[str, str, Optional[str]]:
        subject = format_crypto_prices_subject(result)
        body_text = format_crypto_prices_email_text(result)
        body_html = format_crypto_prices_email_html(result)
        return subject, body_text, body_html


class TefasPricesEmailRenderer(JobEmailRenderer[TefasPricesJobResult]):
    def render(self, result: TefasPricesJobResult) -> Tuple[str, str, Optional[str]]:
        subject = format_tefas_prices_subject(result)
        body_text = format_tefas_prices_email_text(result)
        body_html = format_tefas_prices_email_html(result)
        return subject, body_text, body_html


class FxPriceEmailRenderer(JobEmailRenderer[FxPriceJobResult]):
    def render(self, result: FxPriceJobResult) -> Tuple[str, str, Optional[str]]:
        status_label = result.status.upper()
        subject = f"[FinBot UYARI] USDTRY fiyat job'u — {status_label}"
        body_text = (
            f"Durum: {result.status}\n"
            f"Fiyat: {result.price}\n"
            f"Önceki kapanış: {result.previous_close}\n"
            f"Hata: {result.error or '-'}"
        )
        return subject, body_text, None


class EconomicCalendarEmailRenderer(JobEmailRenderer[EconomicCalendarRefreshJobResult]):
    def render(self, result: EconomicCalendarRefreshJobResult) -> Tuple[str, str, Optional[str]]:
        subject = format_economic_calendar_refresh_subject(result)
        body_text = format_economic_calendar_refresh_email_text(result)
        body_html = format_economic_calendar_refresh_email_html(result)
        return subject, body_text, body_html


class EarningsEmailRenderer(JobEmailRenderer[EarningsRefreshJobResult]):
    def render(self, result: EarningsRefreshJobResult) -> Tuple[str, str, Optional[str]]:
        subject = format_earnings_refresh_subject(result)
        body_text = format_earnings_refresh_email_text(result)
        body_html = format_earnings_refresh_email_html(result)
        return subject, body_text, body_html


class PortfolioSnapshotEmailRenderer(JobEmailRenderer[PortfolioSnapshotJobResult]):
    def render(self, result: PortfolioSnapshotJobResult) -> Tuple[str, str, Optional[str]]:
        subject = format_portfolio_snapshots_subject(result)
        body_text = format_portfolio_snapshots_email_text(result)
        body_html = format_portfolio_snapshots_email_html(result)
        return subject, body_text, body_html
