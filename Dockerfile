# A pinned runtime keeps the persistent Telegram session independent of a PaaS
# provider's default Python version.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./

RUN useradd --create-home --shell /usr/sbin/nologin worker && chown -R worker:worker /app
USER worker

EXPOSE 8080

CMD ["python", "worker.py"]
