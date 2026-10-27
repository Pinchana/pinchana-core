from pydantic import BaseModel, HttpUrl, Field, model_validator
from typing import Optional, List

from .storage import set_cache_scope


class ScrapeRequest(BaseModel):
    """Client payload specifying the target URL."""
    url: HttpUrl = Field(..., description="The complete Instagram Post, Reel, or Carousel URL.")
    cache_scope: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")

    @model_validator(mode="after")
    def activate_cache_scope(self):
        # FastAPI validates the request in its own async task before invoking
        # scraper code. Scope must never bleed into the next scrape request.
        set_cache_scope(self.cache_scope)
        return self


class MediaItem(BaseModel):
    """A single item within a carousel."""
    index: int
    media_type: str
    thumbnail_url: str
    video_url: Optional[str] = None
    looping: bool = False


class TrackItem(BaseModel):
    """A single track within an album/playlist result."""
    index: int
    title: str
    artist: str
    audio_url: str


class ScrapeResponse(BaseModel):
    """API response with locally stored media paths."""
    shortcode: str
    caption: str
    author: str
    media_type: str
    thumbnail_url: str
    video_url: Optional[str] = None
    audio_url: Optional[str] = None
    cover_url: Optional[str] = None
    duration: Optional[int] = None
    title: Optional[str] = None
    album: Optional[str] = None
    carousel: Optional[List[MediaItem]] = None
    tracklist: Optional[List[TrackItem]] = None
