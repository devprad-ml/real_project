''' init fastAPI api factory, lifespan, router wiring'''
# standard library imports always first
from contextlib import asynccontextmanager
# then third party dependencies

from fastapi import FastAPI
from sqlalchemy import text
from importlib.metadata import PackageNotFoundError, version
# then first party(local files)
from app.db import engine
from app.api.health import router as health_router

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
    return app

# ASGI entrypoint for uvicorn
app = create_app()



