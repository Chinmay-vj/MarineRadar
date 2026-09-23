FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=appuser:appuser . .
RUN mkdir -p /app/data && chown -R appuser:appuser /app/data

USER appuser
EXPOSE 8502

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8502/health', timeout=3)"

CMD ["uvicorn", "map_server:app", "--host", "0.0.0.0", "--port", "8502", "--workers", "2"]
