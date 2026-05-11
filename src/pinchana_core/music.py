"""Shared music downloader base for Pinchana music modules.

All music modules (Deezer, SoundCloud, Spotify, YTMusic) share the same pipeline:
1. Resolve the input URL to a downloadable source (yt-dlp or API search)
2. Download best audio via yt-dlp with multi-strategy fallback
3. Convert to MP3 320kbps with ffmpeg, embedding metadata + cover art
4. Return the file path + metadata
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Optional

import aiohttp
from PIL import Image
from yt_dlp import YoutubeDL

logger = logging.getLogger(__name__)


class MusicDownloadError(Exception):
    """Raised when a music download fails after all retries."""
    pass


class MusicDownloader:
    """Base downloader for audio extraction via yt-dlp + ffmpeg MP3 conversion.

    Subclasses must implement:
        - `resolve(url: str)` -> tuple[download_url_or_id, metadata_dict]
    """

    # yt-dlp format: best audio-only stream, fallback to best combined
    YTDLP_FORMAT = "ba/b"

    # yt-dlp default clients (2026.03+) are already tuned by maintainers:
    # tv, ios, web_safari, web_creator, android_vr
    # We do NOT override player_client — we just provide cookies when available.
    # Strategies ordered by reliability for audio extraction:
    YTDLP_STRATEGIES = [
        {"name": "default_cookies", "override_client": False, "cookies": True},
        {"name": "default_nocookies", "override_client": False, "cookies": False},
        {"name": "tv_cookies", "override_client": True, "client": ["tv"], "cookies": True},
        {"name": "android_vr", "override_client": True, "client": ["android_vr"], "cookies": False},
    ]

    def __init__(self, base_dir: str | Path, proxy: str | None = None):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.proxy = proxy
        self.cookies_path = self._find_cookies()

    # ------------------------------------------------------------------
    # Subclass hooks
    # ------------------------------------------------------------------
    async def resolve(self, url: str) -> tuple[str, dict]:
        """Resolve a platform URL to a direct yt-dlp download URL/ID.

        Returns:
            (download_target, metadata)
            metadata keys: title, artist, album, duration, cover_url
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Shared pipeline
    # ------------------------------------------------------------------
    async def download(self, url: str) -> tuple[Path, dict]:
        """Full pipeline: resolve → yt-dlp download → MP3 conversion.

        Returns:
            (mp3_path, metadata_dict)
        """
        target, meta = await self.resolve(url)
        if not target:
            raise MusicDownloadError(f"Could not resolve URL: {url}")

        post_id = meta.get("id") or self._slugify(meta.get("title", "track"))
        post_dir = self.base_dir / post_id
        post_dir.mkdir(parents=True, exist_ok=True)

        # 1. Download raw audio
        raw_audio = await self._ytdlp_download(target, post_dir)
        if not raw_audio:
            raise MusicDownloadError(f"yt-dlp failed for {target}")

        # 2. Download cover art
        cover_path = None
        cover_url = meta.get("cover_url")
        if cover_url:
            cover_path = post_dir / "cover.jpg"
            await self._download_cover(cover_url, cover_path)

        # 3. Convert to MP3 320kbps
        mp3_path = post_dir / f"{post_id}.mp3"
        await self._to_mp3(
            raw_audio,
            mp3_path,
            title=meta.get("title"),
            artist=meta.get("artist"),
            album=meta.get("album"),
            cover_path=cover_path,
        )

        # 4. Cleanup raw audio
        if raw_audio.exists():
            raw_audio.unlink(missing_ok=True)

        if not mp3_path.exists() or mp3_path.stat().st_size == 0:
            raise MusicDownloadError("MP3 conversion produced empty file")

        logger.info("Music ready: %s (%s bytes)", mp3_path, mp3_path.stat().st_size)
        return mp3_path, meta

    # ------------------------------------------------------------------
    # yt-dlp download with strategy fallback
    # ------------------------------------------------------------------
    async def _ytdlp_download(self, target: str, post_dir: Path) -> Path | None:
        """Download best audio via yt-dlp with multi-strategy fallback."""
        loop = asyncio.get_running_loop()
        outtmpl = str(post_dir / "raw.%(ext)s")

        for strategy in self.YTDLP_STRATEGIES:
            logger.info("Trying yt-dlp strategy: %s", strategy["name"])

            opts: dict = {
                "format": self.YTDLP_FORMAT,
                "outtmpl": outtmpl,
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "overwrites": True,
                "prefer_ffmpeg": True,
                "retries": 2,
                "fragment_retries": 2,
                "format_sort": ["quality", "br", "asr", "size"],
                "format_sort_force": True,
            }

            if strategy.get("override_client") and strategy.get("client"):
                opts["extractor_args"] = {
                    "youtube": {"player_client": strategy["client"]}
                }

            if strategy.get("cookies") and self.cookies_path:
                opts["cookiefile"] = str(self.cookies_path)

            if self.proxy:
                opts["proxy"] = self.proxy

            try:
                info = await loop.run_in_executor(
                    None, lambda: self._run_ytdlp(target, opts)
                )
                if info:
                    # Find downloaded file
                    for ext in (".m4a", ".mp4", ".webm", ".opus", ".ogg", ".flac", ".wav", ".aac"):
                        candidate = post_dir / f"raw{ext}"
                        if candidate.exists() and candidate.stat().st_size > 0:
                            return candidate
            except Exception as e:
                logger.warning("Strategy %s failed: %s", strategy["name"], e)
                continue

        return None

    @staticmethod
    def _run_ytdlp(target: str, opts: dict) -> dict | None:
        with YoutubeDL(opts) as ydl:
            return ydl.sanitize_info(ydl.extract_info(target, download=True))

    # ------------------------------------------------------------------
    # ffmpeg MP3 conversion with metadata + cover
    # ------------------------------------------------------------------
    async def _to_mp3(
        self,
        input_path: Path,
        output_path: Path,
        title: str | None,
        artist: str | None,
        album: str | None,
        cover_path: Path | None,
    ) -> None:
        """Convert raw audio to 320kbps MP3, embed metadata and cover art."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: self._run_ffmpeg(input_path, output_path, title, artist, album, cover_path),
        )

    @staticmethod
    def _run_ffmpeg(
        input_path: Path,
        output_path: Path,
        title: str | None,
        artist: str | None,
        album: str | None,
        cover_path: Path | None,
    ) -> None:
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(input_path),
        ]

        inputs = 1
        if cover_path and cover_path.exists():
            cmd += ["-i", str(cover_path)]
            inputs += 1

        cmd += [
            "-map", "0:a:0",
            "-c:a", "libmp3lame", "-b:a", "320k",
            "-id3v2_version", "3",
        ]

        if inputs > 1:
            cmd += [
                "-map", "1:v:0",
                "-c:v", "mjpeg", "-disposition:v", "attached_pic",
            ]

        cmd += ["-metadata", f"title={title or ''}"]
        cmd += ["-metadata", f"artist={artist or ''}"]
        if album:
            cmd += ["-metadata", f"album={album}"]

        cmd += [str(output_path)]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error("ffmpeg failed: %s", result.stderr)
            raise MusicDownloadError(f"ffmpeg failed: {result.stderr}")

    # ------------------------------------------------------------------
    # Cover art download + process
    # ------------------------------------------------------------------
    async def _download_cover(self, url: str, dest: Path) -> bool:
        """Download cover image, resize to 320x320, compress to <200KB."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    resp.raise_for_status()
                    data = await resp.read()

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, lambda: self._process_cover_image(data, dest)
            )
            return True
        except Exception as e:
            logger.warning("Cover download failed: %s", e)
            return False

    @staticmethod
    def _process_cover_image(data: bytes, dest: Path) -> None:
        from io import BytesIO
        img = Image.open(BytesIO(data))
        img.thumbnail((320, 320), Image.Resampling.LANCZOS)
        quality = 85
        img.save(dest, "jpeg", quality=quality)
        while dest.stat().st_size > 200 * 1024 and quality > 10:
            quality -= 5
            img.save(dest, "jpeg", quality=max(quality, 10))

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------
    def _find_cookies(self) -> Path | None:
        """Look for a cookies.txt in known locations."""
        candidates = [
            Path(os.getenv("YTDLP_COOKIE_FILE", "")),
            Path(os.getenv("YTDLP_COOKIES_DIR", "/run/pinchana-cookies")) / "cookies.txt",
            Path(os.getenv("YTDLP_COOKIES_DIR", "/run/pinchana-cookies")) / "youtube.com_cookies.txt",
        ]
        for p in candidates:
            if p.exists() and p.is_file():
                return p
        # fallback: any .txt in cookies dir
        cookies_dir = Path(os.getenv("YTDLP_COOKIES_DIR", "/run/pinchana-cookies"))
        if cookies_dir.exists():
            txts = sorted(cookies_dir.glob("*.txt"))
            if txts:
                return txts[0]
        return None

    @staticmethod
    def _slugify(text: str) -> str:
        text = re.sub(r"[^\w\s-]", "", text.lower())
        text = re.sub(r"[-\s]+", "-", text).strip("-")
        return text or f"track-{uuid.uuid4().hex[:8]}"
