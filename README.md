# crawlyard

crawlyard runs a fleet of scraper workers on a single Docker host and gives you a web panel to drive it. Jobs go into a Redis list, workers turn each job into records with a pluggable `fetch(job) -> records` scraper, and records are upserted into one generic `items` table in Postgres. The panel scales workers, edits their code, inspects queues, tails container logs and shows harvest numbers.

The bundled example scraper targets [books.toscrape.com](https://books.toscrape.com), a sandbox site published for scraping practice.

![Fleet tab](docs/screenshots/fleet.png)

## Architecture

```
┌──────────────────────────── Browser ────────────────────────────┐
│   Fleet   │   Code (Monaco)   │   Queues   │   Logs   │   Stats  │
└────────────────────────────────┬────────────────────────────────┘
                                 │ HTTPS (Caddy, optional) + HTTP Basic auth
┌────────────────────────────────▼────────────────────────────────┐
│ panel  (FastAPI, 127.0.0.1:9090)                                 │
│ /api/fleet/*  /api/files/*  /api/redis/*  /api/stats  /ws/logs   │
└──────┬─────────────────┬───────────────────┬────────────────┬────┘
       │ docker.sock     │ docker compose    │ redis          │ asyncpg
       ▼                 ▼                   ▼                ▼
  container logs    scale / start /     ┌─────────┐     ┌──────────┐
  and status        stop services       │  Redis  │     │ Postgres │
                                        │ queues  │     │  items   │
                                        └────▲────┘     └────▲─────┘
                                   BRPOP job │               │ upsert
                                        ┌────┴───────────────┴────┐
                                        │ worker × N              │
                                        │ scrapers.fetch(job)     │──► target site
                                        └─────────────────────────┘
```

Everything runs on one internal Docker network. Postgres and Redis publish no ports. The panel binds to `127.0.0.1` only, and Caddy (behind `--profile tls`) is the one thing meant to face a network.

Each worker (`worker/main.py`) loops: `BRPOP scrape_queue`, look up the scraper named in the job, call `fetch`, upsert the records, sleep `WORKER_DELAY_MS`. A failed job goes back on the queue with `attempts + 1`; after `MAX_JOB_ATTEMPTS` (3) it moves to `dead_jobs`. Counters go to the Redis hash `stats`.

Retries happen at two levels. `get_text` retries a single request on 429, 5xx, timeouts and refused connections with exponential backoff (0.5 s, 1 s), and does not retry other 4xx. Above that, the job itself is retried up to three times.

Records are keyed on `(source, external_id)` and written with `INSERT … ON CONFLICT DO UPDATE`, so processing the same job twice leaves the row count unchanged and refreshes `last_seen`.

### Adding a scraper

```python
# worker/scrapers.py
class MySite:
    name = "mysite"

    async def fetch(self, job, session):
        html = await get_text(session, job["url"])     # retries included
        return [Record(external_id=..., title=..., url=..., data={...}) for ... in parse(html)]

SCRAPERS = {s.name: s for s in [BooksToScrape(), MySite()]}
```

Queue a job with `{"scraper": "mysite", "url": "…"}`. A bare URL is treated as a job for `DEFAULT_SCRAPER`.

## Running it

Needs Docker Engine with Compose v2.

```bash
cp .env.example .env          # then replace every CHANGE_ME (openssl rand -base64 24)
docker compose up -d --build
docker compose exec worker python enqueue.py --pages 50    # all 50 catalogue pages, 1000 books
```

Open http://127.0.0.1:9090 and log in with `PANEL_USER` / `PANEL_PASSWORD`. The panel refuses to start if the password is shorter than 12 characters.

To reach the panel from another machine, put it behind TLS. Do not publish port 9090:

```bash
PANEL_DOMAIN=panel.example.com docker compose --profile tls up -d   # Let's Encrypt
docker compose --profile tls up -d                                   # localhost, Caddy's local CA
```

Scale from the Fleet tab or from the shell:

```bash
docker compose up -d --scale worker=4
```

Optional SOCKS proxy rotation: set `USE_PROXY=true` and start `docker compose --profile proxy up -d`. The proxy manager pulls the pre-checked lists from [monosans/proxy-list](https://github.com/monosans/proxy-list) every hour; workers ban proxies that fail at the connection level.

### Tests and checks

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/ruff check .
TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:5432/postgres .venv/bin/pytest
npx jscpd@4.0.5 worker panel bench tests
```

| Test | What it proves |
|---|---|
| `test_idempotency.py` | Running the same job twice against a real Postgres leaves 20 rows, not 40. Skipped without `TEST_DATABASE_URL`. |
| `test_panel_security.py` | `../../etc/passwd`, `/etc/passwd` and the sibling-prefix case `../code-secrets/key.txt` all return 403. Requests and WebSocket handshakes without credentials are refused. |
| `test_retry.py` | Against a fake HTTP server: a 503, then a 429, then a 200 succeeds on the third try; three 500s give up; a 404 is not retried; refused connections are retried. |

CI (`.github/workflows/ci.yml`) runs ruff, jscpd (fails above 3% duplication), pytest with a Postgres service container, and `docker compose config` against `.env.example`.

## Measured numbers

Measured on a laptop: Intel Core i7-12700H (20 threads), 15 GB RAM, Docker 29.9, Linux. Reproduce on your own host with `bench/run.sh`; the raw log is in `bench/results/`.

Against the real site (books.toscrape.com, 50 jobs, 2 workers, `WORKER_DELAY_MS=1000`), crawlyard stored 1000 records in about 40 s with 0 failed jobs. The 1 s pause each worker takes between jobs sets that pace.

The other runs use the local fixture `bench/fixture_site.py`, which serves the same markup as books.toscrape.com, adds 200 ms of latency to every response and answers 5% of requests with a 503. 600 jobs, 20 records each, `WORKER_DELAY_MS=0`:

| Workers | Time | Jobs/min | Records/min | Requests answered 503 | Jobs re-queued | Dead jobs | Memory per worker | CPU, all workers |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 154.8 s | 233 | 4,651 | 6.0% | 0 | 0 | 35 MiB | 3% |
| 2 | 73.5 s | 490 | 9,798 | 4.3% | 1 | 0 | 35 MiB | 6% |
| 4 | 35.9 s | 1,004 | 20,080 | 3.4% | 0 | 0 | 34 MiB | 12% |
| 8 | 19.1 s | 1,889 | 37,776 | 4.3% | 1 | 0 | 34 MiB | 21% |

With latency in the way, throughput scales almost linearly with worker count: each worker spends most of its time waiting. The request-level retry absorbed every injected 503. Only two jobs out of 2,400 needed a job-level retry, and none ended in `dead_jobs`.

With the latency and the failures switched off (2,000 jobs), the workers become CPU-bound:

| Workers | Time | Jobs/min | Records/min | CPU, all workers |
|---:|---:|---:|---:|---:|
| 1 | 17.3 s | 6,946 | 138,927 | 74% |
| 4 | 5.9 s | 20,446 | 408,915 | 317% |
| 8 | 3.7 s | 32,080 | 641,602 | 641% |

Here the ceiling is HTML parsing (BeautifulSoup with `html.parser`): 8 workers use about 6.4 cores for 4.6× the throughput of one. The 8-worker run is short enough that the 0.1 s completion poll adds a few percent of error.

## Screenshots

| | |
|---|---|
| ![Code editor](docs/screenshots/code.png) | ![Queues](docs/screenshots/queues.png) |
| ![Logs](docs/screenshots/logs.png) | ![Stats](docs/screenshots/stats.png) |

## Security

**The panel is root on the host.** It mounts `/var/run/docker.sock` to list containers, stream logs and run `docker compose`. Anyone who can call the Docker API can start a privileged container that mounts `/` and do anything on the host. The Code tab writes files that workers execute, which is remote code execution by design. Treat panel credentials like the host's root password:

- The panel only listens on `127.0.0.1`. Reach it over SSH (`ssh -L 9090:127.0.0.1:9090 host`) or through the Caddy TLS profile. Never publish 9090 directly.
- HTTP Basic sends the password with every request, so it must travel over TLS. Without it, anyone on the path can read it.
- A [docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy) that only allows `containers` and `logs` would shrink the blast radius, but scaling through `docker compose` needs most of the API anyway. If you do not need the panel, do not run it.

Other measures that are in place:

- Postgres and Redis have no published ports. Their passwords live in `.env` (git-ignored), and compose refuses to start without them.
- Redis requires a password (`--requirepass`).
- `_safe_path` resolves every path the file API receives and rejects anything outside the code directory with `Path.is_relative_to`. A string `startswith` check would let `../code-secrets` through when the base is `/opt/code`.
- The UI escapes every value it renders, because anyone who can push a job controls what the queue view shows.

## Known limits

- A job can be lost if a worker dies mid-job. `BRPOP` removes the job before work starts. The fix is `BLMOVE` into a per-worker processing list with a reaper for dead workers; it is marked in `worker/main.py`.
- Everything runs on one host, so scaling only adds containers to the same Docker daemon. Spreading workers across machines would need Swarm, Nomad or Kubernetes, plus Redis and Postgres reachable over the network with TLS.
- There is one shared Basic-auth account, with no roles and no audit log of who scaled what or edited which file.
- The throughput figures come from a laptop and a local fixture. Against a real site the limit is how fast that site lets you go. Keep `WORKER_DELAY_MS` polite.
- HTML parsing is the CPU ceiling. Switching to `lxml` would raise it, which the rates above do not call for.
- Log windows can only be reordered by dragging with a mouse; there is no keyboard or touch alternative yet.

## License

MIT, see [LICENSE](LICENSE).
