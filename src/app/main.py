''' init fastAPI api factory, lifespan, router wiring'''
# standard library imports always first
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version

# then third party dependencies
from fastapi import FastAPI
from sqlalchemy import text

# then first party(local files)
from app.api.admin import router as admin_router
from app.api.documents import router as documents_router
from app.api.health import router as health_router
from app.api.review import router as review_router
from app.api.search import router as search_router
from app.api.validation import router as validation_router
from app.api.webhooks import router as webhooks_router
from app.db import engine

# check version match else revert to default
try:
    __version__ = version("app")
except PackageNotFoundError:
    __version__ = "0.1.0"

@asynccontextmanager
async def lifespan(app: FastAPI):
    '''this try except block is for smoke tests 
    fails on wrong database URL'''
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as e:
        print(f"Database connection failed: {e}")
        raise 
    yield
    engine.dispose()
    print("Database connection pool closed")   # for m9 structured logs

# creating app factory
def create_app() -> FastAPI:
    '''App factory pattern'''
    app = FastAPI(
        title="Document ingestion pipeline",
        lifespan=lifespan,
        version=__version__
    )
    app.include_router(health_router)
    app.include_router(search_router)
    app.include_router(documents_router)
    app.include_router(validation_router)
    app.include_router(review_router)
    app.include_router(webhooks_router)
    app.include_router(admin_router)
    return app

# ASGI entrypoint for uvicorn
app = create_app()



