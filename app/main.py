from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.core.config import Settings
from app.runtime import Runtime
from app.bot.dispatcher import build_dispatcher
from app.api.webhook import router
from app.api.health import router as health_router


def create_app(settings=None, runtime=None):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        app.state.runtime = runtime or Runtime(settings)
        app.state.dispatcher = build_dispatcher(app.state.runtime)
        yield
        await app.state.runtime.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(router(settings))
    app.include_router(health_router)
    return app
