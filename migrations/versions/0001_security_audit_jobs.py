"""RBAC, ABAC, audit and job persistence."""
from alembic import op
import sqlalchemy as sa

revision = "0001_security_audit_jobs"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("principals", sa.Column("id", sa.String(32), primary_key=True), sa.Column("subject", sa.String(255), nullable=False, unique=True), sa.Column("provider", sa.String(64), nullable=False), sa.Column("attributes", sa.JSON(), nullable=False), sa.Column("active", sa.Boolean(), nullable=False)); op.create_index("ix_principals_subject", "principals", ["subject"])
    op.create_table("roles", sa.Column("id", sa.String(32), primary_key=True), sa.Column("name", sa.String(128), nullable=False, unique=True))
    op.create_table("permissions", sa.Column("id", sa.String(32), primary_key=True), sa.Column("name", sa.String(128), nullable=False, unique=True))
    op.create_table("principal_roles", sa.Column("principal_id", sa.String(32), sa.ForeignKey("principals.id", ondelete="CASCADE"), primary_key=True), sa.Column("role_id", sa.String(32), sa.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True))
    op.create_table("role_permissions", sa.Column("role_id", sa.String(32), sa.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True), sa.Column("permission_id", sa.String(32), sa.ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True))
    op.create_table("abac_policies", sa.Column("id", sa.String(32), primary_key=True), sa.Column("name", sa.String(255), nullable=False, unique=True), sa.Column("effect", sa.String(16), nullable=False), sa.Column("action", sa.String(128), nullable=False), sa.Column("resource", sa.String(255), nullable=False), sa.Column("condition", sa.JSON(), nullable=False), sa.Column("priority", sa.Integer(), nullable=False), sa.Column("active", sa.Boolean(), nullable=False)); op.create_index("ix_abac_policies_action", "abac_policies", ["action"])
    op.create_table("audit_events", sa.Column("id", sa.String(32), primary_key=True), sa.Column("created_at", sa.Float(), nullable=False), sa.Column("event", sa.String(128), nullable=False), sa.Column("outcome", sa.String(32), nullable=False), sa.Column("subject", sa.String(255)), sa.Column("request_id", sa.String(128)), sa.Column("payload", sa.JSON(), nullable=False)); op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])
    op.create_table("jobs", sa.Column("id", sa.String(32), primary_key=True), sa.Column("kind", sa.String(64), nullable=False), sa.Column("status", sa.String(32), nullable=False), sa.Column("progress", sa.Integer(), nullable=False), sa.Column("cancel_requested", sa.Boolean(), nullable=False), sa.Column("created_at", sa.Float(), nullable=False), sa.Column("started_at", sa.Float()), sa.Column("finished_at", sa.Float()), sa.Column("payload", sa.JSON(), nullable=False), sa.Column("result", sa.JSON()), sa.Column("error", sa.Text()))

def downgrade():
    for table in ("jobs", "audit_events", "abac_policies", "role_permissions", "principal_roles", "permissions", "roles", "principals"): op.drop_table(table)
