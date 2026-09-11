import base64
import time
from typing import Literal

import httpx

from app.config import settings
from app.pipeline.relevance import FINDINGS_CODES


class ModelCallError(Exception):
    def __init__(self, message: str, upstream_status: int | None = None):
        super().__init__(message)
        self.upstream_status = upstream_status


class OpenRouterClient:
    def __init__(self):
        headers = {"Authorization": f"Bearer {settings.openrouter_api_key}"} if settings.openrouter_api_key else {}
        # spec's own tier-3 estimate is ~30s total; a stuck provider that eats
        # 60s before falling back to the next candidate can blow that budget
        # by itself. 25s lets one bad attempt fail and fall back within the
        # advertised window instead of doubling it.
        self._client = httpx.AsyncClient(base_url=settings.openrouter_base_url, headers=headers, timeout=25.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _chat(self, model: str, messages: list[dict], **kwargs) -> tuple[dict, int]:
        """POST /chat/completions, falling back through MODEL_FALLBACKS on failure."""
        last_err: Exception | None = None
        for candidate in [model, *settings.model_fallbacks]:
            started = time.monotonic()
            try:
                resp = await self._client.post(
                    "/chat/completions",
                    json={"model": candidate, "messages": messages, **kwargs},
                )
                if resp.status_code >= 400:
                    last_err = ModelCallError(resp.text, resp.status_code)
                    continue
                return resp.json(), int((time.monotonic() - started) * 1000)
            except httpx.HTTPError as e:
                last_err = ModelCallError(str(e))
        raise last_err or ModelCallError("all model candidates failed")

    async def vision_identify(self, image_bytes: bytes, mime: str, hint: str | None) -> tuple[dict, int, int]:
        """Returns (descriptor dict, ms, tokens). Raises ModelCallError on failure."""
        b64 = base64.b64encode(image_bytes).decode()
        tool = {
            "type": "function",
            "function": {
                "name": "identify_product",
                "description": "Identify the product photographed.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "brand": {"type": "string"},
                        "name": {"type": "string"},
                        "variant": {"type": "string"},
                        "category": {"type": "string"},
                        "legible": {"type": "boolean"},
                        "brand_confidence": {"type": "number", "description": "0-1, how confident you are in `brand`. 0 if not set."},
                        "name_confidence": {"type": "number", "description": "0-1, how confident you are in `name`. 0 if not set."},
                        "variant_confidence": {"type": "number", "description": "0-1, how confident you are in `variant`. 0 if not set."},
                        "other_products": {
                            "type": "array",
                            "description": "If the photo shows more than one distinct, roughly equally prominent product (e.g. a shelf or several items side by side), one entry per product OTHER than the primary one above. Leave empty for a single product, or when one product is clearly the main subject and others are just incidentally in frame.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "brand": {"type": "string"},
                                    "name": {"type": "string"},
                                    "category": {"type": "string"},
                                },
                            },
                        },
                        "is_test_report": {
                            "type": "boolean",
                            "description": "True if this photo shows an Öko-Test magazine cover, article, or webpage about a test — not a purchasable product itself.",
                        },
                        "test_subject": {
                            "type": "string",
                            "description": "Only if is_test_report: the topic being tested, e.g. 'Olivenöl', read from the cover/headline text.",
                        },
                    },
                    "required": ["legible", "brand_confidence", "name_confidence", "variant_confidence", "other_products"],
                },
            },
        }
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": self._vision_prompt(hint)},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }
        ]
        data, ms = await self._chat(
            settings.vision_model,
            messages,
            tools=[tool],
            tool_choice={"type": "function", "function": {"name": "identify_product"}},
            max_tokens=768,
            reasoning={"enabled": False},  # chain-of-thought otherwise eats the whole max_tokens budget before the tool call
        )
        tokens = data.get("usage", {}).get("total_tokens", 0)
        try:
            args = _extract_tool_args(data, "identify_product")
        except ModelCallError:
            # tool_choice isn't reliably enforced by every provider (spec §1.2:
            # "response_format... without JSON-schema enforcement" — tool_choice
            # can be just as loose in practice). A model that answers in prose
            # instead of calling the tool found nothing structured to report,
            # which is functionally the same as an unreadable image.
            args = {"legible": False}
        return args, ms, tokens

    @staticmethod
    def _vision_prompt(hint: str | None) -> str:
        base = (
            "Identify the single most prominent consumer product in this photo. "
            "Return brand, name, variant (size/scent/colourway) and category if "
            "legible, plus your confidence (0-1) in each of brand/name/variant. "
            "Do not read barcodes — a separate decoder handles that. Set "
            "legible=false if the product cannot be confidently identified. If "
            "the photo shows several distinct, similarly prominent products "
            "(e.g. a shelf), list the ones other than your primary pick in "
            "other_products — leave it empty for a single product or when one "
            "is clearly the main subject. If the photo is instead of an "
            "Öko-Test magazine cover, article, or webpage about a test (not a "
            "purchasable product), set is_test_report=true and test_subject to "
            "the topic being tested."
        )
        return f"{base}\nUser-supplied hint: {hint}" if hint else base

    async def embed(self, text: str, *, kind: Literal["query", "document"]) -> tuple[list[float], int]:
        """kind is required and unguessable — see spec §1.2 on the prefix bug."""
        started = time.monotonic()
        try:
            resp = await self._client.post(
                "/embeddings", json={"model": settings.embedding_model, "input": f"{kind}: {text}"}
            )
        except httpx.HTTPError as e:
            raise ModelCallError(str(e))
        if resp.status_code >= 400:
            raise ModelCallError(resp.text, resp.status_code)
        data = resp.json()
        vec = data["data"][0]["embedding"]
        return vec, int((time.monotonic() - started) * 1000)

    async def extract_test_result(
        self, search_results: list[dict], product_descriptor: str, *, is_test_report: bool = False
    ) -> tuple[dict | None, int]:
        tool = {
            "type": "function",
            "function": {
                "name": "extract_test_result",
                "description": "Extract a structured Öko-Test result from search results, plus separate general product info. Only recognized verdict values allowed; never invent a source URL.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "found": {"type": "boolean"},
                        "verdict": {
                            "type": "string",
                            "enum": ["sehr_gut", "gut", "befriedigend", "ausreichend", "mangelhaft", "ungenuegend"],
                        },
                        "test_date": {"type": "string", "description": "YYYY-MM"},
                        "issue": {"type": "string"},
                        "test_name": {"type": "string"},
                        "source_urls": {"type": "array", "items": {"type": "string"}},
                        "confidence": {"type": "number"},
                        "general_description": {
                            "type": "string",
                            "description": "Neutral description from the search results only — for a single product, 1-2 sentences; for a test report, a fuller summary of what the test covered and found. Not an Öko-Test verdict.",
                        },
                        "general_features": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Factual bullet points found in the search results — product facts (ingredients, use, size, claims) or, for a test report, facts about the test itself (scope, methodology, named products, headline results). Not an Öko-Test verdict.",
                        },
                        "general_source_urls": {"type": "array", "items": {"type": "string"}},
                        "findings": {
                            "type": "array",
                            "description": "Substance groups or deductions the test itself reported as impacting the verdict — only from the closed vocabulary, only if explicitly stated in the results. Leave empty rather than guess.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "code": {"type": "string", "enum": list(FINDINGS_CODES.keys())},
                                    "as_reported": {"type": "string", "description": "The finding as the test stated it, e.g. 'nachgewiesen'."},
                                    "deduction": {"type": "string", "enum": ["verdict_impacting", "informational"]},
                                    "source_url": {"type": "string"},
                                },
                                "required": ["code", "deduction"],
                            },
                        },
                        "products": {
                            "type": "array",
                            "description": "Only one entry per specific, individually-named, commercially available product with its own verdict tied to it in the results — never an entry representing the test, category, or an aggregate ('X of Y products failed'). Leave empty rather than guess.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "brand": {"type": "string"},
                                    "name": {"type": "string"},
                                    "variant": {"type": "string"},
                                    "verdict": {
                                        "type": "string",
                                        "enum": ["sehr_gut", "gut", "befriedigend", "ausreichend", "mangelhaft", "ungenuegend"],
                                    },
                                    "confidence": {"type": "number"},
                                    "source_urls": {"type": "array", "items": {"type": "string"}},
                                    "buy_links": {"type": "array", "items": {"type": "string"}},
                                },
                                "required": ["brand", "name", "verdict", "confidence"],
                            },
                        },
                    },
                    # general_description/general_features are required (not just
                    # documented) because a tool-calling model reliably fills
                    # required fields and just as reliably skips optional ones
                    # once found=false — observed dropping them entirely even
                    # when the search results had plenty to say about the
                    # product. Empty string/array is a valid value; omission isn't.
                    "required": ["found", "confidence", "general_description", "general_features"],
                },
            },
        }
        sources_text = "\n\n".join(f"[{r['url']}]\n{r.get('title','')}\n{r.get('snippet','')}" for r in search_results)
        report_instructions = (
            "This is a photo of an Öko-Test report/magazine covering multiple "
            "products, not a single purchasable product. List every distinct "
            "product you can confidently identify from the results in "
            "`products`, each with its own verdict and source_urls. Set "
            "found=true and leave the top-level verdict fields empty if there "
            "is no single primary product. "
            if is_test_report
            else ""
        )
        messages = [
            {
                "role": "user",
                "content": (
                    f"{report_instructions}Extract the Öko-Test verdict for this "
                    "product from the search results below. Only use information "
                    "present in the results; never invent a URL or a verdict. In "
                    "source_urls, cite the oekotest.de URL(s) if one appears in "
                    "the results — a third-party article reporting on the same "
                    "test is not a substitute and should only be cited if no "
                    "oekotest.de URL is present. If no Öko-Test result is "
                    "present, set found=false. If the test itself reported a "
                    "specific substance/deduction from the closed `findings` "
                    "vocabulary as impacting the verdict, list it in `findings` "
                    "with the source_url — leave findings empty rather than "
                    "guess at a code that doesn't clearly match.\n\n"
                    + (
                        "Separately, regardless of whether you could list individual "
                        "products above, ALWAYS fill general_description and "
                        "general_features with everything the results say about this "
                        "test: what it covered, how many products, the headline "
                        "finding (e.g. 'only one rated sehr gut'), methodology, "
                        "notable named products even without a full verdict — pull "
                        "from any source, not just oekotest.de. This is what gets "
                        "shown to the user, so be as complete as the results allow. "
                        "Only leave it empty if the results truly say nothing about "
                        "this test.\n\n"
                        if is_test_report
                        else "Separately, regardless of whether an Öko-Test verdict was "
                        "found, fill general_description and general_features with "
                        "neutral factual information — pull from any source in the "
                        "results, not just oekotest.de — using only what's in the "
                        "results, never invent a fact. This is general product info, "
                        "not a substitute for a missing verdict; leave it empty if "
                        "the results say nothing about the product itself.\n\n"
                    )
                    + f"Product: {product_descriptor}\n\n{sources_text}"
                ),
            }
        ]
        data, ms = await self._chat(
            settings.extraction_model,
            messages,
            tools=[tool],
            tool_choice={"type": "function", "function": {"name": "extract_test_result"}},
            # general_description/general_features are required and can run
            # 300-400 tokens on their own; 512 was truncating the JSON mid-
            # generation and silently falling back to the bare-minimum shape.
            max_tokens=1536 if is_test_report else 1024,
            reasoning={"enabled": False},
        )
        try:
            args = _extract_tool_args(data, "extract_test_result")
        except ModelCallError:
            # same tool_choice non-compliance as vision (see vision_identify).
            # No structured answer means nothing safe to write back — treat as
            # "no result found" rather than failing the whole scan.
            args = {"found": False, "confidence": 0.0}
        return args, ms

    async def check_product_availability(
        self, product_descriptor: str, gtin: str | None, search_results: list[dict]
    ) -> tuple[dict, int]:
        """Deliberately separate from vision_identify/extract_test_result: takes
        only clean product text (never the photo, never the raw vision output)
        so a vision hallucination can't leak into a buy-link claim. Only ever
        called with results from a real web search already run by the caller.
        """
        tool = {
            "type": "function",
            "function": {
                "name": "check_availability",
                "description": (
                    "Given real web search results, determine whether this exact "
                    "product can currently be bought somewhere in Europe. Only "
                    "use a URL that is copied verbatim, character for character, "
                    "from the search results below — never invent, guess, or "
                    "reconstruct a URL, even a plausible one."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "available": {"type": "boolean"},
                        "buy_url": {
                            "type": "string",
                            "description": "Copied exactly from one of the result URLs below. Omit entirely if not available.",
                        },
                        "retailer": {"type": "string", "description": "The store/site name, e.g. 'Rossmann', 'Amazon.de'."},
                        "country_hint": {
                            "type": "string",
                            "description": "Best-effort ISO 3166-1 alpha-2 country code for the retailer, e.g. DE, FR, NL. Leave empty if unclear.",
                        },
                        "confidence": {"type": "number"},
                    },
                    "required": ["available", "confidence"],
                },
            },
        }
        sources_text = "\n\n".join(f"[{r['url']}]\n{r.get('title', '')}\n{r.get('snippet', '')}" for r in search_results)
        gtin_line = f"\nGTIN/barcode: {gtin}" if gtin else ""
        messages = [
            {
                "role": "user",
                "content": (
                    "Below are web search results for a specific product. Decide "
                    "whether it can currently be bought somewhere in Europe. Set "
                    "available=true only if one of the results is an actual "
                    "product/retailer page selling this specific item (not a "
                    "review, news article, or an out-of-stock/discontinued "
                    "notice) shipping to or based in Europe. If none of the "
                    "results is a real purchase page for this product, set "
                    "available=false and omit buy_url entirely — do not guess a "
                    "plausible-looking URL.\n\n"
                    f"Product: {product_descriptor}{gtin_line}\n\n{sources_text}"
                ),
            }
        ]
        data, ms = await self._chat(
            settings.availability_model,
            messages,
            tools=[tool],
            tool_choice={"type": "function", "function": {"name": "check_availability"}},
            max_tokens=512,
            reasoning={"enabled": False},
        )
        try:
            args = _extract_tool_args(data, "check_availability")
        except ModelCallError:
            args = {"available": False, "confidence": 0.0}
        return args, ms


def _extract_tool_args(data: dict, fn_name: str) -> dict:
    import json
    import re

    try:
        choice = data["choices"][0]["message"]
    except (KeyError, IndexError):
        raise ModelCallError(f"model did not return a valid {fn_name} tool call")

    calls = choice.get("tool_calls") or []
    for call in calls:
        try:
            if call["function"]["name"] == fn_name:
                return json.loads(call["function"]["arguments"])
        except (KeyError, json.JSONDecodeError):
            continue

    # tool_choice isn't reliably enforced by every provider: the model can
    # still answer in prose instead of a real tool call. Observed verbatim —
    # a complete, correct answer as a ```json fenced block in `content`,
    # discarded entirely because only tool_calls was checked. Recover it
    # rather than throw away a fully valid response.
    content = choice.get("content")
    if isinstance(content, str) and content.strip():
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

    raise ModelCallError(f"model did not return a valid {fn_name} tool call")


client = OpenRouterClient()