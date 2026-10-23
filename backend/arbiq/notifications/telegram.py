"""Official Telegram HTTPS API. Exceptions expose codes only, never token-bearing URLs."""

import json
import re
import stat
from pathlib import Path

import aiohttp

TOKEN_PATTERN = re.compile(r"^[0-9]{5,15}:[A-Za-z0-9_-]{25,100}$")


class TelegramError(Exception):
    def __init__(self, code: str, *, retry_after: int | None = None, ambiguous=False):
        self.code, self.retry_after, self.ambiguous = code, retry_after, ambiguous
        super().__init__(code)


def read_bot_token(path: str) -> str:
    try:
        target = Path(path)
        info = target.stat()
        # Docker secrets are read-only root-owned 0400/0444 mounts; host token must be private.
        if not stat.S_ISREG(info.st_mode) or info.st_size > 256:
            raise TelegramError("TOKEN_FILE_INVALID")
        if not path.startswith("/run/secrets/") and stat.S_IMODE(info.st_mode) & 0o077:
            raise TelegramError("TOKEN_FILE_PERMISSIONS")
        token = target.read_text().strip()
    except OSError:
        raise TelegramError("TOKEN_FILE_UNAVAILABLE") from None
    if not TOKEN_PATTERN.fullmatch(token):
        raise TelegramError("TOKEN_FILE_INVALID")
    return token


class TelegramClient:
    def __init__(self, session: aiohttp.ClientSession, token: str):
        self.session, self._token = session, token

    def __repr__(self):
        return "TelegramClient(token=<redacted>)"

    async def call(self, method: str, payload=None):
        if method not in {"getMe", "getWebhookInfo", "getUpdates", "sendMessage"}:
            raise TelegramError("UNSUPPORTED_METHOD")
        try:
            async with self.session.post(
                f"https://api.telegram.org/bot{self._token}/{method}",
                json=payload or {},
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=35 if method == "getUpdates" else 10),
            ) as response:
                raw = await response.content.read(262145)
                if len(raw) > 262144:
                    raise TelegramError("RESPONSE_TOO_LARGE", ambiguous=method == "sendMessage")
                try:
                    body = json.loads(raw)
                except (ValueError, TypeError):
                    raise TelegramError(
                        "INVALID_RESPONSE", ambiguous=method == "sendMessage"
                    ) from None
                if not isinstance(body, dict):
                    raise TelegramError("INVALID_RESPONSE", ambiguous=method == "sendMessage")
                if response.status != 200 or not body.get("ok"):
                    try:
                        code = int(body.get("error_code", response.status))
                        retry = body.get("parameters", {}).get("retry_after")
                        retry = min(max(int(retry), 0), 86400) if retry else None
                    except (ValueError, TypeError, AttributeError):
                        raise TelegramError(
                            "INVALID_RESPONSE", ambiguous=method == "sendMessage"
                        ) from None
                    raise TelegramError(f"TELEGRAM_{code}", retry_after=retry)
                result = body.get("result")
                if method == "getUpdates":
                    valid = isinstance(result, list) and len(result) <= 25
                else:
                    valid = isinstance(result, dict)
                if method == "sendMessage":
                    valid = valid and isinstance(result.get("message_id"), int)
                if not valid:
                    raise TelegramError("INVALID_RESPONSE", ambiguous=method == "sendMessage")
                return result
        except (aiohttp.ClientError, TimeoutError, OSError):
            raise TelegramError("TELEGRAM_NETWORK", ambiguous=method == "sendMessage") from None

    async def readiness(self):
        identity = await self.call("getMe")
        webhook = await self.call("getWebhookInfo")
        if webhook.get("url"):
            raise TelegramError("WEBHOOK_CONFLICT")
        return {"bot_username": identity.get("username"), "bot_id": identity.get("id")}

    async def updates(self, offset):
        return await self.call(
            "getUpdates",
            {"offset": offset, "limit": 25, "timeout": 20, "allowed_updates": ["message"]},
        )

    async def send(self, chat_id, message, *, silent=False):
        return await self.call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": message[:4000],
                "disable_notification": silent,
                "link_preview_options": {"is_disabled": True},
                "protect_content": True,
            },
        )
