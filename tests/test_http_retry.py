from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.providers.calendar.http_retry import (
    BACKOFF_DELAYS,
    MAX_ATTEMPTS,
    describe_exception,
    http_get_with_retry,
)


def test_describe_exception_non_empty():
    exc1 = httpx.ConnectTimeout("connection timed out")
    assert describe_exception(exc1) == "ConnectTimeout: connection timed out"

    exc2 = Exception()
    assert describe_exception(exc2) == "Exception: Exception()"

    exc3 = ValueError("   ")
    assert describe_exception(exc3) == "ValueError: ValueError('   ')"


@pytest.mark.asyncio
async def test_retry_timeout_then_200():
    client = MagicMock(spec=httpx.AsyncClient)
    req = httpx.Request("GET", "https://example.com/data")
    resp_200 = httpx.Response(200, request=req, text="OK")

    client.get = AsyncMock(side_effect=[
        httpx.ConnectTimeout("timeout", request=req),
        resp_200
    ])

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        res = await http_get_with_retry(client, "https://example.com/data")
        assert res.status_code == 200
        assert client.get.call_count == 2
        assert mock_sleep.call_count == 1
        mock_sleep.assert_called_once_with(BACKOFF_DELAYS[0])


@pytest.mark.asyncio
async def test_retry_500_then_500_then_200():
    client = MagicMock(spec=httpx.AsyncClient)
    req = httpx.Request("GET", "https://example.com/data")
    resp_500_1 = httpx.Response(500, request=req, text="Server Error 1")
    resp_500_2 = httpx.Response(500, request=req, text="Server Error 2")
    resp_200 = httpx.Response(200, request=req, text="Success")

    client.get = AsyncMock(side_effect=[resp_500_1, resp_500_2, resp_200])

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        res = await http_get_with_retry(client, "https://example.com/data")
        assert res.status_code == 200
        assert client.get.call_count == 3
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(BACKOFF_DELAYS[0])
        mock_sleep.assert_any_call(BACKOFF_DELAYS[1])


@pytest.mark.asyncio
async def test_retry_429_with_retry_after():
    client = MagicMock(spec=httpx.AsyncClient)
    req = httpx.Request("GET", "https://example.com/data")
    resp_429 = httpx.Response(429, request=req, headers={"Retry-After": "3"}, text="Too Many Requests")
    resp_200 = httpx.Response(200, request=req, text="OK")

    client.get = AsyncMock(side_effect=[resp_429, resp_200])

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        res = await http_get_with_retry(client, "https://example.com/data")
        assert res.status_code == 200
        assert client.get.call_count == 2
        mock_sleep.assert_called_once_with(3.0)


@pytest.mark.asyncio
async def test_retry_503_max_attempts_fails():
    client = MagicMock(spec=httpx.AsyncClient)
    req = httpx.Request("GET", "https://example.com/data")
    resp_503 = httpx.Response(503, request=req, text="Service Unavailable")

    client.get = AsyncMock(return_value=resp_503)

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await http_get_with_retry(client, "https://example.com/data")
        assert exc_info.value.response.status_code == 503
        assert client.get.call_count == MAX_ATTEMPTS
        assert mock_sleep.call_count == MAX_ATTEMPTS - 1


@pytest.mark.asyncio
async def test_retry_404_no_retry():
    client = MagicMock(spec=httpx.AsyncClient)
    req = httpx.Request("GET", "https://example.com/data")
    resp_404 = httpx.Response(404, request=req, text="Not Found")

    client.get = AsyncMock(return_value=resp_404)

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await http_get_with_retry(client, "https://example.com/data")
        assert exc_info.value.response.status_code == 404
        assert client.get.call_count == 1
        mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_retry_timeout_max_attempts_fails_with_non_empty_description():
    client = MagicMock(spec=httpx.AsyncClient)
    req = httpx.Request("GET", "https://example.com/data")

    client.get = AsyncMock(side_effect=httpx.ReadTimeout("Read timed out", request=req))

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with pytest.raises(httpx.ReadTimeout) as exc_info:
            await http_get_with_retry(client, "https://example.com/data")
        assert describe_exception(exc_info.value) == "ReadTimeout: Read timed out"
        assert client.get.call_count == MAX_ATTEMPTS
        assert mock_sleep.call_count == MAX_ATTEMPTS - 1
