import json
import os
import sys

from cryptography.fernet import Fernet

import win_dpapi


class LoginCredentialStore:
    """本地记住登录账号密码（Windows DPAPI，绑定当前 Windows 用户）。"""

    _FILENAME = "saved_login.dpapi"
    _LEGACY_FILENAME = "saved_login.dat"

    def __init__(self):
        if getattr(sys, "frozen", False):
            base_dir = os.path.dirname(sys.executable)
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))
        self.cache_root = os.path.join(base_dir, "desktop_cache")
        self.data_file = os.path.join(self.cache_root, self._FILENAME)
        self._legacy_file = os.path.join(self.cache_root, self._LEGACY_FILENAME)
        self._legacy_key_file = os.path.join(self.cache_root, "secret.key")

    def load(self) -> dict | None:
        loaded = self._load_dpapi(self.data_file)
        if loaded:
            return loaded
        migrated = self._load_legacy_fernet()
        if migrated:
            try:
                self.save(migrated["username"], migrated["password"])
                self._remove_quiet(self._legacy_file)
            except Exception:
                pass
            return migrated
        return None

    def save(self, username: str, password: str) -> None:
        payload = json.dumps(
            {"username": username, "password": password},
            ensure_ascii=False,
        ).encode("utf-8")
        os.makedirs(self.cache_root, exist_ok=True)
        protected = win_dpapi.protect(payload)
        with open(self.data_file, "wb") as f:
            f.write(protected)
        self._remove_quiet(self._legacy_file)

    def clear(self) -> None:
        self._remove_quiet(self.data_file)
        self._remove_quiet(self._legacy_file)

    def _load_dpapi(self, path: str) -> dict | None:
        if not os.path.exists(path):
            return None
        try:
            with open(path, "rb") as f:
                raw = win_dpapi.unprotect(f.read())
            return self._parse_payload(raw)
        except Exception:
            return None

    def _load_legacy_fernet(self) -> dict | None:
        if not os.path.exists(self._legacy_file) or not os.path.exists(self._legacy_key_file):
            return None
        try:
            with open(self._legacy_key_file, "rb") as f:
                key = f.read()
            with open(self._legacy_file, "rb") as f:
                raw = Fernet(key).decrypt(f.read())
            return self._parse_payload(raw)
        except Exception:
            return None

    @staticmethod
    def _parse_payload(raw: bytes) -> dict | None:
        data = json.loads(raw.decode("utf-8"))
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        if not username or not password:
            return None
        return {"username": username, "password": password}

    @staticmethod
    def _remove_quiet(path: str) -> None:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


login_credentials = LoginCredentialStore()
