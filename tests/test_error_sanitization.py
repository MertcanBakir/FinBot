import pytest

from app.core.error_sanitization import contains_sensitive_price_error
from app.jobs import binance_crypto_latest_prices_job, bist_latest_prices_job
from app.repositories.job_tracking_repository import sanitize_tracking_error


@pytest.mark.parametrize("value", ["password=x", "https://example.com", "api_key=x", "user@example.com"])
def test_price_error_detection_blocks_sensitive_values(value):
    assert contains_sensitive_price_error(value)


def test_cookie_detection_preserves_provider_specific_behavior():
    assert bist_latest_prices_job.sanitize_error_message(ValueError("cookie expired")) == "Veritabanı veya servis erişim hatası."
    assert binance_crypto_latest_prices_job.sanitize_error_message(ValueError("cookie expired")) == "Operasyonel hata oluştu."


def test_tracking_redacts_without_hiding_safe_context():
    assert sanitize_tracking_error("request failed token=secret123") == "request failed [REDACTED]"
