import json
import os
import sys

from cryptography.fernet import Fernet


class LoginCredentialStore:
    """本地记住登录账号密码（Fernet 加密，与 SecureStorage 共用 secret.key）。"""

    _FILENAME = "saved_login.dat"

    def __init__(self):
        if getattr(sys, "frozen", False):
            base_dir = os.path.dirname(sys.executable)
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))
        self.cache_root = os.path.join(base_dir, "desktop_cache")
        self.key_file = os.path.join(self.cache_root, "secret.key")
        self.data_file = os.path.join(self.cache_root, self._FILENAME)
        self._fernet = self._build_cipher()

    def _build_cipher(self) -> Fernet:
        os.makedirs(self.cache_root, exist_ok=True)
        if not os.path.exists(self.key_file):
            key = Fernet.generate_key()
            with open(self.key_file, "wb") as f:
                f.write(key)
        else:
            with open(self.key_file, "rb") as f:
                key = f.read()
        return Fernet(key)

    def load(self) -> dict | None:
        if not os.path.exists(self.data_file):
            return None
        try:
            with open(self.data_file, "rb") as f:
                raw = self._fernet.decrypt(f.read())
            data = json.loads(raw.decode("utf-8"))
            username = (data.get("username") or "").strip()
            password = data.get("password") or ""
            if not username or not password:
                return None
            return {"username": username, "password": password}
        except Exception:
            return None

    def save(self, username: str, password: str) -> None:
        payload = json.dumps(
            {"username": username, "password": password},
            ensure_ascii=False,
        ).encode("utf-8")
        os.makedirs(self.cache_root, exist_ok=True)
        with open(self.data_file, "wb") as f:
            f.write(self._fernet.encrypt(payload))

    def clear(self) -> None:
        if os.path.exists(self.data_file):
            try:
                os.remove(self.data_file)
            except OSError:
                pass


login_credentials = LoginCredentialStore()
