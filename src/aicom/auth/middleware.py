from collections.abc import Awaitable, Callable
from pathlib import Path

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


def is_static_bundle_request(method: str, path: str, static_dir: Path) -> bool:
    """True if this GET/HEAD names a real file inside the built console bundle.

    The bundle is the SPA shell (`index.html`) plus its JS/CSS -- login form and
    application code, no data -- so it is safe to serve without a session; without
    this exemption a cookie-less browser can never even load the login form (it
    would need a cookie to fetch the page that is the only way to obtain one).

    Deliberately filesystem-backed rather than a path-prefix rule such as
    `path.startswith("/assets/")`: a prefix rule would silently exempt any future
    API route that happened to live under the same prefix, defeating the point of
    "every route is protected by default". This instead resolves the request path
    against `static_dir` and only exempts it if that exact file exists there (and
    stays inside `static_dir` -- `..` cannot escape it), so a same-shaped path that
    is actually a route, not a built asset, is still gated by the session check.
    """
    if method not in ("GET", "HEAD"):
        return False
    if not static_dir.is_dir():
        return False
    relative = "index.html" if path == "/" else path.lstrip("/")
    if not relative:
        return False
    static_root = static_dir.resolve()
    candidate = (static_root / relative).resolve()
    try:
        candidate.relative_to(static_root)
    except ValueError:
        return False  # would escape static_dir (path traversal)
    return candidate.is_file()


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(
        self, app: object, *, secret: str, max_age_seconds: int, static_dir: Path
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._secret = secret
        self._max_age = max_age_seconds
        self._static_dir = static_dir

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)
        if is_static_bundle_request(request.method, request.url.path, self._static_dir):
            return await call_next(request)
        token = request.cookies.get(SESSION_COOKIE, "")
        if not verify_session(token, self._secret, self._max_age):
            return Response(status_code=401)
        return await call_next(request)
