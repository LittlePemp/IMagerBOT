from src.presentation.api.workers import start_workers
from fastapi import FastAPI
from prometheus_client import Counter, start_http_server
from src.presentation.api import routers

app = FastAPI()

start_http_server(8001)

REQUEST_COUNT = Counter('request_count', 'Total number of requests')


@app.middleware('http')
async def add_metrics(request, call_next):
    response = await call_next(request)
    REQUEST_COUNT.inc()
    return response


app.include_router(routers.router, prefix='/api')

@app.on_event('startup')
async def startup_event():
    start_workers()
