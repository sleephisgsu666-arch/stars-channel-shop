import hashlib
import hmac
from uuid import UUID


class Denied(Exception):
    """Expected domain rejection; transport renders only a safe message."""


class InsufficientBalance(Denied):
    def __init__(self, missing: int):
        self.missing = missing
        super().__init__(
            f"Недостаточно средств. Не хватает {missing} ⭐. Пополните баланс и повторите покупку."
        )


def uuid_value(value: str) -> UUID:
    if not isinstance(value, str) or len(value) != 36:
        raise Denied("Некорректный идентификатор.")
    try:
        result = UUID(value)
    except ValueError:
        raise Denied("Некорректный идентификатор.") from None
    if str(result) != value:
        raise Denied("Некорректный идентификатор.")
    return result


def authorized(user_id: int, admin_ids: list[int]) -> None:
    if type(user_id) is not int or user_id not in admin_ids:
        raise Denied("Недостаточно прав.")


def secret_matches(provided: str, expected: str) -> bool:
    return hmac.compare_digest(provided.encode(), expected.encode())


def lock_key(value: str) -> int:
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big", signed=True)


class RateLimiter:
    SCRIPT = """
    local n = redis.call('INCR', KEYS[1])
    if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
    return n
    """
    LIMITS = {
        "invoice": (5, 60),
        "invite": (3, 60),
        "free": (3, 60),
        "callback": (30, 60),
        "admin": (20, 60),
        "support": (3, 60),
    }

    def __init__(self, redis):
        self.redis = redis

    async def check(self, user_id: int, operation: str) -> None:
        limit, seconds = self.LIMITS[operation]
        count = await self.redis.eval(self.SCRIPT, 1, f"limit:{user_id}:{operation}", seconds)
        if count > limit:
            raise Denied("Слишком много запросов. Попробуйте через минуту.")
