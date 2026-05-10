"""Base scraper protocol for type consistency across scraper services."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class BaseScraper(Protocol):
    """Protocol that all scraper implementations should satisfy.

    In the modular architecture, each scraper runs as a standalone
    FastAPI service, so this protocol is primarily for documentation
    and in-process usage.
    """

    async def scrape(self, url: str) -> dict:
        """Extract metadata and media URLs for the given post URL.

        Returns a normalized dict matching the ScrapeResponse shape.
        """
        ...
