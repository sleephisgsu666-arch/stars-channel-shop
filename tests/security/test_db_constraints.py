import pytest
from sqlalchemy.exc import IntegrityError
from app.db.models import Plan, Entitlement


async def test_database_rejects_null_fixed_duration(db, seeded):
    with pytest.raises(IntegrityError):
        async with db.transaction() as s:
            s.add(
                Plan(
                    product_id=seeded.product.id,
                    name="bad",
                    billing_type="fixed",
                    price_stars=1,
                    duration_days=None,
                )
            )


async def test_database_rejects_duplicate_entitlement(db, seeded):
    with pytest.raises(IntegrityError):
        async with db.transaction() as s:
            s.add_all([Entitlement(user_id=seeded.user.id, product_id=seeded.product.id) for _ in range(2)])
