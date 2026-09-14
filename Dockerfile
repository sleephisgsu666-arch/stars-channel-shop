FROM python:3.12.12-slim AS build
WORKDIR /build
COPY requirements.lock ./
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.lock

FROM python:3.12.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --gid 10001 shop && useradd --uid 10001 --gid shop --create-home shop
WORKDIR /app
COPY --from=build /wheels /wheels
COPY requirements.lock ./
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.lock && rm -rf /wheels
COPY --chown=shop:shop app ./app
COPY --chown=shop:shop alembic ./alembic
COPY --chown=shop:shop alembic.ini ./
USER shop
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)"
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--limit-concurrency", "32"]
