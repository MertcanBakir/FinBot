import asyncio
import logging
from typing import List, Optional, Type

import httpx

from app.models.earnings import OfficialIrProviderResult
from app.providers.earnings.official_ir.aksa import AksaIrProvider
from app.providers.earnings.official_ir.base import (
    DEFAULT_HEADERS,
    BaseOfficialIrProvider,
)
from app.providers.earnings.official_ir.bimas import BimasIrProvider
from app.providers.earnings.official_ir.halkb import HalkbIrProvider
from app.providers.earnings.official_ir.mavi import MaviIrProvider
from app.providers.earnings.official_ir.sokm import SokmIrProvider
from app.providers.earnings.official_ir.thyao import ThyaoIrProvider

logger = logging.getLogger(__name__)


class OfficialIrRegistry:
    """Registry managing and orchestrating the enabled official BIST100 IR providers."""

    # GARAN is temporarily excluded: its calendar is no longer in the server-rendered HTML.
    DEFAULT_PROVIDER_CLASSES: List[Type[BaseOfficialIrProvider]] = [
        BimasIrProvider,
        HalkbIrProvider,
        MaviIrProvider,
        ThyaoIrProvider,
        SokmIrProvider,
        AksaIrProvider,
    ]

    def __init__(self, providers: Optional[List[BaseOfficialIrProvider]] = None):
        if providers is not None:
            self.providers = providers
        else:
            self.providers = [cls() for cls in self.DEFAULT_PROVIDER_CLASSES]

    def get_all_providers(self) -> List[BaseOfficialIrProvider]:
        """Return list of registered provider instances."""
        return self.providers

    async def fetch_all(
        self,
        client: Optional[httpx.AsyncClient] = None,
        max_concurrency: int = 4
    ) -> List[OfficialIrProviderResult]:
        """Fetch all official IR calendars concurrently with bounded concurrency."""
        sem = asyncio.Semaphore(max_concurrency)
        close_client = False

        if client is None:
            client = httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=DEFAULT_HEADERS)
            close_client = True

        async def _fetch_one(provider: BaseOfficialIrProvider) -> OfficialIrProviderResult:
            async with sem:
                try:
                    return await provider.fetch_calendar(client=client)
                except Exception as e:
                    logger.error(f"Error executing provider {provider.symbol}: {e}", exc_info=True)
                    return OfficialIrProviderResult(
                        symbol=provider.symbol,
                        source_provider=provider.source_provider,
                        status="failed",
                        parser_failed=True,
                        warnings=[f"Unhandled exception during fetch: {str(e)}"]
                    )

        try:
            tasks = [_fetch_one(p) for p in self.providers]
            results = await asyncio.gather(*tasks)
            return list(results)
        finally:
            if close_client:
                await client.aclose()


official_ir_registry = OfficialIrRegistry()
