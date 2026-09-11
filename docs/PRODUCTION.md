# Production Guide

Operational handover: what to configure, what to watch, what will actually
go wrong and why. `README.md` has the quick-start; this is the deeper
version for whoever's on call.

## Architecture in one paragraph

FastAPI app, single process. Postgres (with `pgvector`) is the only stateful
dependency. OpenRouter provides three model calls (vision, embedding,
extraction); `ddgs` (DuckDuckGo, no key) provides web search. A scan runs a
resolution ladder — sha256 cache, barcode/GTIN exact match, vector
similarity, then a live web lookup — stopping at the first tier that
resolves. Background work (the live-lookup tier, the scan-retention purge
loop) runs as `asyncio` tasks inside the same process; there is no queue,
no worker pool, no Redis.

## Deploying

```bash
docker build -t okotest-api .
docker run -d --env-file .env -p 8000:8000 okotest-api
```

Point `DATABASE_URL` at a real Postgres 16+ with the `vector` extension.
Apply `db/schema.sql` once against a fresh database — there is no migration
tool; schema changes since initial deploy have all been additive
(`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`), so re-running the whole file
is not idempotent for an existing database. Check `db/schema.sql`'s git
history for what's changed since your last deploy if you're not starting
fresh.

Issue at least one API key before anything can call the API:
```bash
python scripts/create_api_key.py scan read feedback
```

### Required config before going live

| Variable | Why it matters |
|---|---|
| `OPENROUTER_API_KEY` | Paid tier. See "Cost and rate limits" below — the free tier's real ceiling is nowhere near what a production scan volume needs. |
| `CORS_ALLOWED_ORIGINS` | Defaults to `*`. Set to the actual PHP site's origin(s) before launch — a wildcard plus an API-key-gated endpoint means any page with (or guessing) a key can drive scans from a user's browser. |
| `WEBHOOK_SIGNING_SECRET` | Empty by default, which signs webhook payloads with an empty HMAC key. Set this if `webhook_url` is used at all. |
| `TRUSTED_SOURCE_DOMAINS` | Defaults to `oekotest.de,www.oekotest.de` — correct out of the box, but if this is ever accidentally cleared, every tier-3 write-back silently stops persisting anything, with no error anywhere. This bit us once in development; it's why the default now lives in code, not just `.env.example`. Don't override it to empty. |

Everything else in `.env.example` has a working default.

## The single-process constraint (read this before scaling)

Two things are in-process state, not shared:

1. **Rate limiting** (`app/ratelimit.py`) — an in-memory sliding window, keyed by API key. Running N replicas or N uvicorn workers gives each one its own counter, so the *effective* rate limit becomes N× the configured value with no warning.
2. **Scan-retention purge** (`app/main.py`, `_purge_expired_scans_loop`) — runs on a 1-hour timer in every process that starts. Harmless if it runs redundantly (the DELETE is naturally idempotent), but it's wasted work per replica.

The Dockerfile runs exactly one uvicorn worker for this reason. If you need
to scale beyond one process:
- Move rate limiting to Postgres (a `rate_limit_hits` table with a
  short-TTL cleanup) or Redis (`INCR`+`EXPIRE`).
- Elect a single instance to run the purge loop (a Postgres advisory lock
  is the simplest option — `pg_try_advisory_lock` around the loop body).

Neither is hard, but both need doing before this runs as more than one
process.

## Cost and rate limits (OpenRouter)

Every scan makes 2-4 model calls (vision, embedding, and — only on a tier-3
live lookup — extraction, sometimes a fallback retry). At current OpenRouter
pricing (varies by model/provider, check `openrouter.ai/models` for current
rates) this is a small fraction of a cent per scan on the paid tier, but:

- **The app refuses to boot** if `SCAN_RATE_LIMIT_PER_MIN × MODEL_CALLS_PER_SCAN_MAX` exceeds `UPSTREAM_RPM_ALLOWANCE` (`app/config.py::assert_boot_invariants`). This is deliberate — it catches a rate-limit config that would silently produce `502 upstream_model_error` under load instead of an honest `429`. If the app won't start, this is almost always why; check the error message, it names both values.
- **Free-tier OpenRouter models are capped hard**: 20 req/min platform-wide, daily caps of 50-1000 depending on account credit history, and failed attempts still count against the daily quota. `MODEL_FALLBACKS` (default `minimax/minimax-m3:free`) exists as a safety net for when the paid endpoint itself errors, not as the primary path — if you see scans consistently landing on the free model in `pipeline.vision.model`/`pipeline.extraction.model`, something's wrong with the paid path (see gotchas below) or the account is out of credit.
- **Watch your OpenRouter credit balance directly** (`https://openrouter.ai/settings/credits` or the `/api/v1/credits` endpoint) — a 402 from OpenRouter (insufficient credit for the requested `max_tokens`) surfaces as `upstream_model_error` and, before a fix made in development, silently fell back to the free tier every time. See gotcha #2 below.

## Monitoring

`GET /v1/health` (no auth) — poll this, not a raw TCP check:

| `status` | Meaning | Action |
|---|---|---|
| `ok` | everything answering | — |
| `degraded` | `openrouter` key unset (tiers 0-2 still work, tier 3 will fail every time) or `database`/`vector_index` transiently unreachable | investigate, not necessarily urgent |
| `down` | database unreachable | page — nothing works without it |

Beyond health, the useful signals are in the data, not a metrics endpoint
(none exists yet):
- `scans.status = 'failed'` rate — spikes mean OpenRouter or the DB is having a bad day.
- `resolution_reason = 'live_lookup_failed'` rate — spikes mean either the write-back trust checks are rejecting everything (check whether `TRUSTED_SOURCE_DOMAINS` got cleared — see gotchas) or extraction is failing structurally (check `warnings` on affected scans for `upstream_model_error` vs `not_write_back_eligible`, they mean very different things).
- `review_queue` row count — findings codes outside the closed vocabulary land here instead of being silently dropped; a growing queue means the vocabulary (`vocabularies/findings_v1.yaml`) needs a human to extend it.

## Security notes

- API keys are hashed (SHA-256) at rest; the raw key is shown exactly once at creation and cannot be recovered. Revoke with `UPDATE api_keys SET revoked_at = now() WHERE key_id = ...` — there's no revoke endpoint yet, it's a direct SQL operation.
- CORS is wide open by default (`*`) — see "Required config" above. This matters more than usual here because the scan endpoint is a real cost center (OpenRouter calls) and a real per-key rate limit; an open CORS policy on a leaked or guessable key lets a third party spend both.
- Uploaded images are never persisted to disk or the database — only their sha256 hash and the vision model's structured output are stored. If the privacy policy needs to say what happens to images, the honest answer is: sent to OpenRouter for the vision call, discarded immediately after, never written anywhere else in this app.
- The `/test` browser page is unauthenticated and serves from the same origin as the API (no separate auth needed to view it) — it doesn't leak an API key on its own (you type one in), but don't deploy it somewhere public-facing without deciding whether you want a live scan console reachable by anyone. Consider gating `/test` behind the same reverse proxy auth as any other internal tool, or removing the route in production.

## Gotchas already hit in development (read before debugging the same thing again)

These were real, confirmed bugs found by testing against the live OpenRouter
API — not hypothetical edge cases. Listed because the failure mode is
non-obvious and easy to reintroduce.

1. **`EMBEDDING_MODEL` must be `baai/bge-m3`, not the spec's original `liquid/lfm-2.5-embedding-350m`.** That model doesn't exist on OpenRouter at all — verified against `/api/v1/models` and `/api/v1/embeddings` directly. `bge-m3` is the replacement: 1024-dim (matches the `vector(1024)` column with no migration) and multilingual. If you ever change `EMBEDDING_MODEL`, confirm the new model's dimension matches `EMBEDDING_DIM` (the app asserts this at boot) and re-embed the catalog (`app/repo.py` has no built-in re-embed job — this would need to be written).

2. **Uncapped `max_tokens` silently degrades every call to the free model.** Chat completions without an explicit `max_tokens` default to the model's full context (131k tokens), which a limited-credit account can't afford — OpenRouter returns a 402, and the fallback chain (`MODEL_FALLBACKS`) quietly retries on `minimax-m3:free` every single time. If `pipeline.vision.model`/`pipeline.extraction.model` in scan responses keep showing the `:free` suffix even with a funded paid key, check `max_tokens` on the relevant call in `app/openrouter.py` and check the account's actual credit balance.

3. **`tool_choice` forcing a specific function is not reliably honored by every model/provider.** The model sometimes answers with a complete, correct JSON object as markdown-fenced prose in `content` instead of a real `tool_calls` entry. `_extract_tool_args` (`app/openrouter.py`) recovers this by regex-extracting a JSON object from `content` as a fallback — if you see extraction results getting worse after a model swap, check whether the new model's non-compliance rate is higher and whether the recovery regex still matches its prose format.

4. **Reasoning-capable models can burn the entire token budget on chain-of-thought before emitting the actual tool call**, producing a truncated/invalid JSON that looks identical to case 3 above. Both vision and extraction calls pass `reasoning: {"enabled": false}` for this reason — removing it (e.g. to "let the model think more") will reintroduce silent truncation on the more complex extraction schema.

5. **An empty or misconfigured `TRUSTED_SOURCE_DOMAINS` disables every tier-3 write-back with zero visible error.** `is_trusted()` does `any(...)` over the configured list — an empty list makes that always `False`, rejecting even genuine `oekotest.de` URLs. The default now lives in `app/config.py` itself (not just `.env.example`) specifically so this can't happen by omission again. If write-backs mysteriously stop (`live_lookup_failed` with `not_write_back_eligible` on everything, even known-good products), check this first.

6. **DuckDuckGo search snippets rarely contain the actual verdict** — it's in the article body, not the meta description shown in results. `fetch_page_text()` (`app/pipeline/search.py`) fetches full text for trusted-domain hits specifically so extraction has something to cite; if this ever gets removed for a "the search API already gives us enough" reason, write-back success rate will drop sharply.

7. **A model reconstructing a URL by hand can introduce a typo in the path even when the domain is correct** — `is_trusted()` only checks the hostname, so a subtly wrong URL still "passes." Source URLs are additionally checked for exact membership in the set of URLs actually handed to the model (`seen_urls` in `app/pipeline/orchestrator.py`) before being trusted as a citation.

8. **A cached copy of `web/index.html` in a developer's browser can silently run stale JS** after a fix ships — the file changed many times during development and a plain reload doesn't always re-fetch it. `/test` now serves with `Cache-Control: no-store` and a visible build-marker line for exactly this reason; if a "fixed" bug in the test page appears to still happen, the person testing it needs a hard reload (Ctrl+Shift+R), not another fix.

## Known deferred work (not silently skipped)

- **Conflict/re-verification** of an *existing* catalog product against a newer Öko-Test issue isn't wired — every live lookup that reaches write-back is treated as a brand-new product. Re-scanning a product that's since been re-tested under a later issue will not update its record.
- **Findings extraction** (§7.3 of the original spec) only runs for the single-product path, not for a test-report scan's multi-product list.
- **Webhook delivery has no dead-letter.** After 5 failed retries the payload is dropped with no record. If webhook reliability becomes a real requirement, add a row to `review_queue` (or a dedicated table) on final failure.
- **No admin endpoints exist** despite the `admin` scope being defined — key revocation, review-queue triage, and dedupe-merge decisions (spec §6.3/6.4) are all direct SQL today.
- **No backfill/dedupe job** — if there's a legacy product database to seed this catalog from, `docs/API.md`'s data model matches spec §6.3's `mappings/*.yaml` design intent but nothing has been built against it (no legacy source was available during initial development).
