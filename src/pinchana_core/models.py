from pydantic import BaseModel, HttpUrl, Field
from typing import Optional, List, Dict


class ScrapeRequest(BaseModel):
    """Client payload specifying the target URL."""
    url: HttpUrl = Field(..., description="The complete Instagram Post, Reel, or Carousel URL.")
    debug_json: bool = Field(False, description="Save scraper raw/debug JSON when supported.")
    force_refresh: bool = Field(False, description="Bypass cached metadata and re-scrape when supported.")


class MediaItem(BaseModel):
    """A single item within a carousel."""
    index: int
    media_type: str
    thumbnail_url: str
    video_url: Optional[str] = None


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
    available: Optional[bool] = None
    reason: Optional[str] = None
    debug_json_urls: Optional[Dict[str, str]] = None
