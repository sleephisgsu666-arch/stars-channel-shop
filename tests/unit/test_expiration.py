from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from app.services.entitlements import calculate_expiration, active

AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


def grant(kind="fixed", days=30, at=AT, **kwargs):
    return SimpleNamespace(
        kind=kind, duration_days=days, created_at=at, revoked=False, period_end=kwargs.get("end")
    )


def test_fixed_stacks_remaining():
    a, b = grant(), grant(at=AT + timedelta(days=10))
    assert calculate_expiration([a, b]) == (True, AT + timedelta(days=60))


def test_fixed_after_expiration():
    assert calculate_expiration([grant(), grant(at=AT + timedelta(days=90))])[1] == AT + timedelta(days=120)


def test_lifetime_not_shortened():
    assert calculate_expiration([grant("lifetime", None), grant()]) == (True, None)


def test_free_lifetime():
    assert calculate_expiration([grant("free", None)]) == (True, None)


def test_recurring_out_of_order():
    later = grant("recurring_30d", end=AT + timedelta(days=60))
    earlier = grant("recurring_30d", end=AT + timedelta(days=30))
    assert calculate_expiration([later, earlier])[1] == later.period_end


def test_refund_preserves_other_purchases():
    a, b = grant(), grant(at=AT + timedelta(days=10))
    a.revoked = True
    assert calculate_expiration([a, b])[1] == AT + timedelta(days=40)


def test_expiration_boundary():
    ent = SimpleNamespace(status="active", expires_at=AT)
    assert not active(ent, AT)
    assert active(ent, AT - timedelta(seconds=1))
