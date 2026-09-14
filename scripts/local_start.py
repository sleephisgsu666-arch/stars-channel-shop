"""macOS local launcher. Never source .env as shell code or print its values."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".local"
PYTHON = ROOT / ".venv/bin/python"
PG_PORT, REDIS_PORT, API_PORT = 55432, 56380, 18080


class LaunchError(Exception):
    pass


def available(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def credentials() -> dict[str, str]:
    path = STATE / "credentials.json"
    if path.exists():
        return json.loads(path.read_text())
    values = {
        name: secrets.token_urlsafe(48) for name in ("owner", "app", "migrator", "redis", "path", "secret")
    }
    with path.open("x") as file:
        os.chmod(path, 0o600)
        json.dump(values, file)
    return values


def dependencies() -> None:
    digest = hashlib.sha256((ROOT / "requirements.lock").read_bytes()).hexdigest()
    stamp = STATE / "dependencies.sha256"
    if stamp.exists() and stamp.read_text() == digest:
        return
    print("Устанавливаю зависимости из requirements.lock…", flush=True)
    result = subprocess.run([str(PYTHON), "-m", "pip", "install", "-r", "requirements.lock"], cwd=ROOT)
    if result.returncode:
        raise LaunchError("Не удалось установить зависимости. Проверьте подключение к интернету.")
    stamp.write_text(digest)


async def prepare_database(passwords: dict[str, str]) -> None:
    import asyncpg

    connection = await asyncpg.connect(
        host="127.0.0.1",
        port=PG_PORT,
        user="shop_local_owner",
        password=passwords["owner"],
        database="postgres",
    )
    try:
        for role, key in [("shop_app", "app"), ("shop_migrator", "migrator")]:
            if not await connection.fetchval("SELECT 1 FROM pg_roles WHERE rolname=$1", role):
                # Identifiers are constants; escape literals even though passwords are generated locally.
                literal = passwords[key].replace("'", "''")
                await connection.execute(
                    f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '{literal}'"
                )
        if not await connection.fetchval("SELECT 1 FROM pg_database WHERE datname='shop_local'"):
            await connection.execute("CREATE DATABASE shop_local OWNER shop_migrator")
        await connection.execute("ALTER ROLE shop_app SET search_path = shop")
        await connection.execute("ALTER ROLE shop_migrator SET search_path = shop")
    finally:
        await connection.close()
    connection = await asyncpg.connect(
        host="127.0.0.1",
        port=PG_PORT,
        user="shop_local_owner",
        password=passwords["owner"],
        database="shop_local",
    )
    try:
        await connection.execute("""
            REVOKE CREATE ON SCHEMA public FROM PUBLIC;
            CREATE SCHEMA IF NOT EXISTS shop AUTHORIZATION shop_migrator;
            GRANT USAGE ON SCHEMA shop TO shop_app;
            ALTER DEFAULT PRIVILEGES FOR ROLE shop_migrator IN SCHEMA shop
                GRANT SELECT, INSERT, UPDATE ON TABLES TO shop_app;
            ALTER DEFAULT PRIVILEGES FOR ROLE shop_migrator IN SCHEMA shop
                GRANT USAGE, SELECT ON SEQUENCES TO shop_app;
        """)
    finally:
        await connection.close()


def main() -> None:
    os.chdir(ROOT)
    os.umask(0o077)
    STATE.mkdir(mode=0o700, exist_ok=True)
    lock_file = (STATE / "launcher.lock").open("a")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise LaunchError("Проект уже запущен в другом окне.") from None
    dependencies()
    sys.path.insert(0, str(ROOT))
    from dotenv import dotenv_values
    from pydantic import ValidationError
    from aiogram.utils.token import validate_token
    from app.core.config import Settings

    passwords = credentials()
    values = {key: value for key, value in dotenv_values(ROOT / ".env").items() if value is not None}
    # Only application settings are forwarded; Docker/bootstrap credentials remain outside children.
    allowed = {name.upper() for name in Settings.model_fields}
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in allowed
        and key
        not in {
            "MIGRATION_DATABASE_URL",
            "POSTGRES_PASSWORD",
            "APP_DB_PASSWORD",
            "MIGRATOR_DB_PASSWORD",
            "REDIS_PASSWORD",
        }
    }
    env.update({key: value for key, value in values.items() if key in allowed})
    env.update(
        APP_ENV="development",
        DATABASE_URL=f"postgresql+asyncpg://shop_app:{passwords['app']}@127.0.0.1:{PG_PORT}/shop_local",
        REDIS_URL=f"redis://:{passwords['redis']}@127.0.0.1:{REDIS_PORT}/0",
        WEBHOOK_PATH=passwords["path"],
        WEBHOOK_SECRET=passwords["secret"],
        WEBHOOK_BASE_URL="https://localhost.invalid",
    )
    env["TERMS_URL"] = env.get("TERMS_URL") or "https://example.com/terms"
    try:
        settings = Settings(
            _env_file=None,
            **{
                key.lower(): value
                for key, value in env.items()
                if key in allowed and key != "ADMIN_TELEGRAM_IDS"
            },
            admin_telegram_ids=json.loads(env.get("ADMIN_TELEGRAM_IDS", "[]")),
        )
        validate_token(settings.bot_token.get_secret_value())
    except Exception as exc:
        # Never render validation input or the original exception (may contain a secret).
        if isinstance(exc, ValidationError):
            fields = ", ".join(".".join(map(str, error["loc"])) for error in exc.errors(include_input=False))
            raise LaunchError(f"Проверьте поля .env: {fields}. Значения секретов не выводятся.") from None
        raise LaunchError("Проверьте BOT_TOKEN и ADMIN_TELEGRAM_IDS в .env.") from None

    pgdata = STATE / "postgres"
    children: list[tuple[str, subprocess.Popen]] = []
    logs = []
    pg_started = False
    stopping = False

    def stop_signal(signum, frame):
        nonlocal stopping
        stopping = True

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, stop_signal)

    def run_quiet(args: list[str], logfile: str, process_env=None) -> None:
        with (STATE / logfile).open("ab") as output:
            result = subprocess.run(args, cwd=ROOT, env=process_env, stdout=output, stderr=output)
        if result.returncode:
            raise LaunchError(f"Ошибка подготовки. Подробности: .local/{logfile}")

    def start(name: str, args: list[str]) -> subprocess.Popen:
        output = (STATE / f"{name}.log").open("ab")
        logs.append(output)
        process = subprocess.Popen(
            args, cwd=ROOT, env=env, stdout=output, stderr=output, start_new_session=True
        )
        children.append((name, process))
        return process

    try:
        for port in (REDIS_PORT, API_PORT):
            if not available(port):
                raise LaunchError(f"Порт {port} занят. Закройте предыдущий локальный запуск.")
        if not (pgdata / "PG_VERSION").exists():
            if pgdata.exists() and any(pgdata.iterdir()):
                raise LaunchError(
                    "Незавершённая инициализация .local/postgres. Данные не удалены; проверьте postgres-init.log."
                )
            pwfile = STATE / "postgres-password"
            pwfile.write_text(passwords["owner"])
            try:
                run_quiet(
                    [
                        "initdb",
                        "-D",
                        str(pgdata),
                        "-U",
                        "shop_local_owner",
                        "--auth=scram-sha-256",
                        "--pwfile",
                        str(pwfile),
                    ],
                    "postgres-init.log",
                )
            finally:
                pwfile.unlink(missing_ok=True)
        running = subprocess.run(["pg_ctl", "-D", str(pgdata), "status"], capture_output=True).returncode == 0
        if not running:
            if not available(PG_PORT):
                raise LaunchError(f"Порт PostgreSQL {PG_PORT} занят другим процессом.")
            print("Запускаю PostgreSQL проекта…", flush=True)
            run_quiet(
                [
                    "pg_ctl",
                    "-D",
                    str(pgdata),
                    "-l",
                    str(STATE / "postgres.log"),
                    "-o",
                    f"-h 127.0.0.1 -p {PG_PORT}",
                    "-w",
                    "start",
                ],
                "postgres-start.log",
            )
            pg_started = True
        asyncio.run(prepare_database(passwords))
        redis_config = STATE / "redis.conf"
        redis_config.write_text(
            f"bind 127.0.0.1\nprotected-mode yes\nport {REDIS_PORT}\n"
            f'requirepass {passwords["redis"]}\ndir "{STATE}"\nappendonly yes\ndaemonize no\n'
        )
        redis_process = start("redis", ["redis-server", str(redis_config)])
        from redis import Redis

        client = Redis(host="127.0.0.1", port=REDIS_PORT, password=passwords["redis"], socket_timeout=1)
        try:
            for _ in range(50):
                if stopping or redis_process.poll() is not None:
                    raise LaunchError("Redis не запустился. Проверьте .local/redis.log")
                try:
                    if client.ping():
                        break
                except Exception:
                    time.sleep(0.1)
            else:
                raise LaunchError("Redis не ответил за 5 секунд.")
        finally:
            client.close()
        migration_env = dict(
            env,
            MIGRATION_DATABASE_URL=f"postgresql+asyncpg://shop_migrator:{passwords['migrator']}@127.0.0.1:{PG_PORT}/shop_local",
        )
        print("Применяю миграции…", flush=True)
        run_quiet([str(PYTHON), "-m", "alembic", "upgrade", "head"], "migrations.log", migration_env)
        if stopping:
            return
        start(
            "api",
            [
                str(PYTHON),
                "-m",
                "uvicorn",
                "app.main:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(API_PORT),
                "--no-access-log",
            ],
        )
        start("worker", [str(PYTHON), "-m", "app.jobs.worker"])
        start("bot", [str(PYTHON), "-m", "app.cli", "poll"])
        print(
            "\nПроцессы запущены. Откройте своего бота в Telegram и отправьте /start.\n"
            f"Проверка готовности: http://127.0.0.1:{API_PORT}/ready\n"
            "Логи: .local/bot.log, .local/worker.log, .local/api.log\n"
            "Для остановки нажмите Ctrl+C. База и покупки сохранятся.\n"
            "Polling отключает webhook этого бота. Не запускайте его одновременно на другом сервере.\n",
            flush=True,
        )
        while not stopping:
            for name, process in children:
                if process.poll() is not None:
                    raise LaunchError(f"Процесс {name} остановился. Проверьте .local/{name}.log")
            time.sleep(0.5)
    finally:
        print("Останавливаю локальные процессы…", flush=True)
        for _, process in reversed(children):
            if process.poll() is None:
                process.terminate()
        for _, process in reversed(children):
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for output in logs:
            output.close()
        if pg_started:
            subprocess.run(
                ["pg_ctl", "-D", str(pgdata), "-m", "fast", "-w", "stop"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        lock_file.close()


if __name__ == "__main__":
    try:
        main()
    except LaunchError as error:
        print(f"\n{error}", file=sys.stderr)
        sys.exit(1)
    except Exception:
        print(
            "\nНе удалось запустить локальное окружение. Проверьте .env и файлы в .local; "
            "значения ошибок скрыты для защиты секретов.",
            file=sys.stderr,
        )
        sys.exit(1)
