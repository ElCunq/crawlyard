import aiohttp
import pytest
from aiohttp import web

from scrapers import FetchError, get_text


async def flaky_server(aiohttp_server, responses):
    """Serve the given (status, body) pairs in order, one per request."""
    calls = []

    async def handler(request):
        status, body = responses[len(calls)]
        calls.append(status)
        return web.Response(status=status, text=body)

    app = web.Application()
    app.router.add_get("/", handler)
    return await aiohttp_server(app), calls


async def test_retries_5xx_then_succeeds(aiohttp_server):
    server, calls = await flaky_server(aiohttp_server, [(503, ""), (429, ""), (200, "ok")])
    async with aiohttp.ClientSession() as session:
        assert await get_text(session, str(server.make_url("/")), attempts=3, backoff=0) == "ok"
    assert calls == [503, 429, 200]


async def test_gives_up_after_max_attempts(aiohttp_server):
    server, calls = await flaky_server(aiohttp_server, [(500, "")] * 3)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(FetchError, match="gave up after 3 attempts"):
            await get_text(session, str(server.make_url("/")), attempts=3, backoff=0)
    assert len(calls) == 3


async def test_does_not_retry_404(aiohttp_server):
    server, calls = await flaky_server(aiohttp_server, [(404, ""), (200, "ok")])
    async with aiohttp.ClientSession() as session:
        with pytest.raises(FetchError, match="HTTP 404"):
            await get_text(session, str(server.make_url("/")), attempts=3, backoff=0)
    assert calls == [404]


async def test_retries_connection_errors(unused_tcp_port):
    # Nothing listens on this port, so every attempt is refused.
    async with aiohttp.ClientSession() as session:
        with pytest.raises(FetchError, match="ClientConnectorError"):
            await get_text(session, f"http://127.0.0.1:{unused_tcp_port}/", attempts=2, backoff=0)
