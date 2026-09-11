from pathlib import Path

import yaml

_VOCAB_DIR = Path(__file__).resolve().parent.parent.parent / "vocabularies"

_findings = yaml.safe_load((_VOCAB_DIR / "findings_v1.yaml").read_text())
_relevance = yaml.safe_load((_VOCAB_DIR / "relevance_v1.yaml").read_text())

FINDINGS_VERSION: str = _findings["version"]
FINDINGS_CODES: dict = _findings["codes"]
RELEVANCE_VERSION: str = _relevance["version"]
RELEVANCE_MAP: dict = _relevance["map"]

_DEFERRAL_CONTEXTS = {"infant", "pregnancy", "breastfeeding"}


def valid_finding_code(code: str) -> bool:
    return code in FINDINGS_CODES


def _context_keys(context: dict) -> list[str]:
    keys = list(context.get("intended_use") or [])
    for field in ("application", "frequency", "environment"):
        if context.get(field):
            keys.append(context[field])
    return keys


def compute_context_relevance(findings: list[dict], context: dict) -> dict:
    """Static table lookup only (spec §7.4) — never an LLM judgment per request."""
    keys = _context_keys(context)
    relevant = []
    escalated = False
    for finding in findings:
        rules = RELEVANCE_MAP.get(finding["code"], {})
        for key in keys:
            rule = rules.get(key)
            if not rule:
                continue
            relevant.append(
                {
                    "code": finding["code"],
                    "label": finding.get("label", FINDINGS_CODES.get(finding["code"], {}).get("label")),
                    "relevance": rule["relevance"],
                    "why": key,
                    "source_url": finding.get("source_url"),
                }
            )
            if rule.get("escalate"):
                escalated = True

    requires_deferral = escalated and bool(_DEFERRAL_CONTEXTS & set(context.get("intended_use") or []))

    return {
        "matched_context": {
            k: v
            for k, v in context.items()
            if k != "notes" and v  # notes never enters relevance matching (spec §7.2)
        },
        "relevant_findings": relevant,
        "escalated": escalated,
        "relevance_map_version": RELEVANCE_VERSION,
        "disclaimer_required": requires_deferral,
    }
