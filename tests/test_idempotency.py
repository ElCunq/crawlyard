"""Processing the same job twice must not create duplicate rows. Needs a real Postgres (TEST_DATABASE_URL)."""
import os
from pathlib import Path

import aiohttp
import asyncpg
import pytest

from conftest import load_module
from fixture_site import make_app

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")

worker = load_module("worker_main", Path(__file__).parent.parent / "worker" / "main.py")


async def test_same_job_twice_keeps_row_count(aiohttp_server):
    site = await aiohttp_server(make_app())
    job = worker.parse_job(str(site.make_url("/catalogue/page-3.html")))

    pool = await asyncpg.create_pool(os.environ["TEST_DATABASE_URL"], min_size=1, max_size=1)
    try:
        async with pool.acquire() as conn:
            await conn.execute(worker.SCHEMA)
            await conn.execute("DELETE FROM items WHERE source = 'books'")
        async with aiohttp.ClientSession() as session:
            first = await worker.run_job(job, session, pool)
            count_after_first = await pool.fetchval("SELECT count(*) FROM items WHERE source = 'books'")
            second = await worker.run_job(job, session, pool)
            count_after_second = await pool.fetchval("SELECT count(*) FROM items WHERE source = 'books'")
        title = await pool.fetchval("SELECT title FROM items WHERE external_id = 'book-41_41'")
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM items WHERE source = 'books'")
        await pool.close()

    assert first == second == 20
    assert count_after_first == count_after_second == 20
    assert title == "Book 41"
