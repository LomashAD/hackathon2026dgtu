"""Клиент локального Ollama: чат и эмбеддинги. Никаких внешних API."""
from __future__ import annotations

import logging
import time

import httpx

from app.config import get_config

log = logging.getLogger(__name__)


class OllamaClient:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or get_config()
        self.base = cfg["llm"]["base_url"].rstrip("/")
        self.model = cfg["llm"]["model"]
        self.temperature = cfg["llm"].get("temperature", 0)
        self.num_ctx = cfg["llm"].get("num_ctx", 4096)
        self.timeout = cfg["llm"].get("timeout_s", 120)
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
        })
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
            return {"ok": True, "llm": has(self.model), "embed": has(self.embed_model)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}


_client: OllamaClient | None = None


def get_client() -> OllamaClient:
    global _client
    if _client is None:
        _client = OllamaClient()
    return _client
