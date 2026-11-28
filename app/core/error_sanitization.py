"""Shared credential detection and tracking error redaction."""

import re
from typing import Optional

PRICE_SENSITIVE_MARKERS = (
    "password", "auth", "credential", "dsn", "secret", "token", "key",
    "http:", "https:", "://", "@",
)


def contains_sensitive_price_error(message: str, *, include_cookies: bool = False) -> bool:
    markers = PRICE_SENSITIVE_MARKERS + (("cookie", "crumb") if include_cookies else ())
    return any(marker in message.lower() for marker in markers)


SECRET_PATTERNS = [
    re.compile(r"(postgres(?:ql)?|https?|http|ftp)://([^:]+):([^@]+)@", re.IGNORECASE),
    re.compile(r"(password|passwd|pwd|secret|token|api[_-]?key|credential|bearer|smtp_password)\s*[:=]\s*([^\s,;]+)", re.IGNORECASE),
    re.compile(r"Bearer\s+[a-zA-Z0-9_\-\.]+", re.IGNORECASE),
    re.compile(r"ghp_[a-zA-Z0-9]+", re.IGNORECASE),
    re.compile(r"ey[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}", re.IGNORECASE),
]


def sanitize_tracking_error(err: Optional[str]) -> Optional[str]:
    """Ensure error messages stored in telemetry and logs are free of credentials and DSN secrets."""
    if not err:
        return None
    lower = err.lower()
    if any(k in lower for k in ("postgres://", "postgresql://", "password=", "passwd=", "secret=", "token=", "smtp_password", "access_key", "secret_key")):
        if "postgresql://" in lower or "postgres://" in lower:
            return "Veritabanı veya servis erişim hatası."
    sanitized = err
    sanitized = re.sub(r"(postgres(?:ql)?|https?|http|ftp)://([^:]+):([^@]+)@", r"\1://\2:***MASKED***@", sanitized)
    for pattern in SECRET_PATTERNS[1:]:
        sanitized = pattern.sub("[REDACTED]", sanitized)
    return sanitized

