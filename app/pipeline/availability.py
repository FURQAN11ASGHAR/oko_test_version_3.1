from datetime import datetime, timezone

from app.openrouter import ModelCallError, client
from app.pipeline.search import SearchUnavailable, search_web

NOT_FOUND_MESSAGE = "Could not find a link to buy this product in Europe."
DISCLAIMER = (
    "Availability found via web search, not verified or endorsed by "
    "Öko-Test. Stock, price, and shipping region may have changed."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _not_found(message: str = NOT_FOUND_MESSAGE) -> dict:
    return {"available": False, "message": message, "checked_at": _now_iso()}


async def check_availability(*, brand: str | None, name: str | None, variant: str | None, gtin: str | None) -> dict:
    """Runs AFTER the product is already identified (spec: this feature). Takes
    only clean text fields — never the photo, never the vision model's raw
    output blob — specifically so a vision hallucination can't leak into a
    buy-link claim. Uses its own separate web search + separate LLM call from
    the resolution pipeline's vision/embedding/extraction calls.
    """
    descriptor_text = " ".join(filter(None, [brand, name, variant]))
    if not descriptor_text.strip():
        return _not_found("No product identified to check availability for.")

    # Two queries (EN + DE): Öko-Test's own retail market is Germany, but
    # "buy" doesn't surface German shops well and "kaufen" doesn't surface
    # English/EU-wide ones — merging both gives broader European coverage
    # than either alone.
    try:
        results_en = await search_web(f"{descriptor_text} buy online Europe", limit=6)
        results_de = await search_web(f"{descriptor_text} kaufen", limit=6)
    except SearchUnavailable as e:
        return _not_found(f"Could not search for availability right now: {e}")

    seen: dict[str, dict] = {}
    for r in results_en + results_de:
        seen.setdefault(r["url"], r)
    results = list(seen.values())

    if not results:
        return _not_found()

    try:
        extraction, _ms = await client.check_product_availability(descriptor_text, gtin, results)
    except ModelCallError:
        return _not_found("Could not check availability right now.")

    # Same anti-hallucination rule as the verdict write-back path
    # (orchestrator.py's seen_urls check): a URL is only trustworthy if it is
    # verbatim one of the URLs actually handed to the model, never one it
    # reconstructed or remembered.
    seen_urls = set(seen.keys())
    buy_url = extraction.get("buy_url")
    available = bool(extraction.get("available")) and isinstance(buy_url, str) and buy_url in seen_urls

    if not available:
        return _not_found()

    return {
        "available": True,
        "buy_url": buy_url,
        "retailer": extraction.get("retailer") or None,
        "country_hint": extraction.get("country_hint") or None,
        "checked_at": _now_iso(),
        "disclaimer": DISCLAIMER,
    }
