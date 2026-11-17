"""Local stand-in for books.toscrape.com, used by the benchmark and the tests.

Serves /catalogue/page-N.html with 20 products in the same markup as the real site,
so the real parser runs unchanged. FAIL_RATE injects 503s, LATENCY_MS adds a delay,
which lets the benchmark measure the pipeline instead of someone else's server.
"""
import asyncio
import os
import random

from aiohttp import web

PER_PAGE = 20
FAIL_RATE = float(os.getenv("FAIL_RATE", "0"))
LATENCY_MS = int(os.getenv("LATENCY_MS", "0"))
counts = {"requests": 0, "failed": 0}

POD = """<article class="product_pod">
  <h3><a href="../{slug}/index.html" title="Book {n}">Book {n}</a></h3>
  <div class="product_price">
    <p class="price_color">£{price:.2f}</p>
    <p class="instock availability"><i class="icon-ok"></i> In stock</p>
  </div>
  <p class="star-rating {rating}"></p>
</article>"""


def render_page(page: int) -> str:
    pods = []
    for i in range(PER_PAGE):
        n = (page - 1) * PER_PAGE + i + 1
        pods.append(POD.format(slug=f"book-{n}_{n}", n=n, price=10 + n % 50,
                               rating=["One", "Two", "Three", "Four", "Five"][n % 5]))
    return f"<html><body><ol class='row'>{''.join(pods)}</ol></body></html>"


async def page(request: web.Request) -> web.Response:
    if LATENCY_MS:
        await asyncio.sleep(LATENCY_MS / 1000)
    counts["requests"] += 1
    if random.random() < FAIL_RATE:
        counts["failed"] += 1
        return web.Response(status=503, text="try again")
    return web.Response(text=render_page(int(request.match_info["n"])), content_type="text/html")


async def stats(request: web.Request) -> web.Response:
    if request.method == "DELETE":
        counts.update(requests=0, failed=0)
    return web.json_response(counts)


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get(r"/catalogue/page-{n:\d+}.html", page)
    app.router.add_route("*", "/stats", stats)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), port=8000)
