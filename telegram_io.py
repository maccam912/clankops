from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from urllib.request import Request, urlopen


@dataclass
class TelegramIO:
    bot_token: str
    allowed_user_id: int
    poll_timeout_seconds: int = 30
    request_timeout_seconds: int = 35
    _offset: int = 0
    _chat_id: int | None = field(default=None, init=False, repr=False)

    def get_me(self) -> dict[str, object]:
        result = self._api_call("getMe", {})
        if not isinstance(result, dict):
            raise RuntimeError("Unexpected getMe response.")
        return result

    async def read_message(self) -> str:
        while True:
            try:
                updates = await asyncio.to_thread(
                    self._api_call,
                    "getUpdates",
                    {
                        "offset": self._offset,
                        "timeout": self.poll_timeout_seconds,
                        "allowed_updates": ["message"],
                    },
                )
                if not isinstance(updates, list):
                    await asyncio.sleep(1)
                    continue

                for update in updates:
                    if not isinstance(update, dict):
                        continue

                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        self._offset = max(self._offset, update_id + 1)

                    message = update.get("message")
                    if not isinstance(message, dict):
                        continue

                    from_user = message.get("from")
                    if not isinstance(from_user, dict):
                        continue

                    from_id = from_user.get("id")
                    if from_id != self.allowed_user_id:
                        continue

                    chat = message.get("chat")
                    if isinstance(chat, dict) and isinstance(chat.get("id"), int):
                        self._chat_id = int(chat["id"])

                    text = message.get("text")
                    if isinstance(text, str) and text.strip():
                        return text.strip()
            except Exception as err:
                print(f"[telegram] read loop error: {err}")
                await asyncio.sleep(2)

    async def send_message(self, text: str):
        if self._chat_id is None:
            print("[telegram] No authorized chat available yet; skipping outbound message.")
            return

        for chunk in _chunk_text(text, max_chars=4096):
            await asyncio.to_thread(
                self._api_call,
                "sendMessage",
                {
                    "chat_id": self._chat_id,
                    "text": chunk,
                },
            )

    def _api_call(self, method: str, payload: dict[str, object]) -> object:
        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urlopen(request, timeout=self.request_timeout_seconds) as response:
            raw = response.read().decode("utf-8")

        data = json.loads(raw)
        if not data.get("ok"):
            description = data.get("description", "Telegram API error")
            raise RuntimeError(f"{method} failed: {description}")
        return data.get("result")


def _chunk_text(text: str, max_chars: int) -> list[str]:
    content = text.strip()
    if not content:
        return []
    return [content[i : i + max_chars] for i in range(0, len(content), max_chars)]
