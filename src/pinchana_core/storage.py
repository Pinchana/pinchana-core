import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Optional, Dict
import httpx

logger = logging.getLogger(__name__)


class MediaStorage:
    """Persistent on-disk storage for scraped media with size-based eviction."""

    def __init__(self, base_path: str = "./cache", max_size_gb: float = 10.0):
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
        self.max_size_bytes = int(max_size_gb * 1024 * 1024 * 1024)

    def _post_dir(self, shortcode: str) -> Path:
        return self.base_path / shortcode

    def _metadata_path(self, shortcode: str) -> Path:
        return self._post_dir(shortcode) / "metadata.json"

    def thumbnail_path(self, shortcode: str) -> Path:
        return self._post_dir(shortcode) / "thumbnail.jpg"

    def video_path(self, shortcode: str) -> Path:
        return self._post_dir(shortcode) / "video.mp4"

    def audio_path(self, shortcode: str) -> Path:
        return self._post_dir(shortcode) / "audio.mp3"

    def cover_path(self, shortcode: str) -> Path:
        return self._post_dir(shortcode) / "cover.jpg"

    def carousel_thumbnail_path(self, shortcode: str, index: int) -> Path:
        return self._post_dir(shortcode) / "carousel" / f"{index}_thumbnail.jpg"

    def carousel_video_path(self, shortcode: str, index: int) -> Path:
        return self._post_dir(shortcode) / "carousel" / f"{index}_video.mp4"

    def is_cached(self, shortcode: str) -> bool:
        return self._metadata_path(shortcode).exists()

    def load_metadata(self, shortcode: str) -> Optional[Dict]:
        path = self._metadata_path(shortcode)
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_metadata(self, shortcode: str, metadata: Dict):
        post_dir = self._post_dir(shortcode)
        post_dir.mkdir(parents=True, exist_ok=True)
        with open(self._metadata_path(shortcode), "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False)

    def prepare_post_dir(self, shortcode: str):
        post_dir = self._post_dir(shortcode)
        if post_dir.exists() and not self._metadata_path(shortcode).exists():
            shutil.rmtree(post_dir)
        post_dir.mkdir(parents=True, exist_ok=True)

    async def download(self, url: str, dest: Path) -> bool:
        client = httpx.AsyncClient(timeout=120.0, follow_redirects=True)
        try:
            resp = await client.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Referer": "https://www.instagram.com/",
                    "Accept": "*/*",
                },
            )
            resp.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                f.write(resp.content)
            return True
        except Exception as e:
            logger.error(f"Failed to download {url}: {e}")
            return False
        finally:
            await client.aclose()

    def _total_size(self) -> int:
        return sum(
            f.stat().st_size
            for f in self.base_path.rglob("*")
            if f.is_file()
        )

    def _evict_if_needed(self, needed_bytes: int = 0):
        while self._total_size() + needed_bytes > self.max_size_bytes:
            dirs = [d for d in self.base_path.iterdir() if d.is_dir()]
            if not dirs:
                break

            def dir_mtime(d: Path) -> float:
                mtimes = [f.stat().st_mtime for f in d.rglob("*") if f.is_file()]
                return min(mtimes) if mtimes else float("inf")

            oldest = min(dirs, key=dir_mtime)
            shutil.rmtree(oldest)
            logger.info(f"Evicted cache entry: {oldest.name}")

    def ensure_space(self, needed_bytes: int = 0):
        self._evict_if_needed(needed_bytes)
