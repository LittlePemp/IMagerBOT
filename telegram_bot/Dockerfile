FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app:/app/telegram_bot
WORKDIR /app/telegram_bot

COPY telegram_bot/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY imager_contracts /app/imager_contracts
COPY telegram_bot /app/telegram_bot

CMD ["python", "main.py"]
