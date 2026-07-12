"""PostgreSQL persistence for identities, policies, audit events and jobs."""
from __future__ import annotations

import os
import time
import uuid
from typing import Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text, create_engine, delete, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from security import Principal, SUPERADMIN


class Base(DeclarativeBase):
    pass


class PrincipalRow(Base):
    __tablename__ = "principals"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    subject: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(64))
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class RoleRow(Base):
    __tablename__ = "roles"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)


class PermissionRow(Base):
    __tablename__ = "permissions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)


class PrincipalRoleRow(Base):
    __tablename__ = "principal_roles"
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id", ondelete="CASCADE"), primary_key=True)
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)


class RolePermissionRow(Base):
    __tablename__ = "role_permissions"
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)
    permission_id: Mapped[str] = mapped_column(ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True)


class PolicyRow(Base):
    __tablename__ = "abac_policies"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    effect: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(128), index=True)
    resource: Mapped[str] = mapped_column(String(255), default="*")
    condition: Mapped[dict] = mapped_column(JSON, default=dict)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class AuditEventRow(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[float] = mapped_column(Float, index=True)
    event: Mapped[str] = mapped_column(String(128), index=True)
    outcome: Mapped[str] = mapped_column(String(32))
    subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class JobRow(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[float] = mapped_column(Float)
    started_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Persistence:
    def __init__(self, url: str | None = None) -> None:
        self.url = url if url is not None else os.getenv("RAG_DATABASE_URL", "")
        self.engine = create_engine(self.url, pool_pre_ping=True) if self.url else None

    @property
    def enabled(self) -> bool:
        return self.engine is not None

    def bootstrap_superadmin(self, subject: str) -> None:
        if not self.engine:
            return
        with Session(self.engine) as session:
            principal = session.scalar(select(PrincipalRow).where(PrincipalRow.subject == subject))
            if principal is None:
                principal = PrincipalRow(id=uuid.uuid4().hex, subject=subject, provider="bootstrap", attributes={})
                session.add(principal)
            role = session.scalar(select(RoleRow).where(RoleRow.name == SUPERADMIN))
            if role is None:
                role = RoleRow(id=uuid.uuid4().hex, name=SUPERADMIN); session.add(role)
            permission = session.scalar(select(PermissionRow).where(PermissionRow.name == "*"))
            if permission is None:
                permission = PermissionRow(id=uuid.uuid4().hex, name="*"); session.add(permission)
            session.flush()
            if session.get(PrincipalRoleRow, (principal.id, role.id)) is None:
                session.add(PrincipalRoleRow(principal_id=principal.id, role_id=role.id))
            if session.get(RolePermissionRow, (role.id, permission.id)) is None:
                session.add(RolePermissionRow(role_id=role.id, permission_id=permission.id))
            session.commit()

    def ping(self) -> bool:
        if not self.engine: return True
        from sqlalchemy import text
        with self.engine.connect() as connection: connection.execute(text("SELECT 1"))
        return True

    def enrich_principal(self, principal: Principal) -> Principal:
        if not self.engine:
            return principal
        with Session(self.engine) as session:
            row = session.scalar(select(PrincipalRow).where(PrincipalRow.subject == principal.subject, PrincipalRow.active.is_(True)))
            if row is None:
                row = PrincipalRow(id=uuid.uuid4().hex, subject=principal.subject, provider=principal.provider, attributes=dict(principal.attributes))
                session.add(row); session.commit(); session.refresh(row)
            roles = set(session.scalars(select(RoleRow.name).join(PrincipalRoleRow, PrincipalRoleRow.role_id == RoleRow.id).where(PrincipalRoleRow.principal_id == row.id)))
            permissions = set(session.scalars(select(PermissionRow.name).join(RolePermissionRow, RolePermissionRow.permission_id == PermissionRow.id).join(PrincipalRoleRow, PrincipalRoleRow.role_id == RolePermissionRow.role_id).where(PrincipalRoleRow.principal_id == row.id)))
            return Principal(principal.subject, frozenset(roles) | principal.roles, frozenset(permissions) | principal.permissions, {**row.attributes, **principal.attributes}, principal.provider)

    def active_policies(self, action: str) -> list[dict[str, Any]]:
        if not self.engine:
            return []
        with Session(self.engine) as session:
            rows = session.scalars(select(PolicyRow).where(PolicyRow.active.is_(True), PolicyRow.action.in_([action, "*"])).order_by(PolicyRow.priority.desc())).all()
            return [{"name": r.name, "effect": r.effect, "action": r.action, "resource": r.resource, "condition": r.condition, "priority": r.priority} for r in rows]

    def upsert_policy(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.engine: raise RuntimeError("RAG_DATABASE_URL is required")
        with Session(self.engine) as session:
            row = session.scalar(select(PolicyRow).where(PolicyRow.name == payload["name"]))
            if row is None:
                row = PolicyRow(id=uuid.uuid4().hex, name=payload["name"]); session.add(row)
            row.effect = payload["effect"]; row.action = payload["action"]; row.resource = payload.get("resource", "*")
            row.condition = payload.get("condition", {}); row.priority = int(payload.get("priority", 0)); row.active = bool(payload.get("active", True))
            session.commit()
            return {"id": row.id, **{key: getattr(row, key) for key in ("name", "effect", "action", "resource", "condition", "priority", "active")}}

    def grant_role(self, subject: str, role_name: str, permissions: list[str]) -> None:
        if not self.engine: raise RuntimeError("RAG_DATABASE_URL is required")
        with Session(self.engine) as session:
            principal = session.scalar(select(PrincipalRow).where(PrincipalRow.subject == subject))
            if principal is None:
                principal = PrincipalRow(id=uuid.uuid4().hex, subject=subject, provider="external", attributes={}); session.add(principal)
            role = session.scalar(select(RoleRow).where(RoleRow.name == role_name))
            if role is None:
                role = RoleRow(id=uuid.uuid4().hex, name=role_name); session.add(role)
            session.flush()
            if session.get(PrincipalRoleRow, (principal.id, role.id)) is None: session.add(PrincipalRoleRow(principal_id=principal.id, role_id=role.id))
            for name in permissions:
                permission = session.scalar(select(PermissionRow).where(PermissionRow.name == name))
                if permission is None:
                    permission = PermissionRow(id=uuid.uuid4().hex, name=name); session.add(permission); session.flush()
                if session.get(RolePermissionRow, (role.id, permission.id)) is None: session.add(RolePermissionRow(role_id=role.id, permission_id=permission.id))
            session.commit()

    def record_audit(self, payload: dict[str, Any]) -> None:
        if not self.engine:
            return
        with Session(self.engine) as session:
            session.add(AuditEventRow(id=uuid.uuid4().hex, created_at=float(payload["timestamp"]), event=str(payload["event"]), outcome=str(payload["outcome"]), subject=payload.get("subject"), request_id=payload.get("request_id"), payload=payload))
            session.commit()

    def purge_audit(self, retention_days: int) -> int:
        if not self.engine: return 0
        cutoff = time.time() - retention_days * 86400
        with Session(self.engine) as session:
            result = session.execute(delete(AuditEventRow).where(AuditEventRow.created_at < cutoff))
            session.commit()
            return int(result.rowcount or 0)

    def create_job(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        job = JobRow(id=uuid.uuid4().hex, kind=kind, status="queued", progress=0, created_at=time.time(), payload=payload)
        if self.engine:
            with Session(self.engine, expire_on_commit=False) as session: session.add(job); session.commit()
        return self.job_dict(job)

    def update_job(self, job_id: str, **values: Any) -> None:
        if not self.engine: return
        with Session(self.engine) as session:
            row = session.get(JobRow, job_id)
            if row:
                for key, value in values.items(): setattr(row, key, value)
                session.commit()

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        if not self.engine: return None
        with Session(self.engine) as session:
            row = session.get(JobRow, job_id)
            return self.job_dict(row) if row else None

    def request_cancel(self, job_id: str) -> bool:
        if not self.engine: return False
        with Session(self.engine) as session:
            row = session.get(JobRow, job_id)
            if not row or row.status in {"completed", "failed", "cancelled"}: return False
            row.cancel_requested = True; session.commit(); return True

    @staticmethod
    def job_dict(row: JobRow) -> dict[str, Any]:
        return {key: getattr(row, key) for key in ("id", "kind", "status", "progress", "cancel_requested", "created_at", "started_at", "finished_at", "result", "error")}


_store: Persistence | None = None


def get_store() -> Persistence:
    global _store
    if _store is None: _store = Persistence()
    return _store
