"""Domain errors and their HTTP mapping."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from backend.core.logging import get_logger

log = get_logger(__name__)


class AppError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None,
                 **extra: Any) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        self.extra = extra


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"


class NotConfigured(AppError):
    status_code = 501
    code = "not_configured"


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"


class PermissionDenied(AppError):
    """An agent tried to use a tool or path outside its permissions."""

    status_code = 403
    code = "permission_denied"


class SandboxUnavailable(AppError):
    status_code = 503
    code = "sandbox_unavailable"


class BudgetExceeded(AppError):
    status_code = 429
    code = "budget_exceeded"


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        body: dict[str, Any] = {"detail": exc.message, "code": exc.code}
        body.update(exc.extra)
        headers = {}
        if isinstance(exc, RateLimited) and "retry_after" in exc.extra:
            headers["Retry-After"] = str(int(exc.extra["retry_after"]))
        return JSONResponse(status_code=exc.status_code, content=body, headers=headers)
