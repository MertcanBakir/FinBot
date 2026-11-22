import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Optional

import httpx

from app.models.earnings import OfficialIrIssue, OfficialIrProviderResult

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
}

RETRIABLE_EXCEPTIONS = (
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
    httpx.ConnectError,
    httpx.NetworkError,
)


class BaseOfficialIrProvider(ABC):
    symbol: str
    company_name: str
    source_provider: str
    url: str
    timeout: float = 12.0
    max_retries: int = 3
    base_backoff_seconds: float = 1.0

    def __init__(self, timeout: Optional[float] = None, max_retries: Optional[int] = None):
        if timeout is not None:
            self.timeout = timeout
        if max_retries is not None:
            self.max_retries = max_retries

    @abstractmethod
    def parse_html(self, html: str) -> OfficialIrProviderResult:
        """Parse server-rendered HTML into structured OfficialIrProviderResult."""
        pass

    async def fetch_calendar(self, client: Optional[httpx.AsyncClient] = None) -> OfficialIrProviderResult:
        """Fetch live official IR page and parse earnings calendar events with exponential retry/backoff."""
        close_client = False
        if client is None:
            client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, headers=DEFAULT_HEADERS)
            close_client = True

        retry_count = 0
        last_error_msg = ""

        try:
            for attempt in range(1, self.max_retries + 1):
                try:
                    res = await client.get(self.url)
                    # Check retriable HTTP status codes (429, all 5xx)
                    if res.status_code == 429 or (500 <= res.status_code < 600):
                        last_error_msg = f"HTTP {res.status_code} fetching official IR page from {self.url}"
                        if attempt < self.max_retries:
                            retry_count += 1
                            delay = self.base_backoff_seconds * (2 ** (attempt - 1))
                            logger.warning(f"[{self.symbol}] Retry attempt {retry_count} after HTTP {res.status_code} (waiting {delay}s)")
                            await asyncio.sleep(delay)
                            continue
                        else:
                            msg = f"HTTP {res.status_code} fetching official IR page after {retry_count} retries"
                            return OfficialIrProviderResult(
                                symbol=self.symbol,
                                source_provider=self.source_provider,
                                status="failed",
                                events=[],
                                retry_count=retry_count,
                                warnings=[msg],
                                issues=[OfficialIrIssue(message=msg, kind="provider_failure", is_global=True)]
                            )

                    # Non-retriable non-200 HTTP status (e.g. 404, 403, 401)
                    if res.status_code != 200:
                        logger.warning(f"[{self.symbol}] Official IR fetch non-retriable HTTP {res.status_code} at {self.url}")
                        msg = f"HTTP {res.status_code} fetching official IR page from {self.url}"
                        return OfficialIrProviderResult(
                            symbol=self.symbol,
                            source_provider=self.source_provider,
                            status="failed",
                            events=[],
                            retry_count=retry_count,
                            warnings=[msg],
                            issues=[OfficialIrIssue(message=msg, kind="provider_failure", is_global=True)]
                        )

                    # 200 OK -> parse
                    parsed_result = self.parse_html(res.text)
                    parsed_result.retry_count = retry_count
                    return parsed_result

                except RETRIABLE_EXCEPTIONS as exc:
                    last_error_msg = str(exc)
                    if attempt < self.max_retries:
                        retry_count += 1
                        delay = self.base_backoff_seconds * (2 ** (attempt - 1))
                        logger.warning(f"[{self.symbol}] Retry attempt {retry_count} after error {exc} (waiting {delay}s)")
                        await asyncio.sleep(delay)
                        continue
                    else:
                        logger.error(f"[{self.symbol}] Exhausted retries after network error: {exc}", exc_info=True)
                        msg = f"Network/exception fetching official IR page after {retry_count} retries: {exc}"
                        return OfficialIrProviderResult(
                            symbol=self.symbol,
                            source_provider=self.source_provider,
                            status="failed",
                            events=[],
                            retry_count=retry_count,
                            warnings=[msg],
                            issues=[OfficialIrIssue(message=msg, kind="provider_failure", is_global=True)]
                        )
                except Exception as unhandled:
                    logger.error(f"[{self.symbol}] Non-retriable exception fetching official IR page: {unhandled}", exc_info=True)
                    msg = f"Unhandled exception fetching official IR page: {str(unhandled)}"
                    return OfficialIrProviderResult(
                        symbol=self.symbol,
                        source_provider=self.source_provider,
                        status="failed",
                        events=[],
                        retry_count=retry_count,
                        warnings=[msg],
                        issues=[OfficialIrIssue(message=msg, kind="provider_failure", is_global=True)]
                    )

            msg = f"Fetch failed after {retry_count} retries: {last_error_msg}"
            return OfficialIrProviderResult(
                symbol=self.symbol,
                source_provider=self.source_provider,
                status="failed",
                events=[],
                retry_count=retry_count,
                warnings=[msg],
                issues=[OfficialIrIssue(message=msg, kind="provider_failure", is_global=True)]
            )
        finally:
            if close_client:
                await client.aclose()
