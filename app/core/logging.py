import json
import logging
import re


class SafeFormatter(logging.Formatter):
    def __init__(self, secrets=()):
        super().__init__()
        self.secrets = tuple(s for s in secrets if s)

    def format(self, record):
        # Deliberately exclude exception values, stack locals and third-party request bodies.
        message = record.getMessage()
        for secret in self.secrets:
            message = message.replace(secret, "[REDACTED]")
        message = re.sub(r"https?://\S+", "[URL]", message)
        message = re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{20,}", "[TOKEN]", message)
        message = re.sub(r"postgres(?:ql)?(?:\+asyncpg)?://\S+", "[DATABASE]", message)
        return json.dumps({"level": record.levelname, "event": message}, ensure_ascii=False)


def configure(settings):
    handler = logging.StreamHandler()
    handler.setFormatter(
        SafeFormatter(
            [
                settings.bot_token.get_secret_value(),
                settings.webhook_secret.get_secret_value(),
                settings.database_url.get_secret_value(),
                settings.redis_url.get_secret_value(),
                settings.webhook_path,
            ]
        )
    )
    logging.basicConfig(level=settings.log_level, handlers=[handler], force=True)
    for name in ("aiogram", "httpx", "httpcore", "sqlalchemy", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def event(name: str, **fields):
    logging.getLogger("shop").info(json.dumps({"event": name, **fields}, default=str))
