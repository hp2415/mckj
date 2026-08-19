from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request
from sqlalchemy.future import select
from models import User
from database import AsyncSessionLocal
from core.auth_throttle import clear_login_failures, login_guard, record_login_failure
from core.security import verify_password
from fastapi import HTTPException
import os
from dotenv import load_dotenv

load_dotenv()

class AdminAuth(AuthenticationBackend):
    async def login(self, request: Request) -> bool:
        form = await request.form()
        username, password = form["username"], form["password"]

        try:
            login_guard(username)
        except HTTPException:
            return False

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(User).where(User.username == username))
            user = result.scalars().first()
            if not user or not verify_password(password, user.password_hash):
                try:
                    record_login_failure(username)
                except HTTPException:
                    pass
                return False
            if user.role != "admin" or not user.is_active:
                return False

        clear_login_failures(username)
        request.session.update({"token": str(user.id)})
        return True

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        token = request.session.get("token")
        if not token:
            return False
        try:
            user_id = int(token)
        except (TypeError, ValueError):
            request.session.clear()
            return False
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(User).where(User.id == user_id))
            user = result.scalars().first()
            if not user or user.role != "admin" or not user.is_active:
                request.session.clear()
                return False
        return True

admin_auth = AdminAuth(secret_key=os.getenv("SECRET_KEY", ""))
