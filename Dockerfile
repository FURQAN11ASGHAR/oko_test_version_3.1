FROM python:3.11-slim

# libzbar0: runtime dep for pyzbar (barcode decoding)
RUN apt-get update && apt-get install -y --no-install-recommends libzbar0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY web ./web
COPY vocabularies ./vocabularies
COPY db ./db
COPY scripts ./scripts

EXPOSE 8000
# single worker: rate limiting and the scan-purge loop are in-process state
# (see app/ratelimit.py) — running >1 worker silently multiplies rate limits
# and runs the purge loop redundantly per worker. Move both to Postgres/Redis
# before scaling beyond one process.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
