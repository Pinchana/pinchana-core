import asyncio

from pinchana_core.models import ScrapeRequest
from pinchana_core.storage import MediaStorage, set_cache_scope


def test_fastapi_scraper_request_activates_scope(tmp_path):
    import httpx
    from fastapi import FastAPI

    app = FastAPI()
    storage = MediaStorage(str(tmp_path))

    @app.post("/scrape")
    async def scrape(request: ScrapeRequest):
        return {"cache_path": str(storage.base_path), "url": str(request.url)}

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            transient = await client.post("/scrape", json={"url": "https://x.com/p/status/42", "cache_scope": "d" * 32})
            normal = await client.post("/scrape", json={"url": "https://x.com/p/status/42"})
        assert transient.status_code == 200
        assert transient.json()["cache_path"].endswith("/.transient/" + "d" * 32)
        assert normal.json()["cache_path"] == str(tmp_path)

    try:
        asyncio.run(run())
    finally:
        set_cache_scope(None)


def test_transient_requests_never_use_persistent_post_cache(tmp_path):
    storage = MediaStorage(str(tmp_path))
    try:
        ScrapeRequest(url="https://x.com/person/status/42")
        storage.save_metadata("42", {"origin": "public"})
        assert storage.load_metadata("42") == {"origin": "public"}

        ScrapeRequest(url="https://x.com/person/status/42", cache_scope="a" * 32)
        assert storage.load_metadata("42") is None
        storage.save_metadata("42", {"origin": "bot"})
        assert storage.load_metadata("42") == {"origin": "bot"}

        ScrapeRequest(url="https://x.com/person/status/42")
        assert storage.load_metadata("42") == {"origin": "public"}
        assert (tmp_path / ".transient" / ("a" * 32) / "42" / "metadata.json").is_file()
    finally:
        set_cache_scope(None)


def test_scoped_singleflight_does_not_join_persistent_request(tmp_path):
    storage = MediaStorage(str(tmp_path))

    async def task(scope, result):
        set_cache_scope(scope)
        return await storage.singleflight("same-post", lambda: asyncio.sleep(0.01, result=result))

    async def run():
        return await asyncio.gather(task(None, "persistent"), task("b" * 32, "bot"))

    try:
        first, second = asyncio.run(run())
        assert (first, second) == ("persistent", "bot")
    finally:
        set_cache_scope(None)
