import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import os
import sys

import pytest


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[2] / "scripts/local_start.py"
    spec = importlib.util.spec_from_file_location("local_start", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state = tmp_path / ".local"
    state.mkdir()
    (state / "postgres").mkdir()
    (state / "postgres/PG_VERSION").write_text("16")
    (tmp_path / ".env").write_text(
        "BOT_TOKEN=123456789:" + "a" * 35 + "\n"
        "ADMIN_TELEGRAM_IDS=[100]\nSUPPORT_CONTACT=@support\n"
        "DATABASE_URL=postgresql+asyncpg://docker:secret@postgres/shop\n"
        "MIGRATION_DATABASE_URL=postgresql+asyncpg://docker:secret@postgres/shop\n"
    )
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "STATE", state)
    monkeypatch.setattr(module, "PYTHON", Path(sys.executable))
    monkeypatch.setattr(module, "dependencies", lambda: None)
    monkeypatch.setattr(module, "available", lambda port: True)
    monkeypatch.setattr(module.signal, "signal", lambda *args: None)
    monkeypatch.setattr(module, "prepare_database", AsyncMock())
    monkeypatch.chdir(tmp_path)
    old_umask = os.umask(0o077)
    yield module
    os.umask(old_umask)


def test_generated_credentials_persist_with_private_permissions(launcher):
    first = launcher.credentials()
    assert launcher.credentials() == first
    assert len(set(first.values())) == 6
    assert (launcher.STATE / "credentials.json").stat().st_mode & 0o777 == 0o600


def test_child_failure_stops_processes_and_uses_local_urls(launcher, monkeypatch):
    import redis

    monkeypatch.setattr(
        redis, "Redis", lambda **kwargs: SimpleNamespace(ping=lambda: True, close=lambda: None)
    )
    commands = []

    def run(args, **kwargs):
        commands.append((args, kwargs))
        return SimpleNamespace(returncode=1 if args[-1] == "status" else 0)

    monkeypatch.setattr(launcher.subprocess, "run", run)
    processes = []

    class Process:
        def __init__(self, args, **kwargs):
            self.args, self.env = args, kwargs["env"]
            self.exited = args[-1] == "poll"
            self.stopped = False
            processes.append(self)

        def poll(self):
            return 1 if self.exited else None

        def terminate(self):
            self.stopped = True
            self.exited = True

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(launcher.subprocess, "Popen", Process)
    with pytest.raises(launcher.LaunchError, match="bot"):
        launcher.main()
    assert len(processes) == 4
    assert all(p.stopped for p in processes[:-1])
    for process in processes:
        assert "@127.0.0.1:55432/shop_local" in process.env["DATABASE_URL"]
        assert process.env["APP_ENV"] == "development"
        assert "MIGRATION_DATABASE_URL" not in process.env
    assert any(args[-1] == "stop" for args, _ in commands)
    migration = next(kwargs["env"] for args, kwargs in commands if "alembic" in args)
    assert "shop_migrator:" in migration["MIGRATION_DATABASE_URL"]
    assert "@postgres/" in (launcher.ROOT / ".env").read_text()


def test_invalid_token_does_not_start_services_or_leak_secret(launcher):
    (launcher.ROOT / ".env").write_text(
        "BOT_TOKEN=PRIVATE_INVALID_TOKEN\nADMIN_TELEGRAM_IDS=[100]\nSUPPORT_CONTACT=@support\n"
    )
    with pytest.raises(launcher.LaunchError) as caught:
        launcher.main()
    assert "PRIVATE_INVALID_TOKEN" not in str(caught.value)
    launcher.prepare_database.assert_not_awaited()
