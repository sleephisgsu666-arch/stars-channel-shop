import json
import logging
from uuid import uuid4
import pytest
from pydantic import ValidationError
from app.core.security import authorized, Denied, uuid_value, secret_matches, lock_key
from app.core.logging import SafeFormatter
from app.schemas.admin import PlanInput
from app.core.config import Settings


@pytest.mark.parametrize("value", ["", "x" * 100, "1';DROP TABLE users;--", "../etc/passwd", "0000"])
def test_invalid_ids(value):
    with pytest.raises(Denied):
        uuid_value(value)


def test_uuid_roundtrip():
    value = uuid4()
    assert uuid_value(str(value)) == value


def test_admin_id_only():
    authorized(100, [100])
    for value in (101, "100", True):
        with pytest.raises(Denied):
            authorized(value, [100])


def test_secret_comparison():
    assert secret_matches("a" * 40, "a" * 40)
    assert not secret_matches("", "a" * 40)
    assert not secret_matches("б", "a" * 40)


def test_lock_stable():
    assert lock_key("a") == lock_key("a") != lock_key("b")


def test_secret_redaction():
    f = SafeFormatter(["EXACT_SECRET"])
    record = logging.LogRecord(
        "test",
        40,
        "",
        1,
        "EXACT_SECRET https://t.me/+private postgresql+asyncpg://u:pass@host/db 123456789:" + "a" * 35,
        (),
        None,
    )
    output = f.format(record)
    assert all(secret not in output for secret in ("EXACT_SECRET", "+private", "pass@", "a" * 35))
    assert json.loads(output)["level"] == "ERROR"


@pytest.mark.parametrize(
    "kind,price,days",
    [
        ("free", 1, None),
        ("fixed", 0, 30),
        ("fixed", 5, None),
        ("recurring_30d", 10001, 30),
        ("recurring_30d", 5, 90),
        ("lifetime", 5, 30),
    ],
)
def test_plan_rules(kind, price, days):
    with pytest.raises(ValidationError):
        PlanInput.model_validate_json(
            json.dumps(
                dict(
                    product_id=str(uuid4()),
                    name="x",
                    billing_type=kind,
                    price_stars=price,
                    duration_days=days,
                )
            )
        )


def test_no_insecure_config_defaults():
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_long_input_and_extra_fields():
    data = dict(product_id=str(uuid4()), name="x" * 129, billing_type="free", price_stars=0)
    with pytest.raises(ValidationError):
        PlanInput.model_validate_json(json.dumps(data))
    data["name"] = "x"
    data["shell"] = "rm -rf /"
    with pytest.raises(ValidationError):
        PlanInput.model_validate_json(json.dumps(data))
