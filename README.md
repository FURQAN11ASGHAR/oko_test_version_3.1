# Öko-Test Lookup API

Identifies a photographed product and returns its Öko-Test result, or the
closest honest alternative (general product info, or a report's actual
findings) when no official result exists.

- **[docs/API.md](docs/API.md)** — full API reference: request/response shapes, every conditional field, error codes, webhooks
- **[docs/PRODUCTION.md](docs/PRODUCTION.md)** — deployment, scaling limits, monitoring, security, and every real bug already hit and fixed
- `GET /docs` — live OpenAPI/Swagger UI once running

## Run locally

```bash
docker compose up -d                      # Postgres + pgvector
cp .env.example .env                      # fill in OPENROUTER_API_KEY
pip install -r requirements.txt
python scripts/create_api_key.py scan read feedback admin
uvicorn app.main:app --reload
```

Browser test page: `http://localhost:8000/test`.

## Run in production

```bash
docker build -t okotest-api .
docker run -d --env-file .env -p 8000:8000 okotest-api
```

Point `DATABASE_URL` at a real Postgres instance with the `vector` extension
(the same `db/schema.sql` this repo uses locally — apply it once against a
fresh database, or `docker exec <container> psql ... < db/schema.sql`).

### Required config

| Variable | Notes |
|---|---|
| `DATABASE_URL` | Postgres with `vector` and `pgcrypto` extensions |
| `OPENROUTER_API_KEY` | paid tier recommended — see `MODEL_CALLS_PER_SCAN_MAX`/`UPSTREAM_RPM_ALLOWANCE` below |
| `CORS_ALLOWED_ORIGINS` | comma-separated origins for the PHP site's browser JS; defaults to `*`, **lock this down before going live** |
| `TRUSTED_SOURCE_DOMAINS` | defaults to `oekotest.de,www.oekotest.de` — only these domains can back a written-back verdict |

Everything else in `.env.example` has a working default. The app refuses to
boot if `SCAN_RATE_LIMIT_PER_MIN × MODEL_CALLS_PER_SCAN_MAX` exceeds
`UPSTREAM_RPM_ALLOWANCE` — that's intentional (see `app/config.py`), fix the
rate limit or the allowance rather than removing the check.

### Known scaling limit

Rate limiting (`app/ratelimit.py`) and the scan-retention purge loop
(`app/main.py`) are in-process state, correct for exactly one running
instance. The Dockerfile runs a single uvicorn worker on purpose — running
more workers or replicas silently multiplies rate limits per-process.
Move both to Postgres or Redis before scaling horizontally.

### API keys

`X-API-Key` header, scoped to `scan` / `read` / `feedback` / `admin`. Issue
one with `python scripts/create_api_key.py <scopes...>` — the raw key is
shown once, only its hash is stored.

## Architecture

```
app/
  main.py           FastAPI app, CORS, lifespan (DB pool, scan purge loop)
  config.py         env config + boot-time invariant checks
  auth.py           X-API-Key -> scoped ApiKey
  ratelimit.py       in-memory sliding-window limits + X-RateLimit-* headers
  openrouter.py      vision / embedding / extraction calls (OpenRouter)
  repo.py            all SQL
  schemas.py         request/response shapes
  pipeline/
    orchestrator.py  the resolution ladder (tiers 0-3)
    barcode.py       GTIN decode (pyzbar), ahead of any vision call
    search.py        web search (ddgs) + trusted-domain check
    relevance.py     findings/context-relevance vocab lookup (spec §7)
  routers/           scans, products, health
db/schema.sql        Postgres schema (products, test_results, scans, ...)
vocabularies/        findings_v1.yaml, relevance_v1.yaml — human-edited, not model-generated
web/index.html       browser test page, served at /test
```

## What "no result" actually means

Every scan lands in one of: resolved (cache/GTIN/vector match), resolved-new
(fresh live lookup, written back), or unresolved with a specific `reason`.
`product_untested` is a positive finding — the product was identified but
Öko-Test has never tested it — and is cached (`UNTESTED_RECHECK_DAYS`) so
repeat scans don't re-pay for a live lookup. `product_ambiguous` means
several similarly-prominent products were seen in one photo; the response
carries a `candidates` array instead of guessing one.

A verdict only gets written to the catalog if it cites a genuine
`oekotest.de` URL that was actually present in the search results (never a
model-transcribed or third-party one) and clears `WRITE_BACK_MIN_CONFIDENCE`.
Everything else — `general_info` on the product, a test report's full
product list — is clearly separate from `test_results` and carries its own
disclaimer; it is real, sourced content, never a substitute for a verdict
that doesn't exist.
