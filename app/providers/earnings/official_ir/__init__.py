from app.providers.earnings.official_ir.aksa import AksaIrProvider
from app.providers.earnings.official_ir.base import BaseOfficialIrProvider
from app.providers.earnings.official_ir.bimas import BimasIrProvider
from app.providers.earnings.official_ir.garan import GaranIrProvider
from app.providers.earnings.official_ir.halkb import HalkbIrProvider
from app.providers.earnings.official_ir.mavi import MaviIrProvider
from app.providers.earnings.official_ir.registry import (
    OfficialIrRegistry,
    official_ir_registry,
)
from app.providers.earnings.official_ir.sokm import SokmIrProvider
from app.providers.earnings.official_ir.thyao import ThyaoIrProvider

__all__ = [
    "BaseOfficialIrProvider",
    "BimasIrProvider",
    "GaranIrProvider",
    "HalkbIrProvider",
    "MaviIrProvider",
    "ThyaoIrProvider",
    "SokmIrProvider",
    "AksaIrProvider",
    "OfficialIrRegistry",
    "official_ir_registry",
]
