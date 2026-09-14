"""Reject NULL paid-plan duration; make audit history append-only for runtime role."""

from alembic import op

revision = "002_harden_constraints"
down_revision = "4927d71bd40c"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("valid_plan", "plans", type_="check")
    op.create_check_constraint(
        "valid_plan",
        "plans",
        "(billing_type = 'free' AND price_stars = 0 AND (duration_days IS NULL OR duration_days > 0)) OR "
        "(billing_type = 'fixed' AND price_stars > 0 AND duration_days IS NOT NULL AND duration_days > 0) OR "
        "(billing_type = 'lifetime' AND price_stars > 0 AND duration_days IS NULL) OR "
        "(billing_type = 'recurring_30d' AND price_stars BETWEEN 1 AND 10000 "
        "AND duration_days IS NOT NULL AND duration_days = 30)",
    )
    op.execute("""DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'shop_app') THEN
        REVOKE UPDATE ON audit_logs FROM shop_app;
        REVOKE UPDATE ON update_receipts FROM shop_app;
    END IF;
    END $$""")


def downgrade():
    raise RuntimeError("Security constraints cannot be weakened by automatic downgrade.")
