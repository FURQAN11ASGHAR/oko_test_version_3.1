"""Runnable self-check for the pure logic (no DB/network needed).
Run: python scripts/self_check.py
"""

import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://unused/unused")
sys.path.insert(0, ".")

from app.config import Settings, assert_boot_invariants  # noqa: E402
from app.db import from_pgvector, to_pgvector  # noqa: E402
from app.pipeline.relevance import compute_context_relevance  # noqa: E402
from app.routers.scans import _sniff_mime  # noqa: E402


def check_pgvector_roundtrip():
    vec = [0.1, -0.25, 3.0]
    assert from_pgvector(to_pgvector(vec)) == vec, "pgvector text round-trip must be lossless"
    assert from_pgvector(None) is None


def check_mime_sniff():
    assert _sniff_mime(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert _sniff_mime(b"\x89PNG\r\n\x1a\nrest") == "image/png"
    assert _sniff_mime(b"RIFF0000WEBPrest") == "image/webp"
    assert _sniff_mime(b"not an image") is None


def check_boot_invariants():
    ok = Settings(embedding_dim=1024, scan_rate_limit_per_min=4, model_calls_per_scan_max=4, upstream_rpm_allowance=20)
    assert_boot_invariants(ok)  # must not raise

    bad_dim = Settings(embedding_dim=768)
    try:
        assert_boot_invariants(bad_dim)
        raise AssertionError("expected RuntimeError for mismatched EMBEDDING_DIM")
    except RuntimeError:
        pass

    bad_rate = Settings(scan_rate_limit_per_min=60, model_calls_per_scan_max=4, upstream_rpm_allowance=20)
    try:
        assert_boot_invariants(bad_rate)
        raise AssertionError("expected RuntimeError for scan rate exceeding upstream allowance")
    except RuntimeError:
        pass


def check_relevance_never_touches_notes():
    findings = [{"code": "halogenorganic_compounds", "label": "x", "source_url": "https://oekotest.de/a"}]
    context = {"intended_use": ["infant"], "application": "skin_leave_on", "notes": "medical details"}
    out = compute_context_relevance(findings, context)
    assert "notes" not in out["matched_context"], "notes must never enter relevance matching (spec §7.2)"
    assert out["escalated"] is True
    assert out["disclaimer_required"] is True
    assert any(f["relevance"] == "high" for f in out["relevant_findings"])


if __name__ == "__main__":
    check_pgvector_roundtrip()
    check_mime_sniff()
    check_boot_invariants()
    check_relevance_never_touches_notes()
    print("self_check: all checks passed")
