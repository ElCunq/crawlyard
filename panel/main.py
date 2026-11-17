"""crawlyard panel: JSON API + static single-page UI for running the scraping fleet."""
from __future__ import annotations

import ast
import asyncio
import base64
import os
import secrets
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import docker
import psutil
import redis
import yaml
from docker.errors import DockerException, NotFound
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

BASE_CODE_DIR = Path(os.getenv("BASE_CODE_DIR", "/opt/crawlyard")).resolve()
REDIS_URL = os.environ["REDIS_URL"]
DATABASE_URL = os.environ["DATABASE_URL"]
PANEL_USER = os.getenv("PANEL_USER", "admin")
PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "")
if len(PANEL_PASSWORD) < 12:
    raise RuntimeError("PANEL_PASSWORD must be set and at least 12 characters long")
PANEL_VERSION = "1.0.0"
QUEUES = {"scrape_queue": "list", "dead_jobs": "list", "proxy_queue": "list",
          "good_proxies": "set", "banned_proxies": "set"}
SKIP_DIRS = {"node_modules", "__pycache__", ".git", ".venv"}


class BasicAuth:
    """HTTP Basic auth for every request, including WebSocket handshakes.

    Plain ASGI middleware rather than a FastAPI dependency so the static files
    and the /ws routes are covered too. Basic auth sends the password with every
    request, so the panel must sit behind TLS (see the Caddy service in compose.yml).
    """

    def __init__(self, app):
        self.app = app
        token = base64.b64encode(f"{PANEL_USER}:{PANEL_PASSWORD}".encode()).decode()
        self.expected = f"Basic {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            given = dict(scope["headers"]).get(b"authorization", b"")
            if not secrets.compare_digest(given, self.expected):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"www-authenticate", b'Basic realm="crawlyard"'),
                                        (b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"authentication required"})
                return
        await self.app(scope, receive, send)


_docker: docker.DockerClient | None = None
_redis: redis.Redis | None = None


def get_docker() -> docker.DockerClient:
    global _docker
    if _docker is None:
        try:
            _docker = docker.from_env()
        except DockerException as exc:
            raise HTTPException(503, f"Docker socket unavailable: {exc}") from exc
    return _docker


def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=3)
    return _redis


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    if _docker:
        _docker.close()
    if _redis:
        _redis.close()


app = FastAPI(title="crawlyard", version=PANEL_VERSION, docs_url="/api/docs", redoc_url=None, lifespan=lifespan)
app.add_middleware(BasicAuth)


def _safe_path(rel_path: str) -> Path:
    """Resolve a user-supplied path and refuse anything outside BASE_CODE_DIR."""
    resolved = (BASE_CODE_DIR / rel_path).resolve()
    if not resolved.is_relative_to(BASE_CODE_DIR):
        raise HTTPException(403, "Access denied: path is outside the code directory.")
    return resolved


def _redis_call(fn, *args):
    try:
        return fn(*args)
    except redis.RedisError as exc:
        raise HTTPException(503, f"Redis error: {exc}") from exc


def discover_compose_files() -> list[dict[str, Any]]:
    results = []
    for root, dirs, files in os.walk(BASE_CODE_DIR):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if not f.endswith((".yml", ".yaml")):
                continue
            path = Path(root) / f
            try:
                content = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (yaml.YAMLError, OSError, UnicodeDecodeError):
                continue
            if isinstance(content, dict) and isinstance(content.get("services"), dict):
                rel = path.relative_to(BASE_CODE_DIR)
                # Compose's project name: top-level `name:`, else the directory holding the file.
                project = content.get("name") or path.parent.name
                results.append({"name": str(rel), "file": str(rel), "project": project,
                                "services": list(content["services"])})
    return sorted(results, key=lambda x: x["name"])


def _container_summary(c) -> dict[str, Any]:
    state = c.attrs.get("State", {})
    return {
        "id": c.short_id, "name": c.name,
        "image": ",".join(c.image.tags) if c.image.tags else c.image.short_id,
        "status": c.status, "started_at": state.get("StartedAt", ""),
        "restart_count": state.get("RestartCount", 0),
        "service": c.labels.get("com.docker.compose.service", ""),
        "project": c.labels.get("com.docker.compose.project", ""),
    }


def _build_tree(base: Path, rel: str = "") -> list[dict]:
    items = []
    target = base / rel if rel else base
    try:
        entries = sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    except PermissionError:
        return items
    for entry in entries:
        if entry.name.startswith(".") or entry.name in SKIP_DIRS:
            continue
        rel_path = str(entry.relative_to(base))
        if entry.is_dir():
            items.append({"name": entry.name, "path": rel_path, "type": "dir", "children": _build_tree(base, rel_path)})
        else:
            items.append({"name": entry.name, "path": rel_path, "type": "file", "ext": entry.suffix})
    return items


async def _compose(compose_file: str, *args: str) -> str:
    """Run `docker compose -f <file> <args>` and return stdout, or raise 500 with stderr."""
    cmd = ["docker", "compose", "-f", str(_safe_path(compose_file)), *args]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise HTTPException(500, f"docker compose {' '.join(args)} failed: {stderr.decode()[-2000:]}")
    return stdout.decode() + stderr.decode()


def _require(payload: dict, *keys: str) -> list:
    missing = [k for k in keys if payload.get(k) in (None, "")]
    if missing:
        raise HTTPException(400, f"Missing field(s): {', '.join(missing)}")
    return [payload[k] for k in keys]


# ── Health & host ──
@app.get("/api/health")
async def health():
    docker_ver = None
    with suppress(HTTPException, DockerException):
        docker_ver = get_docker().version().get("Version")
    try:
        redis_ok = bool(get_redis().ping())
    except redis.RedisError:
        redis_ok = False
    return {"panel_version": PANEL_VERSION, "docker_connected": docker_ver is not None,
            "docker_version": docker_ver, "redis_connected": redis_ok,
            "timestamp": datetime.now(UTC).isoformat()}


@app.get("/api/host/stats")
async def host_stats():
    mem = psutil.virtual_memory()
    return {"cpu": psutil.cpu_percent(interval=None), "ram": mem.percent,
            "ram_used": round(mem.used / 1024**3, 2), "ram_total": round(mem.total / 1024**3, 2)}


# ── Files / IDE ──
LANGUAGES = {".py": "python", ".yml": "yaml", ".yaml": "yaml", ".sh": "shell", ".toml": "toml",
             ".json": "json", ".js": "javascript", ".html": "html", ".css": "css", ".md": "markdown"}


@app.get("/api/files")
async def list_files():
    return {"base": str(BASE_CODE_DIR), "tree": _build_tree(BASE_CODE_DIR)}


@app.get("/api/files/read")
async def read_file(path: str = Query(...)):
    fp = _safe_path(path)
    if not fp.is_file():
        raise HTTPException(404, f"File not found: {path}")
    try:
        content = fp.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(400, "Binary files cannot be opened.") from exc
    return {"path": path, "name": fp.name, "content": content, "language": LANGUAGES.get(fp.suffix, "plaintext")}


@app.post("/api/files/create")
async def create_file(payload: dict):
    (path,) = _require(payload, "path")
    fp = _safe_path(path)
    if fp.exists():
        raise HTTPException(409, "File already exists.")
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(payload.get("content", ""), encoding="utf-8")
    return {"created": True, "path": path}


@app.post("/api/files/deploy")
async def deploy_file(payload: dict):
    """Save a file (keeping a .bak of the previous version) and optionally restart a service."""
    path, content = _require(payload, "path", "content")
    fp = _safe_path(path)
    if fp.exists():
        fp.with_suffix(fp.suffix + ".bak").write_bytes(fp.read_bytes())
    else:
        fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")
    restarted = []
    if service := payload.get("service"):
        for c in get_docker().containers.list(filters={"label": f"com.docker.compose.service={service}"}):
            c.restart(timeout=10)
            restarted.append(c.name)
    return {"saved": True, "restarted": restarted}


@app.post("/api/ide/validate")
async def validate_code(payload: dict):
    filename = payload.get("filename", "script.py")
    if not filename.endswith(".py"):
        return {"valid": True, "message": "Syntax check only runs on Python files."}
    try:
        ast.parse(payload.get("content", ""), filename=filename)
    except SyntaxError as e:
        return {"valid": False, "message": f"SyntaxError: {e.msg}", "line": e.lineno,
                "offset": e.offset, "text": (e.text or "").strip()}
    return {"valid": True, "message": "Python syntax is valid."}


# ── Harvest stats ──
@app.get("/api/stats/summary")
async def stats_summary():
    try:
        conn = await asyncpg.connect(DATABASE_URL, timeout=3)
    except (TimeoutError, OSError, asyncpg.PostgresError) as exc:
        return {"connected": False, "error": str(exc)}
    try:
        row = await conn.fetchrow("""
            SELECT count(*) AS items,
                   count(DISTINCT source) AS sources,
                   count(*) FILTER (WHERE first_seen > now() - interval '1 hour') AS new_last_hour,
                   pg_database_size(current_database()) AS db_bytes
            FROM items""")
    except asyncpg.UndefinedTableError:
        row = {"items": 0, "sources": 0, "new_last_hour": 0, "db_bytes": 0}
    finally:
        await conn.close()
    try:
        counters = get_redis().hgetall("stats")
    except redis.RedisError:
        counters = {}
    return {"connected": True, **dict(row), "counters": counters}


# ── Redis ──
@app.get("/api/redis/info")
async def redis_info():
    r = get_redis()
    info = _redis_call(r.info)
    return {"used_memory_human": info.get("used_memory_human"), "connected_clients": info.get("connected_clients"),
            "uptime_seconds": info.get("uptime_in_seconds")}


@app.get("/api/redis/queues")
async def redis_queues():
    r = get_redis()
    return {"queues": {k: {"size": _redis_call(r.llen if t == "list" else r.scard, k), "type": t}
                       for k, t in QUEUES.items()}}


@app.get("/api/redis/queue/peek")
async def redis_peek(queue: str = Query(...), count: int = Query(20, ge=1, le=100)):
    r = get_redis()
    kind = _redis_call(r.type, queue)
    if kind == "list":
        items = _redis_call(r.lrange, queue, 0, count - 1)
    elif kind == "set":
        items = _redis_call(r.srandmember, queue, count)
    else:
        items = []
    return {"queue": queue, "type": kind, "count": len(items), "items": items}


@app.post("/api/redis/queue/push")
async def redis_push(payload: dict):
    queue, values = _require(payload, "queue", "values")
    r = get_redis()
    _redis_call(r.lpush, queue, *values)
    return {"pushed": len(values), "queue": queue, "new_size": _redis_call(r.llen, queue)}


@app.post("/api/redis/queue/flush")
async def redis_flush(payload: dict):
    (key,) = _require(payload, "key")
    _redis_call(get_redis().delete, key)
    return {"flushed": True, "key": key}


# ── Fleet ──
@app.get("/api/fleet/services")
async def fleet_services():
    return {"fleets": discover_compose_files()}


@app.post("/api/fleet/service/{action}")
async def control_service(action: str, payload: dict):
    compose_file, service = _require(payload, "compose_file", "service")
    args = {"start": ["up", "-d", "--no-deps", service], "stop": ["stop", service],
            "remove": ["rm", "-f", "-s", service]}.get(action)
    if args is None:
        raise HTTPException(404, f"Unknown action: {action}")
    return {"status": action, "output": await _compose(compose_file, *args)}


@app.post("/api/fleet/scale")
async def scale_fleet(payload: dict):
    compose_file, service = _require(payload, "compose_file", "service")
    count = payload.get("count")
    if not isinstance(count, int) or not 0 <= count <= 50:
        raise HTTPException(400, "'count' must be an integer between 0 and 50.")
    scale = f"{service}={count}"
    output = await _compose(compose_file, "up", "-d", "--no-deps", "--no-recreate", "--scale", scale, service)
    return {"target": count, "service": service, "output": output}


@app.post("/api/fleet/stop")
async def stop_fleet(payload: dict):
    (compose_file,) = _require(payload, "compose_file")
    return {"output": await _compose(compose_file, "stop")}


@app.get("/api/containers")
async def list_containers(all: bool = False):
    return {"containers": [_container_summary(c) for c in get_docker().containers.list(all=all)]}


@app.post("/api/containers/prune-exited")
async def prune_exited_containers():
    pruned = get_docker().containers.prune(filters={"status": "exited"})
    return {"deleted_count": len(pruned.get("ContainersDeleted") or []),
            "space_reclaimed": pruned.get("SpaceReclaimed") or 0}


# ── Log streaming ──
@app.websocket("/ws/logs/{container_id}")
async def stream_logs(websocket: WebSocket, container_id: str):
    await websocket.accept()
    try:
        container = get_docker().containers.get(container_id)
    except (NotFound, HTTPException) as exc:
        await websocket.send_json({"error": str(exc)})
        await websocket.close()
        return
    stream = await asyncio.to_thread(container.logs, stream=True, follow=True, tail=50, timestamps=True)
    try:
        while (raw := await asyncio.to_thread(next, stream, None)) is not None:
            if line := raw.decode("utf-8", errors="replace").strip():
                await websocket.send_json({"container": container.name, "log": line})
    except WebSocketDisconnect:
        pass
    finally:
        stream.close()


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
