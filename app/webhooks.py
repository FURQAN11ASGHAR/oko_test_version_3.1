import asyncio
import hashlib
import hmac
import json
import os
import time

import httpx

_BACKOFF_S = [1, 4, 16, 64, 256]
_SIGNING_SECRET = os.environ.get("WEBHOOK_SIGNING_SECRET", "")


def _sign(timestamp: int, body: bytes) -> str:
    mac = hmac.new(_SIGNING_SECRET.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


async def deliver(webhook_url: str, event: str, payload: dict) -> None:
    """Fire-and-forget delivery with the documented retry schedule (spec §5).
    Scheduled as an asyncio task; a crash of the whole process loses in-flight
    retries — acceptable for a local single-instance deployment.
    """
    body = json.dumps(payload).encode()
    timestamp = int(time.time())
    headers = {
        "Content-Type": "application/json",
        "X-Okotest-Event": event,
        "X-Okotest-Signature": _sign(timestamp, body),
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        for attempt, delay in enumerate([0, *_BACKOFF_S]):
            if delay:
                await asyncio.sleep(delay)
            try:
                resp = await client.post(webhook_url, content=body, headers=headers)
                if 200 <= resp.status_code < 300:
                    return
            except httpx.HTTPError:
                pass
    # ponytail: exhausted retries are dropped silently; add a dead-letter row
    # in review_queue if webhook reliability becomes a real requirement.
