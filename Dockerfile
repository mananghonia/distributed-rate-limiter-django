FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn==22.0.0

COPY . .

EXPOSE 8000
# Shell form so $PORT expands. Render assigns $PORT at runtime; local
# docker-compose has no $PORT set, so it falls back to 8000.
# Threaded (gthread) workers: the demo self-proxies to its own /demo-upstream,
# so a request thread blocks on an in-process HTTP call -- threads give enough
# concurrency that this can't starve the worker pool.
CMD gunicorn gateway.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 2 --threads 8 --timeout 60
