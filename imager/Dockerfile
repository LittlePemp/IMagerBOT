FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app:/app/imager
WORKDIR /app/imager

COPY imager/requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements.txt

COPY imager_contracts /app/imager_contracts
COPY imager_config /app/imager_config
COPY config /app/config
COPY imager /app/imager

CMD ["python", "-m", "src.presentation.temporal.worker"]
