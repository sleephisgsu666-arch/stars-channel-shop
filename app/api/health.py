from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request):
    try:
        async with request.app.state.runtime.db.sessions() as s:
            await s.execute(text("SELECT 1"))
        await request.app.state.runtime.redis.ping()
    except Exception:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "ready"}
