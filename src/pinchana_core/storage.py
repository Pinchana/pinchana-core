import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable, Optional, Dict, TypeVar
import httpx

logger = logging.getLogger(__name__)
T = TypeVar("T")


class MediaStorage:
    """Persistent on-disk storage for scraped media with size-based eviction."""

    def __init__(self, base_path: str = "./cache", max_size_gb: float = 10.0):
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
        self.max_size_bytes = int(max_size_gb * 1024 * 1024 * 1024)
        self._http_client: httpx.AsyncClient | None = None
        self._http_client_lock = asyncio.Lock()
        self._download_limit = asyncio.Semaphore(
            max(1, int(os.getenv("MEDIA_DOWNLOAD_CONCURRENCY", "4")))
        )
        self._inflight: dict[str, asyncio.Task] = {}
        self._inflight_lock = asyncio.Lock()

    async def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            async with self._http_client_lock:
                if self._http_client is None:
                    self._http_client = httpx.AsyncClient(
                        timeout=httpx.Timeout(120.0, connect=15.0),
                        follow_redirects=True,
                        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                    )
        return self._http_client

    async def close(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def singleflight(self, key: str, operation: Callable[[], Awaitable[T]]) -> T:
        """Run at most one operation for a cache key and share its result."""
        async with self._inflight_lock:
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(operation())
                self._inflight[key] = task
                task.add_done_callback(
                    lambda completed: asyncio.create_task(
                        self._remove_inflight(key, completed)
                    )
                )

        return await asyncio.shield(task)

    async def _remove_inflight(self, key: str, task: asyncio.Task) -> None:
        async with self._inflight_lock:
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)

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
        target = self._metadata_path(shortcode)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        with open(temporary, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        temporary.replace(target)

    def prepare_post_dir(self, shortcode: str):
        post_dir = self._post_dir(shortcode)
        if post_dir.exists() and not self._metadata_path(shortcode).exists():
            shutil.rmtree(post_dir)
        post_dir.mkdir(parents=True, exist_ok=True)

    async def download(self, url: str, dest: Path) -> bool:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "*/*",
        }
        if "instagram.com" in url:
            headers["Referer"] = "https://www.instagram.com/"

        temporary = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.part")
        started = time.perf_counter()
        try:
            async with self._download_limit:
                client = await self._client()
                async with client.stream("GET", url, headers=headers) as resp:
                    resp.raise_for_status()
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with open(temporary, "wb") as f:
                        async for chunk in resp.aiter_bytes(128 * 1024):
                            f.write(chunk)
                        f.flush()
                        os.fsync(f.fileno())
                    temporary.replace(dest)
            logger.info(
                "media_download_complete path=%s bytes=%d elapsed_ms=%.1f",
                dest,
                dest.stat().st_size,
                (time.perf_counter() - started) * 1000,
            )
            return True
        except Exception as e:
            logger.error("Failed to download %s: %s", url, e)
            temporary.unlink(missing_ok=True)
            return False

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
