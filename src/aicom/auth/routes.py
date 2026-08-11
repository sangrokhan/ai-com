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
        response.delete_cookie(SESSION_COOKIE)
        return {"status": "ok"}

    return router
