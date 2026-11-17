"""Scraper interface and the bundled example target.

A scraper turns one job into a list of records:  fetch(job, session) -> records.
The worker owns the queue, retries at the job level, proxies and storage;
a scraper only knows how to read one page of one site.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class FetchError(Exception):
    """The page could not be fetched, even after retries."""


class ParseError(Exception):
    """The page was fetched but did not look like what the scraper expects."""


@dataclass
class Record:
    external_id: str
    title: str
    url: str
    data: dict[str, Any] = field(default_factory=dict)


class Scraper(Protocol):
    name: str

    async def fetch(self, job: dict[str, Any], session: aiohttp.ClientSession) -> list[Record]: ...


async def get_text(
    session: aiohttp.ClientSession,
    url: str,
    attempts: int = 3,
    backoff: float = 0.5,
    timeout: float = 30,
) -> str:
    """GET a page, retrying 429/5xx and connection errors with exponential backoff.

    Other 4xx responses are not retried: asking again will not change the answer.
    """
    last_error = ""
    for attempt in range(attempts):
        if attempt:
            await asyncio.sleep(backoff * 2 ** (attempt - 1))
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status == 200:
                    return await resp.text()
                if resp.status not in RETRYABLE_STATUS:
                    raise FetchError(f"HTTP {resp.status} for {url}")
                last_error = f"HTTP {resp.status}"
        except (TimeoutError, aiohttp.ClientConnectionError) as exc:
            last_error = type(exc).__name__
    raise FetchError(f"{url}: gave up after {attempts} attempts ({last_error})")


RATINGS = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}


class BooksToScrape:
    """books.toscrape.com, a sandbox site published for scraping practice.

    A job is one catalogue listing page; each page holds 20 books.
    """

    name = "books"

    async def fetch(self, job: dict[str, Any], session: aiohttp.ClientSession) -> list[Record]:
        return parse_books(await get_text(session, job["url"]), job["url"])


def parse_books(html: str, page_url: str) -> list[Record]:
    soup = BeautifulSoup(html, "html.parser")
    records = []
    for pod in soup.select("article.product_pod"):
        link = pod.select_one("h3 a")
        price = pod.select_one("p.price_color")
        if not link or not price:
            raise ParseError(f"unexpected product markup on {page_url}")
        href = urljoin(page_url, link["href"])
        rating = next((RATINGS[c] for c in pod.select_one("p.star-rating")["class"] if c in RATINGS), None)
        records.append(Record(
            # /catalogue/a-light-in-the-attic_1000/index.html -> a-light-in-the-attic_1000
            external_id=href.rstrip("/").split("/")[-2],
            title=link["title"],
            url=href,
            data={
                "price": price.get_text(strip=True).lstrip("Â"),
                "in_stock": "In stock" in pod.select_one("p.availability").get_text(),
                "rating": rating,
            },
        ))
    if not records:
        raise ParseError(f"no products found on {page_url}")
    return records


SCRAPERS: dict[str, Scraper] = {s.name: s for s in [BooksToScrape()]}
