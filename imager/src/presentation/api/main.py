from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from prometheus_client import CollectorRegistry, Counter, CONTENT_TYPE_LATEST, generate_latest
from src.presentation.api import routers
from src.presentation.api.workers import start_workers, stop_workers


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_workers()
    try:
        yield
    finally:
        await stop_workers()


def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    registry = CollectorRegistry()
    request_count = Counter('request_count', 'Total number of requests', registry=registry)

    @app.middleware('http')
    async def add_metrics(request, call_next):
        response = await call_next(request)
        request_count.inc()
        return response

    @app.get('/metrics', include_in_schema=False)
    async def metrics():
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    app.include_router(routers.router, prefix='/api')
    return app


app = create_app()
