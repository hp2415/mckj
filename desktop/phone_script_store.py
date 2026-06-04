"""电话工作台话术本地缓存：按客户×业务微信线程隔离，持久化到用户配置目录。"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from config_loader import cfg
from logger_cfg import logger

MAX_ENTRIES = 300
_PLACEHOLDER_MARKERS = (
    "暂无已生成话术",
    "后续将支持一键 AI 生成",
)


def customer_script_key(customer: dict | None) -> str | None:
    """与侧栏客户行一致：(raw_customer_id, sales_wechat_id)。"""
    if not isinstance(customer, dict):
        return None
    rid = str(customer.get("id") or "").strip()
    if not rid:
        return None
    sw = str(customer.get("sales_wechat_id") or "").strip()
    return f"{rid}|{sw}"


def is_persistable_script(text: str) -> bool:
    t = (text or "").strip()
    if not t or t.startswith("⚠️"):
        return False
    return not any(m in t for m in _PLACEHOLDER_MARKERS)


class PhoneScriptStore:
    def __init__(self) -> None:
        base = os.path.dirname(cfg.config_path)
        os.makedirs(base, exist_ok=True)
        self._path = os.path.join(base, "phone_scripts.json")
        self._order: list[str] = []
        self._entries: dict[str, dict] = {}
        self._load()

    def get(self, key: str | None) -> str | None:
        if not key:
            return None
        entry = self._entries.get(key)
        if not entry:
            return None
        script = (entry.get("script") or "").strip()
        return script or None

    def put(self, key: str | None, script: str) -> None:
        if not key or not is_persistable_script(script):
            return
        script = script.strip()
        self._entries[key] = {
            "script": script,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if key in self._order:
            self._order.remove(key)
        self._order.append(key)
        while len(self._order) > MAX_ENTRIES:
            old = self._order.pop(0)
            self._entries.pop(old, None)
        self._save()

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                return
            entries = raw.get("entries")
            order = raw.get("order")
            if isinstance(entries, dict):
                self._entries = {
                    str(k): v for k, v in entries.items() if isinstance(v, dict)
                }
            if isinstance(order, list):
                self._order = [str(k) for k in order if str(k) in self._entries]
            for k in self._entries:
                if k not in self._order:
                    self._order.append(k)
        except Exception as e:
            logger.warning(f"读取电话话术缓存失败: {e}")

    def _save(self) -> None:
        try:
            payload = {"order": self._order, "entries": self._entries}
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception as e:
            logger.warning(f"保存电话话术缓存失败: {e}")


_store: PhoneScriptStore | None = None


def get_phone_script_store() -> PhoneScriptStore:
    global _store
    if _store is None:
        _store = PhoneScriptStore()
    return _store
