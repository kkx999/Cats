import hashlib
import hmac
import secrets
import time
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import LoginChallenge, User

COOKIE_NAME = "miaobot_session"


def hash_login_code(code: str) -> str:
    return hmac.new(settings.login_code_pepper.encode(), code.encode(), hashlib.sha256).hexdigest()


def new_login_code() -> str:
    return f"{secrets.randbelow(900000) + 100000:06d}"


def sign_session(user_id: int, expires_at: int | None = None) -> str:
    expires_at = expires_at or int(time.time()) + 60 * 60 * 24 * 30
    payload = f"{user_id}.{expires_at}"
    signature = hmac.new(settings.app_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def read_session(value: str | None) -> int | None:
    if not value:
        return None
    try:
        user_raw, expires_raw, signature = value.split(".", 2)
        payload = f"{user_raw}.{expires_raw}"
        expected = hmac.new(
            settings.app_secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected) or int(expires_raw) < int(time.time()):
            return None
        return int(user_raw)
    except (TypeError, ValueError):
        return None


async def current_user(request: Request, session: AsyncSession) -> User:
    user_id = read_session(request.cookies.get(COOKIE_NAME))
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    user = await session.get(User, user_id)
    if not user or user.blocked:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号不可用")
    return user


def is_superadmin(user_id: int) -> bool:
    return user_id in settings.superadmin_ids


async def create_challenge(session: AsyncSession) -> tuple[LoginChallenge, str]:
    now = datetime.now(UTC)
    for _ in range(8):
        code = new_login_code()
        code_hash = hash_login_code(code)
        existing = await session.scalar(
            select(LoginChallenge.id).where(
                LoginChallenge.code_hash == code_hash,
                LoginChallenge.expires_at > now,
            )
        )
        if not existing:
            break
    else:
        raise RuntimeError("无法生成唯一验证码")

    challenge = LoginChallenge(
        code_hash=code_hash,
        access_token=secrets.token_urlsafe(36),
        expires_at=now + timedelta(seconds=settings.login_ttl_seconds),
    )
    session.add(challenge)
    await session.commit()
    return challenge, code
