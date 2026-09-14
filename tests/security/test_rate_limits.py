import os
import asyncio
import pytest
from redis.asyncio import Redis
from app.core.security import RateLimiter, Denied


async def test_atomic_user_operation_limits():
    url = os.getenv("TEST_REDIS_URL")
    if not url:
        pytest.skip("Set TEST_REDIS_URL to disposable Redis database")
    redis = Redis.from_url(url)
    try:
        await redis.flushdb()
        limiter = RateLimiter(redis)
        results = await asyncio.gather(
            *(limiter.check(101, "invite") for _ in range(20)), return_exceptions=True
        )
        assert sum(isinstance(x, Denied) for x in results) == 17
        await limiter.check(202, "invite")
        await limiter.check(101, "invoice")
        assert await redis.ttl("limit:101:invite") > 0
    finally:
        await redis.aclose()
