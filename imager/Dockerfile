FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app:/app/imager
WORKDIR /app/imager

COPY imager/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY imager_contracts /app/imager_contracts
COPY imager /app/imager

CMD ["python", "-m", "src.presentation.temporal.worker"]
