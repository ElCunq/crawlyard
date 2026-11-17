"""Optional proxy pool feeder (compose profile "proxy").

Every hour it downloads the pre-checked SOCKS lists published by monosans/proxy-list,
adds proxies workers reported as good, drops the ones they banned, and rewrites
the Redis list `proxy_queue` that workers rotate through.
"""
import logging
import os
import random
import time
import urllib.error
import urllib.request

import redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", force=True)
log = logging.getLogger("proxy-manager")

REDIS_URL = os.environ["REDIS_URL"]
SOURCES = {
    "socks5": "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    "socks4": "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt",
}
REFRESH_SECONDS = 3600


def download(scheme: str, url: str) -> set[str]:
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            lines = resp.read().decode("utf-8", errors="ignore").split()
    except (urllib.error.URLError, TimeoutError) as exc:
        log.warning("could not download %s list: %s", scheme, exc)
        return set()
    return {line if "://" in line else f"{scheme}://{line}" for line in lines if not line.startswith("#")}


def refresh(r: redis.Redis) -> None:
    fresh = set().union(*(download(s, u) for s, u in SOURCES.items()))
    pool = (fresh | r.smembers("good_proxies") | set(r.lrange("proxy_queue", 0, -1))) - r.smembers("banned_proxies")
    pool = [p for p in pool if p.startswith(("socks4://", "socks5://"))]
    if not pool:
        log.error("no usable proxies; keeping the current queue")
        return
    random.shuffle(pool)
    with r.pipeline() as pipe:
        pipe.delete("proxy_queue").lpush("proxy_queue", *pool).execute()
    log.info("proxy_queue rewritten: %d proxies (%d downloaded)", len(pool), len(fresh))


def main() -> None:
    r = redis.from_url(REDIS_URL, decode_responses=True)
    while True:
        try:
            refresh(r)
        except redis.ConnectionError as exc:
            log.error("redis unavailable: %s", exc)
            time.sleep(30)
            continue
        time.sleep(REFRESH_SECONDS)


if __name__ == "__main__":
    main()
