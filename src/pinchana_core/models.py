from pydantic import BaseModel, HttpUrl, Field
from typing import Optional, List


class ScrapeRequest(BaseModel):
    """Client payload specifying the target URL."""
    url: HttpUrl = Field(..., description="The complete Instagram Post, Reel, or Carousel URL.")


class MediaItem(BaseModel):
    """A single item within a carousel."""
    index: int
    media_type: str
    thumbnail_url: str
    video_url: Optional[str] = None


class ScrapeResponse(BaseModel):
    """API response with locally stored media paths."""
    shortcode: str
    caption: str
    author: str
    media_type: str
    thumbnail_url: str
    video_url: Optional[str] = None
    audio_url: Optional[str] = None
    carousel: Optional[List[MediaItem]] = None
