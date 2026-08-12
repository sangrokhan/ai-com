from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from aicom.auth.password import SESSION_COOKIE, check_password, issue_session
from aicom.config import Settings


class LoginBody(BaseModel):
    password: str


def make_auth_router(settings: Settings) -> APIRouter:
    router = APIRouter()

    @router.post("/auth/login")
    def login(body: LoginBody, response: Response) -> dict:
        if not check_password(body.password, settings.console_password):
            # No detail about whether a password is even configured.
            raise HTTPException(status_code=401)
        response.set_cookie(
            SESSION_COOKIE,
            issue_session(settings.session_secret),
            httponly=True,
            samesite="lax",
            secure=settings.session_cookie_secure,
            max_age=settings.session_max_age_seconds,
        )
        return {"status": "ok"}

    @router.post("/auth/logout")
    def logout(response: Response) -> dict:
        # This only clears the cookie on the client that calls it. The token
        # itself is a signed, stateless credential with no server-side record,
        # so it remains valid (for anyone still holding it) until it expires
        # on its own after session_max_age_seconds. Real revocation would
        # require server-side session state, which this task does not add.
        response.delete_cookie(SESSION_COOKIE)
        return {"status": "ok"}

    return router
