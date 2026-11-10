import asyncio
from pathlib import Path

from pinchana_core.music import MusicDownloader
from pinchana_core.storage import MediaStorage, set_cache_scope


def test_shared_downloader_isolates_concurrent_request_scopes(tmp_path):
    storage = MediaStorage(tmp_path)
    class Downloader(MusicDownloader):
        async def resolve(self, url):
            await asyncio.sleep(0)
            return url, {"id": "same-track", "title": "Track"}
        async def _ytdlp_download(self, target, post_dir):
            path = post_dir / "raw.audio"
            path.write_bytes(b"raw")
            await asyncio.sleep(0)
            return path
        async def _to_mp3(self, raw, output, **kwargs):
            output.write_bytes(str(output.parent).encode())
    # Construct before any request scope is active, like a module singleton.
    downloader = Downloader(lambda: storage.base_path)
    async def request(scope):
        set_cache_scope(scope)
        path, _ = await downloader.download("https://music.youtube.com/watch?v=abcdefghijk")
        await asyncio.sleep(0)
        destination = storage.audio_path("same-track")
        path.rename(destination)
        return destination
    async def run():
        return await asyncio.gather(request("a"*32), request("b"*32), request(None))
    try:
        first, second, persistent = asyncio.run(run())
        assert first == tmp_path / ".transient" / ("a"*32) / "same-track" / "audio.mp3"
        assert second == tmp_path / ".transient" / ("b"*32) / "same-track" / "audio.mp3"
        assert persistent == tmp_path / "same-track" / "audio.mp3"
        assert all(path.is_file() for path in (first, second, persistent))
        assert len({path.read_bytes() for path in (first, second, persistent)}) == 3
    finally:
        set_cache_scope(None)


def test_static_download_directory_remains_supported(tmp_path):
    assert MusicDownloader(tmp_path).base_dir == tmp_path
    assert MusicDownloader(str(tmp_path)).base_dir == tmp_path
