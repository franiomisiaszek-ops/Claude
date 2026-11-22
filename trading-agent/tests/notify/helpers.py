"""A fake Telegram Bot API, served through httpx's mock transport (no network)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from papertrader.notify.telegram import TelegramClient

TOKEN = "123456:SECRET-token_value"
CHAT = "4242"


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []  # sendMessage payloads
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.updates: list[dict[str, Any]] = []
        self.fail: Callable[[str, dict[str, Any]], httpx.Response | None] = lambda *_: None
        self._next_id = 100

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.telegram.org"
        assert request.url.path.startswith(f"/bot{TOKEN}/")
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content or b"{}")
        self.calls.append((method, payload))
        failure = self.fail(method, payload)
        if failure is not None:
            return failure
        if method == "sendMessage":
            self.sent.append(payload)
            self._next_id += 1
            return ok({"message_id": self._next_id})
        if method == "getUpdates":
            offset = payload.get("offset") or 0
            return ok([u for u in self.updates if u["update_id"] >= offset])
        if method == "getMe":
            return ok({"id": 1, "username": "my_papertrader_bot", "first_name": "Papertrader"})
        if method in ("answerCallbackQuery", "editMessageReplyMarkup"):
            return ok(True)
        return error(404, "Not Found")

    def client(self) -> TelegramClient:
        return TelegramClient(TOKEN, http=httpx.Client(transport=httpx.MockTransport(self.handler)))

    def message(self, text: str, *, chat: str = CHAT, date: float | None = None) -> None:
        update_id = len(self.updates) + 1
        message: dict[str, Any] = {"message_id": update_id, "chat": {"id": int(chat), "first_name": "Fran"},
                                   "text": text}  # fmt: skip
        if date is not None:
            message["date"] = int(date)
        self.updates.append({"update_id": update_id, "message": message})

    def tap(self, data: str, message_id: int, *, chat: str = CHAT) -> None:
        """A tap on an inline button (X6)."""
        update_id = len(self.updates) + 1
        self.updates.append({"update_id": update_id, "callback_query": {
            "id": f"cb{update_id}", "data": data, "from": {"id": int(chat)},
            "message": {"message_id": message_id, "chat": {"id": int(chat)}}}})  # fmt: skip

    def method_calls(self, method: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.calls if name == method]

    def texts(self) -> list[str]:
        return [p["text"] for p in self.sent]


def ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def error(code: int, description: str, retry_after: int | None = None) -> httpx.Response:
    body: dict[str, Any] = {"ok": False, "error_code": code, "description": description}
    if retry_after is not None:
        body["parameters"] = {"retry_after": retry_after}
    return httpx.Response(code, json=body)
