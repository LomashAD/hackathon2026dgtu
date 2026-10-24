"""Клиент локального Ollama: чат и эмбеддинги. Никаких внешних API."""
from __future__ import annotations

import ipaddress
import logging
import socket
import time
from urllib.parse import urlsplit

import httpx

from app.config import get_config

log = logging.getLogger(__name__)


def is_internal(url: str) -> bool:
    """Адрес модели внутри контура: localhost, частная сеть или имя сервиса без домена (ollama в Docker)."""
    host = urlsplit(url).hostname or ""
    try:
        ips = {ipaddress.ip_address(a[4][0].split("%")[0]) for a in socket.getaddrinfo(host, None)}
    except (OSError, ValueError):
        return host == "localhost" or ("." not in host and ":" not in host and bool(host))
    return bool(ips) and all(ip.is_private or ip.is_loopback for ip in ips)


class RemoteModelError(RuntimeError):
    """llm.base_url указывает за пределы контура."""


class OllamaClient:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or get_config()
        self.base = cfg["llm"]["base_url"].rstrip("/")
        # Фрагменты базы знаний и вопросы сотрудников не должны уйти во внешний сервис из-за ошибки в настройках
        if not cfg["llm"].get("allow_remote", False) and not is_internal(self.base):
            raise RemoteModelError(f"llm.base_url = {self.base} — адрес вне внутренней сети. Данные базы знаний "
                                   "передаются только локальной модели; если внешний адрес нужен осознанно, "
                                   "задайте llm.allow_remote: true в config.yaml")
        self.model = cfg["llm"]["model"]
        self.temperature = cfg["llm"].get("temperature", 0)
        self.num_ctx = cfg["llm"].get("num_ctx", 4096)
        self.timeout = cfg["llm"].get("timeout_s", 120)
        self.wait = cfg["llm"].get("wait_s", self.timeout)   # чат в диалоге необязателен: у каждого вызова есть запасной путь
        self.embed_model = cfg["embeddings"]["model"]
        self.batch = cfg["embeddings"].get("batch_size", 16)
        self._http = httpx.Client(timeout=self.timeout)

    def chat(self, messages: list[dict], max_tokens: int = 300, stop: list[str] | None = None) -> str:
        t0 = time.perf_counter()
        opts = {"temperature": self.temperature, "num_ctx": self.num_ctx, "num_predict": max_tokens, "seed": 42}
        if stop:
            opts["stop"] = stop
        r = self._http.post(f"{self.base}/api/chat", json={
            "model": self.model, "messages": messages, "stream": False, "options": opts,
            "keep_alive": "30m",
        }, timeout=self.wait)
        r.raise_for_status()
        out = r.json()["message"]["content"].strip()
        log.info("llm chat %.2fs, %d chars", time.perf_counter() - t0, len(out))
        return out

    def embed(self, texts: list[str]) -> list[list[float]]:
        vecs: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            r = self._http.post(f"{self.base}/api/embed", json={
                "model": self.embed_model, "input": texts[i:i + self.batch], "keep_alive": "30m"})
            r.raise_for_status()
            vecs.extend(r.json()["embeddings"])
        return vecs

    def health(self) -> dict:
        try:
            tags = self._http.get(f"{self.base}/api/tags", timeout=5).json()
            names = {m["name"] for m in tags.get("models", [])}
            def has(n):
                return n in names or f"{n}:latest" in names
            return {"ok": True, "llm": has(self.model), "embed": has(self.embed_model), "internal": is_internal(self.base)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}


_client: OllamaClient | None = None


def get_client() -> OllamaClient:
    global _client
    if _client is None:
        _client = OllamaClient()
    return _client
