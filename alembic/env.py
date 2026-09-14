import asyncio
import os
from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import pool
from app.db.models import Base


# Migration credentials are separate from runtime credentials.
def database_url():
    from dotenv import load_dotenv

    load_dotenv()
    return os.environ.get("MIGRATION_DATABASE_URL") or os.environ["DATABASE_URL"]


def sync_run(connection):
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    engine = create_async_engine(database_url(), poolclass=pool.NullPool, hide_parameters=True)
    async with engine.connect() as connection:
        await connection.run_sync(sync_run)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=database_url(), target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
