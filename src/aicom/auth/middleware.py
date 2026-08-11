from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from aicom.auth.password import SESSION_COOKIE, verify_session

# Everything else requires a session. Exemptions are deliberate and few:
# the Slack webhook verifies its own signature and cannot present a cookie,
# the login endpoint is how a cookie is obtained, and the healthcheck is
# called by the container runtime.
EXEMPT_PATHS: frozenset[str] = frozenset(
    {"/slack/interactions", "/auth/login", "/health"}
)


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: object, *, secret: str, max_age_seconds: int) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._secret = secret
        self._max_age = max_age_seconds

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)
        token = request.cookies.get(SESSION_COOKIE, "")
        if not verify_session(token, self._secret, self._max_age):
            return Response(status_code=401)
        return await call_next(request)
