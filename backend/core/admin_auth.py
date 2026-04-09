from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request
from sqlalchemy.future import select
from models import User
from database import AsyncSessionLocal
from core.security import verify_password
import os
from dotenv import load_dotenv

load_dotenv()

class AdminAuth(AuthenticationBackend):
    async def login(self, request: Request) -> bool:
        form = await request.form()
        username, password = form["username"], form["password"]

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(User).where(User.username == username))
            user = result.scalars().first()
            if not user:
                return False
            if not verify_password(password, user.password_hash):
                return False
            if user.role != "admin" or not user.is_active:
                return False

        # Store a token mapping to cookie session
        request.session.update({"token": str(user.id)})
        return True

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        token = request.session.get("token")
        if not token:
            return False
        return True

admin_auth = AdminAuth(secret_key=os.getenv("SECRET_KEY", ""))
