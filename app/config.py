import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path = Path(__file__).resolve().parent.parent / ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines, existing env vars win. Avoids
    adding python-dotenv for something this small."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


def _list(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [v.strip() for v in raw.split(",") if v.strip()]


@dataclass(frozen=True)
class Settings:
    database_url: str = os.environ["DATABASE_URL"]

    openrouter_api_key: str = os.environ.get("OPENROUTER_API_KEY", "")
    openrouter_base_url: str = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    vision_model: str = os.environ.get("VISION_MODEL", "minimax/minimax-m3")
    # spec's liquid/lfm-2.5-embedding-350m does not exist on OpenRouter (verified
    # against /api/v1/models and /api/v1/embeddings). baai/bge-m3 is the closest
    # match still available: 1024-dim (matches vector(1024) below, no migration
    # needed) and multilingual, which German product names need.
    embedding_model: str = os.environ.get("EMBEDDING_MODEL", "baai/bge-m3")
    extraction_model: str = os.environ.get("EXTRACTION_MODEL", "minimax/minimax-m3")
    # Deliberately a separate call/model slot from vision+extraction (see
    # app/pipeline/availability.py) — defaults to EXTRACTION_MODEL so no new
    # env var is required, but can be pointed at a different model.
    availability_model: str = os.environ.get("AVAILABILITY_MODEL", os.environ.get("EXTRACTION_MODEL", "minimax/minimax-m3"))
    model_fallbacks: list[str] = field(default_factory=lambda: _list("MODEL_FALLBACKS"))

    embedding_dim: int = int(os.environ.get("EMBEDDING_DIM", "1024"))
    vector_match_threshold: float = float(os.environ.get("VECTOR_MATCH_THRESHOLD", "0.82"))
    write_back_min_confidence: float = float(os.environ.get("WRITE_BACK_MIN_CONFIDENCE", "0.75"))
    untested_recheck_days: int = int(os.environ.get("UNTESTED_RECHECK_DAYS", "90"))
    dedupe_review_threshold: float = float(os.environ.get("DEDUPE_REVIEW_THRESHOLD", "0.95"))
    scan_retention_days: int = int(os.environ.get("SCAN_RETENTION_DAYS", "30"))
    image_max_bytes: int = int(os.environ.get("IMAGE_MAX_BYTES", str(10 * 1024 * 1024)))

    # oekotest.de is intrinsic to what this API does, not a per-deployment
    # choice — an empty/misconfigured env var must not silently disable every
    # tier-3 write-back (is_trusted() would reject everything, incl. genuine
    # oekotest.de URLs, and nothing would ever surface as an error).
    trusted_source_domains: list[str] = field(
        default_factory=lambda: _list("TRUSTED_SOURCE_DOMAINS", "oekotest.de,www.oekotest.de")
    )

    # Was 4 (vision + embedding + extraction + one fallback retry). The
    # availability check (app/pipeline/availability.py) now runs on every
    # resolved/identified scan — including tier 0-2 hits that previously made
    # zero model calls — adding one more call. Bumped to 5 to keep the boot
    # invariant below honest about real upstream load.
    model_calls_per_scan_max: int = int(os.environ.get("MODEL_CALLS_PER_SCAN_MAX", "5"))
    upstream_rpm_allowance: int = int(os.environ.get("UPSTREAM_RPM_ALLOWANCE", "20"))

    scan_rate_limit_per_min: int = int(os.environ.get("SCAN_RATE_LIMIT_PER_MIN", "4"))
    scan_rate_limit_per_day: int = int(os.environ.get("SCAN_RATE_LIMIT_PER_DAY", "500"))
    live_lookup_rate_limit_per_day: int = int(os.environ.get("LIVE_LOOKUP_RATE_LIMIT_PER_DAY", "200"))
    read_rate_limit_per_min: int = int(os.environ.get("READ_RATE_LIMIT_PER_MIN", "600"))

    # the PHP site's browser JS calls this API cross-origin. "*" is fine for
    # local dev; set to the real site origin(s) before going live — a scan
    # endpoint behind X-API-Key plus a wildcard origin lets any page that has
    # (or guesses) a key drive it from a user's browser.
    cors_allowed_origins: list[str] = field(default_factory=lambda: _list("CORS_ALLOWED_ORIGINS", "*"))


settings = Settings()


def assert_boot_invariants(s: Settings = settings) -> None:
    """Fail fast rather than degrade silently in production (spec §9.1, §10)."""
    if s.embedding_dim != 1024:
        raise RuntimeError(
            f"EMBEDDING_DIM={s.embedding_dim} does not match the vector(1024) "
            "column fixed by liquid/lfm-2.5-embedding-350m. Re-embedding "
            "migration required (spec §6.5) before changing this."
        )
    upstream_needed = s.scan_rate_limit_per_min * s.model_calls_per_scan_max
    if upstream_needed > s.upstream_rpm_allowance:
        raise RuntimeError(
            f"SCAN_RATE_LIMIT_PER_MIN ({s.scan_rate_limit_per_min}) x "
            f"MODEL_CALLS_PER_SCAN_MAX ({s.model_calls_per_scan_max}) = "
            f"{upstream_needed} exceeds UPSTREAM_RPM_ALLOWANCE "
            f"({s.upstream_rpm_allowance}). Lower the scan rate limit, move "
            "vision/extraction off :free endpoints, or raise the allowance "
            "(spec §1.2, §9.1)."
        )