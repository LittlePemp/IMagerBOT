from fastapi import FastAPI
from prometheus_client import Counter, start_http_server
from src.presentation.api import routers

app = FastAPI()

# Prometheus
start_http_server(8001)

# metrics
REQUEST_COUNT = Counter('request_count', 'Total number of requests')


@app.middleware('http')
async def add_metrics(request, call_next):
    response = await call_next(request)
    REQUEST_COUNT.inc()
    return response


app.include_router(routers.router, prefix='/api')
