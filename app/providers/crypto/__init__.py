"""Crypto provider exports without importing unrelated provider stacks eagerly."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.providers.crypto.binance_price_provider import (
        BinancePriceFetchError,
        BinancePriceProvider,
    )

__all__ = [
    "BinancePriceProvider",
    "BinancePriceFetchError",
]


def __getattr__(name: str) -> Any:
    """Load price providers only when their public export is requested."""
    if name in {"BinancePriceProvider", "BinancePriceFetchError"}:
        from app.providers.crypto.binance_price_provider import (
            BinancePriceFetchError,
            BinancePriceProvider,
        )

        return {
            "BinancePriceProvider": BinancePriceProvider,
            "BinancePriceFetchError": BinancePriceFetchError,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
