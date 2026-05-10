"""scraper-core — shared components for the modular scraper platform."""

from .docker_manager import ModuleContainerManager
from .models import MediaItem, ScrapeRequest, ScrapeResponse
from .plugins import PluginRegistry, ScraperPlugin, registry
from .storage import MediaStorage
from .vpn import GluetunController, VpnRotationError

__all__ = [
    "MediaItem",
    "ScrapeRequest",
    "ScrapeResponse",
    "MediaStorage",
    "GluetunController",
    "VpnRotationError",
    "PluginRegistry",
    "ScraperPlugin",
    "registry",
    "ModuleContainerManager",
]
