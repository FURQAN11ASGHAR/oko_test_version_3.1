-- Öko-Test Lookup API — core schema (spec §6.1)
-- Run once against a fresh Postgres 16+ with pgvector installed.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto; -- gen_random_uuid, digest()

CREATE TYPE verdict_t AS ENUM (
  'sehr_gut', 'gut', 'befriedigend', 'ausreichend', 'mangelhaft', 'ungenuegend'
);

CREATE TYPE origin_t AS ENUM ('seed', 'live_lookup', 'manual', 'merged');

CREATE TYPE scan_status_t AS ENUM ('queued', 'processing', 'completed', 'failed', 'expired');

CREATE TYPE resolution_tier_t AS ENUM (
  'image_cache', 'gtin_exact', 'vector_match', 'live_lookup', 'unresolved'
);

CREATE TABLE api_keys (
  key_id        TEXT PRIMARY KEY,              -- 'key_' + ULID
  key_hash      TEXT NOT NULL UNIQUE,           -- sha256 of the raw key
  scopes        TEXT[] NOT NULL,                -- subset of scan|read|feedback|admin
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  revoked_at    TIMESTAMPTZ
);

CREATE TABLE products (
  product_id    TEXT PRIMARY KEY,               -- 'prd_' + ULID
  brand         TEXT NOT NULL,
  name          TEXT NOT NULL,
  variant       TEXT,
  category      TEXT,
  gtins         TEXT[] NOT NULL DEFAULT '{}',
  descriptor    TEXT NOT NULL,
  embedding     vector(1024),
  embedding_model   TEXT,
  embedding_version TEXT,
  origin        origin_t NOT NULL DEFAULT 'live_lookup',
  verified      BOOLEAN NOT NULL DEFAULT false,
  scan_count    INTEGER NOT NULL DEFAULT 0,
  -- General product info gathered from the same tier-3 web search, kept
  -- strictly separate from test_results: never a verdict, never presented
  -- as an Öko-Test finding. {description, features[], source_urls[]}.
  general_info  JSONB,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX products_embedding_hnsw ON products
  USING hnsw (embedding vector_cosine_ops);

-- one row per GTIN so "unique per GTIN" (spec) is enforced without a second table
CREATE TABLE product_gtins (
  gtin        TEXT PRIMARY KEY,
  product_id  TEXT NOT NULL REFERENCES products(product_id) ON DELETE CASCADE
);

CREATE TABLE test_results (
  result_id             TEXT PRIMARY KEY,       -- 'res_' + ULID
  product_id            TEXT NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
  verdict               verdict_t NOT NULL,
  test_date             TEXT,                   -- 'YYYY-MM'
  issue                 TEXT,
  test_name             TEXT,
  findings              JSONB NOT NULL DEFAULT '[]',
  source_urls           TEXT[] NOT NULL DEFAULT '{}',
  extraction_model      TEXT,
  extraction_confidence FLOAT,
  origin                origin_t NOT NULL DEFAULT 'live_lookup',
  is_current            BOOLEAN NOT NULL DEFAULT true,
  retrieved_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX test_results_product_id ON test_results(product_id);

CREATE TABLE scans (
  scan_id           TEXT PRIMARY KEY,           -- 'scn_' + ULID
  api_key_id        TEXT NOT NULL REFERENCES api_keys(key_id),
  image_sha256      TEXT NOT NULL,
  status            scan_status_t NOT NULL DEFAULT 'queued',
  resolution_tier   resolution_tier_t,
  resolution_reason TEXT,
  product_id        TEXT REFERENCES products(product_id),
  confidence        FLOAT,
  hint              TEXT,
  locale            TEXT NOT NULL DEFAULT 'de-DE',
  webhook_url       TEXT,
  allow_live_lookup BOOLEAN NOT NULL DEFAULT true,
  context           JSONB,
  pipeline          JSONB NOT NULL DEFAULT '{}',
  warnings          JSONB NOT NULL DEFAULT '[]',
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at      TIMESTAMPTZ
);

CREATE INDEX scans_image_sha256 ON scans(image_sha256);
CREATE INDEX scans_api_key_created ON scans(api_key_id, created_at);

CREATE TABLE feedback (
  feedback_id  TEXT PRIMARY KEY,                -- 'fbk_' + ULID
  scan_id      TEXT NOT NULL REFERENCES scans(scan_id),
  kind         TEXT NOT NULL,
  payload      JSONB NOT NULL DEFAULT '{}',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at  TIMESTAMPTZ
);

CREATE TABLE review_queue (
  review_id    TEXT PRIMARY KEY,                -- 'rvw_' + ULID
  kind         TEXT NOT NULL,                    -- conflict|dedupe|low_confidence|bad_vocab
  payload      JSONB NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at  TIMESTAMPTZ
);

-- context aggregation (spec §7.7) — counts only, no per-user history
CREATE TABLE context_aggregates (
  product_id   TEXT NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
  bucket_key   TEXT NOT NULL,                    -- e.g. 'intended_use:infant'
  scan_count   INTEGER NOT NULL DEFAULT 0,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (product_id, bucket_key)
);
