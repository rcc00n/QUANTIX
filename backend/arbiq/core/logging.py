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


def redact_sensitive(_logger, _method_name, event_dict):
    def clean(value, depth=0):
        if depth > 8:
            return "[DEPTH_LIMIT]"
        if isinstance(value, dict):
            return {
                key: "[REDACTED]" if _PRIVATE_KEY.search(str(key)) else clean(item, depth + 1)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [clean(item, depth + 1) for item in value[:100]]
        return redact_text(value) if isinstance(value, str) else value

    return clean(event_dict)


class SecretFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact_text(record.getMessage())
        record.args = ()
        return True


def configure_logging(level: str) -> None:
    logging.basicConfig(format="%(message)s", level=level)
    for handler in logging.getLogger().handlers:
        if not any(isinstance(value, SecretFilter) for value in handler.filters):
            handler.addFilter(SecretFilter())
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_sensitive,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
        cache_logger_on_first_use=True,
    )
