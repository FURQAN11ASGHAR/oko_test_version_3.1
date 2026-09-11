import asyncio
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx
from ddgs import DDGS

from app.config import settings

_PAGE_TEXT_CHARS = 4000


class SearchUnavailable(Exception):
    pass


class _TextExtractor(HTMLParser):
    """Minimal HTML-to-text: drops script/style content, keeps everything else."""

    def __init__(self):
        super().__init__()
        self._skip = False
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            stripped = data.strip()
            if stripped:
                self.chunks.append(stripped)


def _search_sync(query: str, limit: int) -> list[dict]:
    return DDGS().text(query, max_results=limit)


async def search_web(query: str, *, limit: int = 8) -> list[dict]:
    """Tier-3 web search via ddgs (DuckDuckGo) — no API key required.
    ddgs is a blocking library, so it runs in a worker thread to avoid
    stalling the event loop.
    """
    try:
        raw = await asyncio.to_thread(_search_sync, query, limit)
    except Exception as e:
        raise SearchUnavailable(str(e))
    return [{"url": r["href"], "title": r.get("title", ""), "snippet": r.get("body", "")} for r in raw]


async def fetch_page_text(url: str) -> str | None:
    """Best-effort full-page text for trusted-domain results — a search
    snippet rarely contains the actual verdict, which lives in the article
    body. Returns None on any failure; callers should fall back to the
    snippet rather than treat this as fatal.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as http:
            resp = await http.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code >= 400:
            return None
        parser = _TextExtractor()
        parser.feed(resp.text)
        return " ".join(parser.chunks)[:_PAGE_TEXT_CHARS]
    except (httpx.HTTPError, UnicodeDecodeError):
        return None


def is_trusted(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return any(host == d or host.endswith(f".{d}") for d in settings.trusted_source_domains)
