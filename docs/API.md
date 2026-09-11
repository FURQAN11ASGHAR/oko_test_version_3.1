# API Reference

Base URL: wherever you deploy it (local dev: `http://localhost:8000`). Live
interactive docs (auto-generated from the actual code, always current):
`GET /docs` (Swagger UI) or `GET /redoc`.

This document explains the *shape and meaning* of responses — the parts an
OpenAPI schema doesn't capture well: which fields appear under which
conditions, and how a client should branch on them.

## Authentication

Every endpoint except `/v1/health` and `/test` requires:

```
X-API-Key: ok_live_...
```

Keys are scoped: `scan`, `read`, `feedback`, `admin` (currently `admin` is
reserved, no endpoint checks for it yet). A missing/invalid key returns
`401 unauthenticated`; a valid key missing the required scope returns
`403 insufficient_scope`.

Issue a key: `python scripts/create_api_key.py scan read feedback` — the raw
key is printed once and only its SHA-256 hash is stored. There is no way to
recover a lost key; issue a new one and revoke the old one directly in the
`api_keys` table (`UPDATE api_keys SET revoked_at = now() WHERE key_id = ...`).

## Rate limits

| Endpoint group | Limit | Enforced by |
|---|---|---|
| `POST /v1/scans` | `SCAN_RATE_LIMIT_PER_MIN`/min, `SCAN_RATE_LIMIT_PER_DAY`/day | per API key |
| `GET` endpoints | `READ_RATE_LIMIT_PER_MIN`/min | per API key |
| Live lookup (tier 3) | `LIVE_LOOKUP_RATE_LIMIT_PER_DAY`/day | per API key |

Every response carries:

```
X-RateLimit-Limit: 4
X-RateLimit-Remaining: 3
X-RateLimit-Reset: 1756809900
```

A `429 rate_limited` additionally carries `Retry-After` (seconds). The
live-lookup budget is different from the others: exhausting it does **not**
return a 429 — a scan request just silently runs tiers 0-2 only and comes
back `unresolved` / `live_lookup_disabled` (see below). This is intentional:
scanning still works, it just can't afford a fresh web lookup right now.

Rate limiting is in-process (see `docs/PRODUCTION.md` — single instance only).

## `POST /v1/scans`

Submit a photo for identification. Two request formats:

**multipart/form-data**

| Field | Required | Notes |
|---|---|---|
| `image` | yes | JPEG/PNG/WebP, ≤ `IMAGE_MAX_BYTES` (default 10MB) |
| `hint` | no | free text passed to the vision model, e.g. a brand name |
| `locale` | no | default `de-DE` |
| `webhook_url` | no | HTTPS URL to POST the finished scan to |
| `allow_live_lookup` | no | `"true"`/`"false"`, default true |
| `context` | no | JSON-encoded string of the context object (see below) |

**application/json**

```json
{
  "image_base64": "...",
  "hint": "Alverde shampoo",
  "locale": "de-DE",
  "webhook_url": "https://example.com/hooks/okotest",
  "allow_live_lookup": true,
  "context": { "intended_use": ["infant"], "application": "skin_leave_on" }
}
```

**Response**

- `200` if tiers 0-2 resolved it inline (cache hit, GTIN match, vector match) — typically under a few seconds.
- `202` if it fell through to tier 3 (live web lookup, up to ~30s). Body includes `poll_url` and `estimated_ms`; poll `GET /v1/scans/{scan_id}` until `status` is `completed`/`failed`.

### The `context` object

Optional, changes nothing about the verdict — it only selects which of the
test's own findings are relevant enough to surface (`context_relevance`,
below).

| Field | Type | Values |
|---|---|---|
| `intended_use` | string[] | `infant`, `child`, `adult`, `pregnancy`, `breastfeeding`, `sensitive_skin`, `allergy_prone`, `pets` |
| `application` | string | `skin_leave_on`, `skin_rinse_off`, `oral`, `inhaled`, `food_contact`, `textile_worn`, `household_surface`, `outdoor` |
| `frequency` | string | `single`, `occasional`, `weekly`, `daily` |
| `environment` | string | `indoor_bedroom`, `indoor_living`, `indoor_wet`, `outdoor`, `enclosed_vehicle` |
| `notes` | string | free text, max 500 chars — passed to vision as a hint only, **never** used for relevance matching |

## `GET /v1/scans/{scan_id}`

Poll for a result. `status` is one of `queued`, `processing`, `completed`,
`failed`, `expired` (scans are deleted after `SCAN_RETENTION_DAYS`; the
derived catalog product is untouched).

### Full response shape

```json
{
  "scan_id": "scn_...",
  "status": "completed",
  "created_at": "2026-09-02T11:04:19Z",
  "completed_at": "2026-09-02T11:04:33Z",
  "duration_ms": 13840,

  "resolution": {
    "tier": "vector_match",
    "confidence": 0.91,
    "verified": true
  },

  "product": {
    "product_id": "prd_...",
    "brand": "Alverde",
    "name": "Feuchtigkeits-Shampoo",
    "variant": "Aloe Vera, 200 ml",
    "category": "koerperpflege/haarpflege/shampoo",
    "gtins": ["4058172123456"],
    "identified_from": { "brand": 0.97, "name": 0.89, "variant": 0.71, "gtin": null },
    "general_info": null
  },

  "test_results": [ /* see below */ ],

  "pipeline": {
    "vision":     { "model": "minimax/minimax-m3", "provider": "openrouter", "ms": 1840, "tokens": 1204 },
    "embedding":  { "model": "baai/bge-m3", "provider": "openrouter", "ms": 210 },
    "extraction": null,
    "search":     null
  },

  "warnings": []
}
```

`candidates`, `test_report`, `context_relevance`, and `product.identified_from`/`general_info`
only appear when relevant (see field reference below) — they are not always
present. `pipeline` also carries `identified_from`/`identified_product` and,
for report scans, `candidates`/`report` internally; the response mirrors the
useful parts of those into the top-level fields described here, so treat
`pipeline` itself as diagnostic/timing info, not a field to build UI on.

### `resolution`

| Field | Meaning |
|---|---|
| `tier` | `image_cache`, `gtin_exact`, `vector_match`, `live_lookup`, or `unresolved` |
| `confidence` | 0-1. For a catalog match, the match score. For an identified-but-unresolved outcome, the vision model's own identification confidence. `null` only when nothing was identified at all (`image_unreadable`, `no_product_detected`). |
| `verified` | `true` for `image_cache`/`gtin_exact`/`vector_match` (already-catalogued, authoritative), and for `live_lookup` when write-back succeeded. `false` for everything else, including a live lookup that found something but couldn't verify it. |
| `reason` | present only when `tier` is `unresolved`, or a `live_lookup` didn't clear write-back. See the reason table below. |

**Branch on `tier` + `reason`, never on whether `test_results` is empty** —
an empty array can mean "genuinely no result exists" (`product_untested`) or
"couldn't get a result this time" (`live_lookup_failed`, retryable). Those
need different UI.

| `reason` | Meaning | Suggested handling |
|---|---|---|
| `image_unreadable` | Vision got nothing usable | Ask for another photo |
| `no_product_detected` | No product in frame | Ask for another photo |
| `product_ambiguous` | Several similarly-prominent products, none confident enough | Show `candidates`, let the user pick, then use `GET /v1/products/search?q=...` |
| `product_untested` | Identified with confidence; Öko-Test has never tested it | Show the identified product + `general_info` if present. Not an error. |
| `live_lookup_disabled` | Live-lookup budget exhausted or `allow_live_lookup:false` | Retryable |
| `live_lookup_failed` | Search/extraction errored, or found a verdict that didn't clear write-back rules | Retryable |

### `product`

Present whenever *something* was identified, even without a catalog entry
(`product_id: null`) — e.g. `live_lookup_disabled` fires before a catalog row
would ever be created, but vision still ran.

- `identified_from`: per-field vision confidence (0-1) for `brand`/`name`/`variant`; `gtin` is always `null` here because a GTIN comes from the barcode decoder, never vision. Only present when vision actually ran (absent for `image_cache`/`gtin_exact` hits, which skip vision entirely).
- `general_info`: present only when a live lookup ran and found something worth saying, whether or not a verdict was found. **Not an Öko-Test finding** — real content sourced from the open web (not restricted to oekotest.de), always carries `disclaimer`, and is never a substitute for a missing verdict. Shape:
  ```json
  {
    "description": "...",
    "features": ["...", "..."],
    "source_urls": ["..."],
    "disclaimer": "General product information gathered from public web sources. Not an Öko-Test finding and not verified or endorsed by Öko-Test."
  }
  ```

### `test_results[]`

Only ever contains **verified, written-back Öko-Test verdicts** — never
general web content. A verdict is written back only if it cites a genuine
`oekotest.de` URL that was actually present in the search results (never
invented or third-party) and clears `WRITE_BACK_MIN_CONFIDENCE`.

```json
{
  "result_id": "res_...",
  "verdict": "sehr_gut",
  "verdict_label": "sehr gut",
  "test_date": "2025-03",
  "publication": { "issue": "ÖKO-TEST Magazin 03/2025", "test_name": "Shampoos für trockenes Haar" },
  "source_urls": ["https://www.oekotest.de/..."],
  "findings": [
    { "code": "halogenorganic_compounds", "label": "Halogenorganische Verbindungen", "as_reported": "nachgewiesen", "deduction": "verdict_impacting", "source_url": "https://www.oekotest.de/..." }
  ],
  "retrieved_at": "2026-09-02T11:04:33Z",
  "is_current": true
}
```

`verdict` is one of `sehr_gut`, `gut`, `befriedigend`, `ausreichend`,
`mangelhaft`, `ungenuegend`. `findings[]` is usually empty — it's only
populated when the test itself explicitly called out a specific substance
group as impacting the verdict, and only from the closed vocabulary in
`vocabularies/findings_v1.yaml`.

### `candidates` (only present when `reason: product_ambiguous`)

```json
[
  { "brand": "Nivea", "name": "Creme", "category": "skincare" },
  { "brand": "Dove", "name": "Soap", "category": "soap" }
]
```

The first entry is vision's primary pick; the rest are the other similarly-
prominent products it saw. None of these have a `product_id` yet — resolve
one with `GET /v1/products/search?q=...`.

### `test_report` (only present for a photo of an Öko-Test magazine/report itself)

When the photographed thing is the *test report*, not a purchasable product,
vision flags that internally and extraction lists every individual product
it can confidently attribute a verdict to from that specific test:

```json
{
  "subject": "Olivenöl",
  "test_name": "Olivenöle im Test",
  "test_date": "2022-05",
  "issue": "ÖKO-TEST Magazin Mai 2022",
  "products": [
    { "product_id": "prd_...", "brand": "...", "name": "...", "test_results": [ /* same shape as above */ ] }
  ]
}
```

`products` can legitimately be empty — Öko-Test frequently paywalls the
full per-product breakdown, showing only a teaser online. When that happens,
`product.general_info` still carries whatever the test's scope/headline
finding actually was (real content, not a substitute for the missing list).

### `context_relevance` (only present when the request included `context`)

```json
{
  "matched_context": { "intended_use": ["infant"], "application": "skin_leave_on" },
  "relevant_findings": [
    { "code": "halogenorganic_compounds", "label": "...", "relevance": "high", "why": "leave_on_application", "source_url": "..." }
  ],
  "escalated": true,
  "relevance_map_version": "relevance_v1.0",
  "disclaimer_required": true
}
```

`relevance` is `low`/`moderate`/`high`, mapped from a static, human-edited
table (`vocabularies/relevance_v1.yaml`) — never an LLM judgment per request.
When `disclaimer_required` is `true` (an `infant`/`pregnancy`/`breastfeeding`
context matched an escalated finding), the client **must** show a deferral
to a pharmacist/physician rather than presenting this as medical guidance.
This block never changes `verdict`/`verdict_label` — those are identical
with or without `context`.

## `POST /v1/scans/{scan_id}/feedback`

```json
{ "kind": "wrong_product", "correct_gtin": "4058172123456", "comment": "..." }
```

`kind`: `wrong_product`, `wrong_test_result`, `outdated_result`,
`product_not_found`, `other`. Returns `202 { "feedback_id": "...", "status": "recorded" }`.
Feedback is recorded, not yet acted on automatically — it lands in the
`feedback` table for manual review.

## `GET /v1/products/{product_id}`

Fetch a catalog entry directly, no image involved. `404 not_found` if it
doesn't exist. Response: `{ "product": {...same shape as scan's product, plus created_at/updated_at/verified/scan_count}, "test_results": [...] }`.

## `GET /v1/products/search`

Manual lookup fallback (no vision call).

| Param | Notes |
|---|---|
| `q` | free text, embedded and matched by vector similarity |
| `gtin` | exact match, takes precedence over `q` |
| `category` | filters by category path prefix |
| `limit` | 1-50, default 10 |
| `min_score` | 0-1, default 0.60 |

```json
{
  "query": "alverde aloe shampoo",
  "match_type": "vector",
  "results": [
    { "product_id": "prd_...", "brand": "Alverde", "name": "...", "variant": "...", "score": 0.88, "verdict": "sehr_gut", "test_date": "2025-03" }
  ],
  "total": 1
}
```

## `GET /v1/health`

No auth required.

```json
{ "status": "ok", "dependencies": { "database": "ok", "vector_index": "ok", "openrouter": "ok", "search_provider": "ok" }, "version": "0.1.0" }
```

`status` is `ok`/`degraded`/`down` (HTTP 200 for the first two, 503 for
`down`). `openrouter` reports `degraded` when no API key is configured —
tiers 0-2 still work, tier 3 will fail per-scan. `search_provider` doesn't
depend on config (it's `ddgs`, no key needed) so it's always `ok` here; a
real outage there only surfaces as a per-scan `live_lookup_failed`.

## Webhooks

If `webhook_url` was supplied, the finished scan (same shape as `GET
/v1/scans/{id}`) is POSTed there on completion.

```
Content-Type: application/json
X-Okotest-Event: scan.completed | scan.failed
X-Okotest-Signature: t=<unix_ts>,v1=<hmac_sha256>
```

Signature = `HMAC-SHA256("{timestamp}.{raw_body}", WEBHOOK_SIGNING_SECRET)`.
**Set `WEBHOOK_SIGNING_SECRET` before relying on this in production** — an
unset secret signs with an empty key. Reject anything older than 5 minutes.
Retries: 5 attempts, 1s/4s/16s/64s/256s backoff, any 2xx counts as delivered.
Exhausted retries are currently dropped silently (no dead-letter yet — see
`docs/PRODUCTION.md`).

## Errors

Uniform envelope on every non-2xx response:

```json
{ "error": { "code": "image_too_large", "message": "...", "request_id": "req_...", "details": { "size_bytes": 14882301, "limit_bytes": 10485760 } } }
```

| HTTP | `code` |
|---|---|
| 400 | `invalid_request`, `unsupported_media_type`, `image_too_large` |
| 401 | `unauthenticated` |
| 403 | `insufficient_scope` |
| 404 | `not_found` |
| 429 | `rate_limited` |
| 500 | `internal_error` |
| 502 | `upstream_model_error` |

A scan that hits `502`/upstream failure mid-pipeline doesn't error the HTTP
request — it completes with `status: "failed"` and the error in `warnings`.
The 502 envelope above is only for calls that fail synchronously (e.g.
`/v1/products/search?q=...` when the embedding call itself fails).
