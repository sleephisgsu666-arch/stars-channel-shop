import asyncio
from contextlib import asynccontextmanager
from sqlalchemy import text
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.core.security import lock_key


class Database:
    def __init__(self, url: str):
        self.engine = create_async_engine(
            url, pool_pre_ping=True, pool_size=10, max_overflow=10, hide_parameters=True
        )
        self.lock_engine = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def lock(self, key: str):
        # Session advisory lock survives commits: network calls need no open DB transaction.
        # Use direct PostgreSQL/session pooling, never PgBouncer transaction pooling.
        async with self.lock_engine.connect() as connection:
            locked = False
            try:
                async with asyncio.timeout(5):
                    await connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": lock_key(key)})
                    await connection.commit()
                    locked = True
                yield
            finally:
                if locked:
                    try:
                        await asyncio.shield(
                            connection.execute(
                                text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key(key)}
                            )
                        )
                        await connection.commit()
                    except BaseException:
                        await connection.invalidate()
                else:
                    await connection.invalidate()

    @asynccontextmanager
    async def transaction(self):
        async with self.sessions() as session, session.begin():
            yield session
