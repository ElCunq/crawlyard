"""Queue consumer: pops jobs from Redis, runs the matching scraper, upserts records into Postgres."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import aiohttp
import asyncpg
import redis.asyncio as redis
from aiohttp_socks import ProxyConnectionError, ProxyConnector, ProxyError

from scrapers import SCRAPERS, FetchError, ParseError, Record

REDIS_URL = os.environ["REDIS_URL"]
DB_URL = os.environ["DATABASE_URL"]
QUEUE = os.getenv("REDIS_QUEUE_NAME", "scrape_queue")
DEAD_QUEUE = "dead_jobs"
DEFAULT_SCRAPER = os.getenv("DEFAULT_SCRAPER", "books")
DELAY_MS = int(os.getenv("WORKER_DELAY_MS", "1000"))
MAX_ATTEMPTS = int(os.getenv("MAX_JOB_ATTEMPTS", "3"))
USE_PROXY = os.getenv("USE_PROXY", "false").lower() == "true"
HEADERS = {"User-Agent": "crawlyard/1.0 (+https://github.com/ElCunq/crawlyard)"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", force=True)
log = logging.getLogger("worker")

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title       TEXT,
    url         TEXT,
    data        JSONB NOT NULL DEFAULT '{}',
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, external_id)
);
"""

UPSERT = """
INSERT INTO items (source, external_id, title, url, data)
VALUES ($1, $2, $3, $4, $5::jsonb)
ON CONFLICT (source, external_id) DO UPDATE
SET title = EXCLUDED.title, url = EXCLUDED.url, data = EXCLUDED.data, last_seen = now()
"""

# Errors that mean "this job failed, try it again later" rather than "the worker is broken".
JOB_ERRORS = (FetchError, ParseError, aiohttp.ClientError, asyncio.TimeoutError, asyncpg.PostgresError)
PROXY_ERRORS = (ProxyError, ProxyConnectionError, aiohttp.ClientConnectionError)


def parse_job(raw: str) -> dict[str, Any]:
    """Jobs are JSON objects; a bare URL is accepted as shorthand for the default scraper."""
    job = json.loads(raw) if raw.lstrip().startswith("{") else {"url": raw.strip()}
    job.setdefault("scraper", DEFAULT_SCRAPER)
    job.setdefault("attempts", 0)
    return job


async def save_records(pool: asyncpg.Pool, source: str, records: list[Record]) -> None:
    async with pool.acquire() as conn:
        await conn.executemany(UPSERT, [(source, r.external_id, r.title, r.url, json.dumps(r.data)) for r in records])


async def run_job(job: dict[str, Any], session: aiohttp.ClientSession, pool: asyncpg.Pool) -> int:
    scraper = SCRAPERS.get(job["scraper"])
    if scraper is None:
        raise ParseError(f"unknown scraper {job['scraper']!r}")
    records = await scraper.fetch(job, session)
    await save_records(pool, job.get("source", scraper.name), records)
    return len(records)


async def next_proxy(r: redis.Redis) -> str | None:
    # Rotate: take from the tail, put it back at the head.
    return await r.lmove("proxy_queue", "proxy_queue", "RIGHT", "LEFT")


async def handle(raw: str, r: redis.Redis, pool: asyncpg.Pool, shared: aiohttp.ClientSession | None) -> None:
    job = parse_job(raw)
    proxy = await next_proxy(r) if USE_PROXY else None
    connector = ProxyConnector.from_url(proxy) if proxy else None
    session = shared or aiohttp.ClientSession(connector=connector, headers=HEADERS)
    try:
        count = await run_job(job, session, pool)
    except JOB_ERRORS as exc:
        job["attempts"] += 1
        await r.hincrby("stats", "job_errors", 1)
        if proxy and isinstance(exc, PROXY_ERRORS):
            await r.sadd("banned_proxies", proxy)
            await r.lrem("proxy_queue", 0, proxy)
        if job["attempts"] >= MAX_ATTEMPTS:
            log.error("dead after %d attempts: %s (%s)", job["attempts"], job.get("url"), exc)
            await r.lpush(DEAD_QUEUE, json.dumps(job))
            await r.hincrby("stats", "jobs_dead", 1)
        else:
            log.warning("attempt %d failed, requeued: %s (%s)", job["attempts"], job.get("url"), exc)
            await r.lpush(QUEUE, json.dumps(job))
        return
    finally:
        if session is not shared:
            await session.close()
    if proxy:
        await r.sadd("good_proxies", proxy)
    await r.hincrby("stats", "jobs_ok", 1)
    await r.hincrby("stats", "items", count)
    log.info("ok %d records: %s", count, job.get("url"))


async def main() -> None:
    pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=2)
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA)
    r = redis.from_url(REDIS_URL, decode_responses=True)
    shared = None if USE_PROXY else aiohttp.ClientSession(headers=HEADERS)
    log.info("waiting for jobs on %s (proxy=%s, delay=%dms)", QUEUE, USE_PROXY, DELAY_MS)
    try:
        while True:
            # ponytail: a job popped here is lost if the process dies mid-job.
            # Upgrade path: BLMOVE into a per-worker processing list and LREM on completion.
            _, raw = await r.brpop(QUEUE, timeout=0)
            await handle(raw, r, pool, shared)
            await asyncio.sleep(DELAY_MS / 1000)
    finally:
        if shared:
            await shared.close()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
