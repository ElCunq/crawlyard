"""Push catalogue pages onto the job queue.

    python enqueue.py --pages 50                                   # books.toscrape.com, pages 1-50
    python enqueue.py --pages 1000 --base http://fixture:8000 --source bench
"""
import argparse
import json
import os

import redis


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=50)
    ap.add_argument("--base", default="https://books.toscrape.com")
    ap.add_argument("--source", help="store records under this source name instead of the scraper's")
    args = ap.parse_args()

    jobs = []
    for page in range(1, args.pages + 1):
        job = {"scraper": "books", "url": f"{args.base}/catalogue/page-{page}.html"}
        if args.source:
            job["source"] = args.source
        jobs.append(json.dumps(job))

    r = redis.from_url(os.environ["REDIS_URL"])
    r.lpush(os.getenv("REDIS_QUEUE_NAME", "scrape_queue"), *jobs)
    print(f"queued {len(jobs)} jobs")


if __name__ == "__main__":
    main()
