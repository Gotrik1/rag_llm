"""Identity and authorization boundary for the RAG service.

Business code receives a Principal and calls authorize(); it neither parses
tokens nor knows whether the principal came from local development or SSO.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Protocol


RAG_ACCESS = "rag.access"
SUPERADMIN = "superadmin"


class AuthenticationError(Exception):
    """The caller did not present a valid identity."""


class AuthorizationError(Exception):
    """The authenticated caller is not permitted to perform an action."""


@dataclass(frozen=True)
class Principal:
    subject: str
    roles: frozenset[str] = field(default_factory=frozenset)
    permissions: frozenset[str] = field(default_factory=frozenset)
    attributes: Mapping[str, str] = field(default_factory=dict)
    provider: str = "unknown"


class IdentityProvider(Protocol):
    def authenticate(self, authorization: str | None) -> Principal: ...


class DevelopmentIdentityProvider:
    """Local-only adapter. A missing header is the configured bootstrap admin."""

    def __init__(self, bootstrap_subject: str) -> None:
        self.bootstrap_subject = bootstrap_subject

    def authenticate(self, authorization: str | None) -> Principal:
        if not authorization:
            return Principal(
                subject=self.bootstrap_subject,
                roles=frozenset({SUPERADMIN}),
                permissions=frozenset({"*"}),
                provider="development",
            )
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise AuthenticationError("Expected a Bearer token")
        # Deliberately simple, non-secret development token format. It is never
        # enabled in production and makes integration tests deterministic.
        subject, _, permissions = token.strip().partition(":")
        if not subject:
            raise AuthenticationError("Development token has no subject")
        granted = frozenset(filter(None, permissions.split(",")))
        return Principal(subject=subject, permissions=granted, provider="development")


class OidcIdentityProvider:
    """Integration seam for corporate SSO.

    The production adapter must validate JWT signature through JWKS, issuer,
    audience and time claims before mapping claims into Principal. Keeping this
    class separate prevents SSO details from leaking into route handlers.
    """

    def authenticate(self, authorization: str | None) -> Principal:
        raise AuthenticationError("OIDC adapter is not configured")


def configured_identity_provider() -> IdentityProvider:
    mode = os.getenv("RAG_AUTH_MODE", "development").lower()
    if mode == "development":
        return DevelopmentIdentityProvider(os.getenv("BOOTSTRAP_SUPERADMIN_SUBJECT", "rag-superadmin"))
    if mode == "oidc":
        return OidcIdentityProvider()
    raise RuntimeError(f"Unsupported RAG_AUTH_MODE: {mode}")


def authorize(principal: Principal, action: str, resource: str = "rag", context: Mapping[str, str] | None = None) -> None:
    """RBAC decision point; ``context`` is intentionally reserved for ABAC."""
    del resource, context
    if SUPERADMIN in principal.roles or "*" in principal.permissions or action in principal.permissions:
        return
    raise AuthorizationError(f"Permission required: {action}")
