import logging
import re

import structlog

_BOT_TOKEN = re.compile(r"(?<!\d)\d{7,13}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])")
_AUTH_VALUE = re.compile(r"(?i)(authorization[\"']?[=: ]+[\"']?(?:bearer|basic)\s+)[^\s,;\"']+")
_URL_USERINFO = re.compile(r"([a-z][a-z0-9+.-]*://)[^/@\s]+:[^/@\s]+@", re.I)
_PRIVATE_KEY = re.compile(
    r"(?i)(password|passphrase|secret|api.?key|authorization|cookie|chat.?id|token|"
    r"database.?url|dsn|credential)"
)


def redact_text(value: str) -> str:
    value = _URL_USERINFO.sub(r"\1[REDACTED]@", value)
    return _AUTH_VALUE.sub(r"\1[REDACTED]", _BOT_TOKEN.sub("[REDACTED]", value))


class SecretFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact_text(record.getMessage())
        record.args = ()
        return True


