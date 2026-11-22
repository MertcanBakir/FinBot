import asyncio
import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

BACKOFF_DELAYS = [0.5, 1.0, 2.0]
MAX_ATTEMPTS = 4


def describe_exception(exc: Exception) -> str:
    """
    Format exception in a non-empty, clear manner.
    """
    text = str(exc).strip()
    if text:
        return f"{type(exc).__name__}: {text}"
    return f"{type(exc).__name__}: {exc!r}"


def _is_retriable_status(status_code: Any) -> bool:
    if not isinstance(status_code, int):
        return False
    return status_code == 429 or 500 <= status_code <= 599



def _is_retriable_exception(exc: Exception) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return _is_retriable_status(exc.response.status_code)
    return False


def _extract_retry_after(headers: httpx.Headers) -> Optional[float]:
    raw = headers.get("Retry-After")
    if not raw:
        return None
    try:
        val = float(raw.strip())
        return min(max(val, 0.5), 5.0)
    except Exception:
        return None


async def http_get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    **kwargs
) -> httpx.Response:
    """
    Execute GET request with up to 4 attempts and exponential backoff.
    Retries on TimeoutException, NetworkError, 429, and 5xx status codes.
    Raises exception if all attempts fail or on non-retriable 4xx errors.
    """
    last_exc: Optional[Exception] = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            res = await client.get(url, **kwargs)
            res.raise_for_status()
            return res
        except Exception as exc:
            last_exc = exc
            if attempt >= MAX_ATTEMPTS or not _is_retriable_exception(exc):
                raise

            delay = BACKOFF_DELAYS[attempt - 1]
            if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
                custom_delay = _extract_retry_after(exc.response.headers)
                if custom_delay is not None:
                    delay = custom_delay

            logger.debug(
                f"HTTP GET {url} failed attempt {attempt}/{MAX_ATTEMPTS} with {describe_exception(exc)}. "
                f"Retrying in {delay:.2f}s..."
            )
            await asyncio.sleep(delay)

    if last_exc:
        raise last_exc
    raise RuntimeError(f"HTTP GET {url} failed without exception after {MAX_ATTEMPTS} attempts")
