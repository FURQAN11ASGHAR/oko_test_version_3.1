from fastapi import Request
from fastapi.responses import JSONResponse
from ulid import ULID


class ApiError(Exception):
    """Uniform error envelope per spec §8."""

    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    headers = {}
    if exc.code == "rate_limited" and "retry_after" in exc.details:
        headers["Retry-After"] = str(exc.details["retry_after"])
        if "limit" in exc.details:
            headers["X-RateLimit-Limit"] = str(exc.details["limit"])
            headers["X-RateLimit-Remaining"] = "0"
        if "reset" in exc.details:
            headers["X-RateLimit-Reset"] = str(exc.details["reset"])
    return JSONResponse(
        status_code=exc.status,
        headers=headers,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "request_id": f"req_{ULID()}",
                "details": exc.details,
            }
        },
    )


# Convenience constructors for the codes enumerated in spec §8
def invalid_request(message: str, **details) -> ApiError:
    return ApiError(400, "invalid_request", message, details)


def unauthenticated() -> ApiError:
    return ApiError(401, "unauthenticated", "Missing or invalid API key.")


def insufficient_scope(scope: str) -> ApiError:
    return ApiError(403, "insufficient_scope", f"API key lacks the '{scope}' scope.")


def not_found(kind: str) -> ApiError:
    return ApiError(404, "not_found", f"Unknown {kind}.")


def rate_limited(retry_after: int, *, limit: int | None = None, reset: int | None = None) -> ApiError:
    details = {"retry_after": retry_after}
    if limit is not None:
        details["limit"] = limit
    if reset is not None:
        details["reset"] = reset
    return ApiError(429, "rate_limited", "Rate limit exceeded.", details)
