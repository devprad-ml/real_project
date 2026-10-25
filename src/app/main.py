''' init fastAPI api factory, lifespan, router wiring'''
# standard library imports always first
import time
import uuid
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version

# then third party dependencies
import anyio.to_thread
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

# then first party(local files)
from app.api.admin import router as admin_router
from app.api.ask import router as ask_router
from app.api.chat import router as chat_router
from app.api.documents import router as documents_router
from app.api.health import router as health_router
from app.api.review import router as review_router
from app.api.search import router as search_router
from app.api.validation import router as validation_router
from app.api.webhooks import router as webhooks_router
from app.config import get_settings
from app.db import engine
from app.observability.logging import configure, log, request_id
from app.processing import embed

# check version match else revert to default
try:
    __version__ = version("app")
except PackageNotFoundError:
    __version__ = "0.1.0"

@asynccontextmanager
async def lifespan(app: FastAPI):
    '''this try except block is for smoke tests 
    fails on wrong database URL'''
    configure()
    # The size db.py's connection pool is built around, and the most requests we let
    # in at once: the rest queue in limit_inflight, holding nothing.
    size = get_settings().threadpool_size
    anyio.to_thread.current_default_thread_limiter().total_tokens = size
    app.state.inflight = anyio.CapacityLimiter(size)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as e:
        log.error("database_unreachable", extra={"error_type": type(e).__name__})
        raise
    if get_settings().warm_embedder:
        # The first /ask after a deploy used to pay 19 s for this. Startup pays it once,
        # before the process accepts traffic.
        embed.encode(["warm-up"])
    yield
    engine.dispose()
    log.info("database connection pool closed")

async def limit_inflight(request: Request, call_next):
    ''' Admission control. Beyond `threadpool_size` concurrent requests the extras wait
    here, cheaply, instead of each grabbing a DB connection and then waiting for a
    thread while the threads wait for connections: that deadlock turned 160 concurrent
    users into 500s after the pool timeout (m13.md §8). A request that waits longer than
    queue_timeout_seconds gets a clean 503 the client can retry.
    ponytail: released when the response starts, not when its body finishes; only
    matters for large streamed bodies (the PDF download). '''
    if request.url.path == "/healthz":  # liveness must answer even when busy
        return await call_next(request)
    limiter = request.app.state.inflight
    try:
        with anyio.fail_after(get_settings().queue_timeout_seconds):
            await limiter.acquire()
    except TimeoutError:
        log.warning("overloaded", extra={"method": request.method, "status_code": 503})
        return JSONResponse(
            {"detail": "server busy, retry shortly"},
            status_code=503,
            headers={"Retry-After": "5"},
        )
    try:
        return await call_next(request)
    finally:
        limiter.release()


async def access_log(request: Request, call_next):
    ''' One line per request, replacing uvicorn's access log (run it with
    --no-access-log): that one prints the query string, and `GET /search?q=...` is PHI.
    This logs the route template, never the path or the query. '''
    rid = (request.headers.get("x-request-id") or uuid.uuid4().hex)[:64]
    token = request_id.set(rid)
    started = time.perf_counter()
    status_code = 500  # an exception escaping the app is a 500 to the client
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = rid
        return response
    finally:
        route = request.scope.get("route")
        log.info(
            "request",
            extra={
                "method": request.method,
                "route": route.path if route else "unmatched",
                "status_code": status_code,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "tenant_id": getattr(request.state, "tenant_id", None),
                "principal_id": getattr(request.state, "principal_id", None),
            },
        )
        request_id.reset(token)


# creating app factory
def create_app() -> FastAPI:
    '''App factory pattern'''
    app = FastAPI(
        title="Document ingestion pipeline",
        lifespan=lifespan,
        version=__version__
    )
    # Added first = inner: access_log, added last, wraps it and so logs the 503s too.
    app.middleware("http")(limit_inflight)
    app.middleware("http")(access_log)
    app.include_router(health_router)
    app.include_router(search_router)
    app.include_router(documents_router)
    app.include_router(validation_router)
    app.include_router(review_router)
    app.include_router(webhooks_router)
    app.include_router(admin_router)
    app.include_router(ask_router)
    app.include_router(chat_router)
    return app

# ASGI entrypoint for uvicorn
app = create_app()



