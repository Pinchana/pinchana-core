"""Plugin registry for runtime scraper discovery."""

from dataclasses import dataclass, field
from typing import Callable

from fastapi import APIRouter


@dataclass
class ScraperPlugin:
    """Metadata and router for a scraper module."""

    name: str
    router: APIRouter
    route_patterns: list[str] = field(default_factory=list)
    scrape_fn: Callable | None = None


class PluginRegistry:
    """Central registry where scraper modules register themselves at import time."""

    def __init__(self):
        self._plugins: dict[str, ScraperPlugin] = {}

    def register(self, plugin: ScraperPlugin) -> None:
        self._plugins[plugin.name] = plugin

    def get(self, name: str) -> ScraperPlugin | None:
        return self._plugins.get(name)

    def items(self):
        return self._plugins.items()

    def match_url(self, url: str) -> ScraperPlugin | None:
        url_lower = url.lower()
        for plugin in self._plugins.values():
            for pattern in plugin.route_patterns:
                if pattern.lower() in url_lower:
                    return plugin
        return None


# Global singleton — imported by both server and scraper modules.
registry = PluginRegistry()
