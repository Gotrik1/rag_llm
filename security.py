"""Identity and authorization boundary for the RAG service.

Business code receives a Principal and calls authorize(); it neither parses
tokens nor knows whether the principal came from local development or SSO.
"""
from __future__ import annotations

import os
from functools import lru_cache
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


RAG_ACCESS = "rag.access"
SUPERADMIN = "superadmin"


class AuthenticationError(Exception):
    """The caller did not present a valid identity."""


class AuthorizationError(Exception):
    """The authenticated caller is not permitted to perform an action."""


@dataclass(frozen=True)
class Principal:
    """Immutable authenticated subject, grants and attributes passed across the authorization boundary."""
    subject: str
    roles: frozenset[str] = field(default_factory=frozenset)
    permissions: frozenset[str] = field(default_factory=frozenset)
    attributes: Mapping[str, str] = field(default_factory=dict)
    provider: str = "unknown"


class IdentityProvider(Protocol):
    """Adapter contract for turning an Authorization header into a principal or AuthenticationError."""
    def authenticate(self, authorization: str | None) -> Principal:
        """Validate adapter-specific credentials and return the authenticated subject and mapped grants."""
        ...


class DevelopmentIdentityProvider:
    """Local-only adapter. A missing header is the configured bootstrap admin."""

    def __init__(self, bootstrap_subject: str) -> None:
        self.bootstrap_subject = bootstrap_subject

    def authenticate(self, authorization: str | None) -> Principal:
        """Interpret the test/development token format; missing headers use the configured bootstrap identity."""
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

    def __init__(self) -> None:
        import jwt
        self.jwt = jwt
        self.issuer = os.environ.get("RAG_OIDC_ISSUER", "").rstrip("/")
        self.audience = os.environ.get("RAG_OIDC_AUDIENCE", "")
        jwks_url = os.environ.get("RAG_OIDC_JWKS_URL", f"{self.issuer}/.well-known/jwks.json")
        if not self.issuer or not self.audience:
            raise RuntimeError("RAG_OIDC_ISSUER and RAG_OIDC_AUDIENCE are required")
        self.jwks = jwt.PyJWKClient(jwks_url, cache_keys=True)

    @staticmethod
    def _values(value: object) -> frozenset[str]:
        if isinstance(value, str): return frozenset(filter(None, value.replace(",", " ").split()))
        if isinstance(value, list): return frozenset(str(item) for item in value)
        return frozenset()

    def authenticate(self, authorization: str | None) -> Principal:
        """Validate JWT signature, issuer, audience and required time/subject claims before mapping grants."""
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise AuthenticationError("Expected a Bearer token")
        try:
            key = self.jwks.get_signing_key_from_jwt(token)
            claims = self.jwt.decode(token, key.key, algorithms=["RS256", "ES256"], issuer=self.issuer, audience=self.audience, options={"require": ["exp", "iat", "sub"]})
        except Exception as exc:
            raise AuthenticationError("Invalid SSO token") from exc
        role_claim = os.getenv("RAG_OIDC_ROLE_CLAIM", "roles")
        permission_claim = os.getenv("RAG_OIDC_PERMISSION_CLAIM", "permissions")
        attribute_names = [item.strip() for item in os.getenv("RAG_OIDC_ATTRIBUTE_CLAIMS", "department,tenant,clearance").split(",") if item.strip()]
        return Principal(
            subject=str(claims["sub"]), roles=self._values(claims.get(role_claim)),
            permissions=self._values(claims.get(permission_claim)),
            attributes={name: str(claims[name]) for name in attribute_names if name in claims}, provider="oidc",
        )


@lru_cache(maxsize=2)
def configured_identity_provider() -> IdentityProvider:
    """Build and cache the configured development or OIDC adapter; reject unsupported authentication modes."""
    mode = os.getenv("RAG_AUTH_MODE", "development").lower()
    if mode == "development":
        return DevelopmentIdentityProvider(os.getenv("BOOTSTRAP_SUPERADMIN_SUBJECT", "rag-superadmin"))
    if mode == "oidc":
        return OidcIdentityProvider()
    raise RuntimeError(f"Unsupported RAG_AUTH_MODE: {mode}")


def authorize(principal: Principal, action: str, resource: str = "rag", context: Mapping[str, Any] | None = None) -> None:
    """RBAC plus ABAC decision point; explicit ABAC deny takes precedence."""
    if SUPERADMIN in principal.roles or "*" in principal.permissions:
        return
    from policy import policy_provider
    decision = policy_provider().decide(principal, action, resource, context)
    if decision is False:
        raise AuthorizationError(f"Policy denied: {action}")
    if action in principal.permissions or decision is True:
        return
    raise AuthorizationError(f"Permission required: {action}")
