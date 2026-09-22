"""Request identity, backed by OpenID Connect.

Tokens are issued by Keycloak (realm `agentops`) and validated here against the
realm's JWKS. This module is the only place that reads the token; routes depend
on the functions below and receive an already-trusted CurrentUser.

Two rules this module exists to enforce:

  * Identity never comes from the request body or a UI-supplied value. The
    customer a caller may act as comes from the `customer_id` token claim, which
    only Keycloak can set.
  * Roles come from the token's realm_access claim, not from anything the client
    can influence.

Configuration (see .env):
    OIDC_ISSUER     e.g. http://keycloak.agentops.local/realms/agentops
    OIDC_AUDIENCE   e.g. agentops-app
    OIDC_REQUIRED   "false" disables enforcement for local work without Keycloak
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

import httpx
import jwt
from fastapi import Depends, Header, HTTPException
from jwt import PyJWKClient

# Realm roles, as configured in Keycloak.
ROLE_CUSTOMER = "customer"
ROLE_SALES = "sales_executive"
ROLE_MANAGER = "manager"

# Staff are anyone who may see orders that are not their own.
STAFF_ROLES = frozenset({ROLE_SALES, ROLE_MANAGER})
# Only a manager may approve a refund above the auto-approval threshold.
APPROVER_ROLES = frozenset({ROLE_MANAGER})

_ISSUER = os.getenv("OIDC_ISSUER", "http://keycloak.agentops.local/realms/agentops")
_AUDIENCE = os.getenv("OIDC_AUDIENCE", "agentops-app")
_REQUIRED = os.getenv("OIDC_REQUIRED", "true").lower() not in ("false", "0", "no")

# The browser reaches Keycloak on the gateway hostname, which is also the issuer
# string embedded in tokens. The API may resolve that name differently (or not
# at all), so JWKS fetching can be pointed elsewhere without changing the
# issuer we verify against.
_JWKS_URL = os.getenv(
    "OIDC_JWKS_URL",
    _ISSUER.rstrip("/") + "/protocol/openid-connect/certs",
)

_jwk_client: PyJWKClient | None = None


def _jwks() -> PyJWKClient:
    """Lazily build the JWKS client so import never depends on the network."""
    global _jwk_client
    if _jwk_client is None:
        # PyJWKClient caches keys and refetches on unknown kid, which is what we
        # want across a Keycloak key rotation.
        _jwk_client = PyJWKClient(_JWKS_URL, cache_keys=True, lifespan=600)
    return _jwk_client


@dataclass(frozen=True)
class CurrentUser:
    user_id: str
    username: str = ""
    customer_id: str | None = None
    roles: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_staff(self) -> bool:
        return bool(self.roles & STAFF_ROLES)

    @property
    def is_approver(self) -> bool:
        return bool(self.roles & APPROVER_ROLES)


class AuthError(HTTPException):
    def __init__(self, detail: str, status_code: int = 401) -> None:
        super().__init__(
            status_code=status_code,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )


def _decode(token: str) -> dict:
    try:
        key = _jwks().get_signing_key_from_jwt(token).key
    except Exception as exc:  # network failure, unknown kid, malformed token
        raise AuthError(f"Cannot verify token signing key: {exc}") from exc

    try:
        return jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=_AUDIENCE,
            issuer=_ISSUER,
            options={"require": ["exp", "iat", "iss", "sub"]},
            leeway=30,  # tolerate small clock drift between host and cluster
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Token expired") from exc
    except jwt.InvalidAudienceError as exc:
        raise AuthError(f"Token audience is not {_AUDIENCE}") from exc
    except jwt.InvalidIssuerError as exc:
        raise AuthError(f"Token issuer is not {_ISSUER}") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError(f"Invalid token: {exc}") from exc


def _user_from_claims(claims: dict) -> CurrentUser:
    realm_roles = (claims.get("realm_access") or {}).get("roles") or []
    customer_id = claims.get("customer_id") or None
    return CurrentUser(
        user_id=claims["sub"],
        username=claims.get("preferred_username", ""),
        customer_id=customer_id,
        roles=frozenset(realm_roles),
    )


async def get_current_user(
    authorization: str | None = Header(default=None),
) -> CurrentUser | None:
    """Return the caller's verified identity, or None when anonymous.

    Anonymous is only a valid answer for endpoints that permit it; routes that
    require identity depend on require_user and its narrower variants.
    """
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthError("Expected an Authorization: Bearer <token> header")
    return _user_from_claims(_decode(token))


async def require_user(
    user: CurrentUser | None = Depends(get_current_user),
) -> CurrentUser:
    if user is None:
        if not _REQUIRED:
            # Explicit local-development escape hatch, off by default.
            return CurrentUser(user_id="dev", username="dev",
                               customer_id="CUS-001",
                               roles=frozenset({ROLE_CUSTOMER}))
        raise AuthError("Authentication required")
    return user


async def require_staff(
    user: CurrentUser = Depends(require_user),
) -> CurrentUser:
    if not user.is_staff:
        raise HTTPException(status_code=403, detail="Staff role required")
    return user


async def require_approver(
    user: CurrentUser = Depends(require_user),
) -> CurrentUser:
    if not user.is_approver:
        raise HTTPException(status_code=403, detail="Manager role required")
    return user


def scope_customer_id(user: CurrentUser, requested: str | None = None) -> str | None:
    """Decide which customer's data this caller may act on.

    Staff may name any customer, or none to see everything. A customer is
    pinned to their own token claim: a requested value that differs is a
    privilege-escalation attempt, not a filter.
    """
    if user.is_staff:
        return requested
    if user.customer_id is None:
        raise HTTPException(
            status_code=403,
            detail="Account has no customer_id claim; ask an administrator",
        )
    if requested is not None and requested.upper() != user.customer_id.upper():
        raise HTTPException(status_code=403, detail="Not your customer record")
    return user.customer_id


def oidc_settings() -> dict:
    """Non-secret OIDC settings, for a discovery endpoint the SPA can read."""
    return {
        "issuer": _ISSUER,
        "audience": _AUDIENCE,
        "clientId": _AUDIENCE,
        "required": _REQUIRED,
    }


def issuer_reachable(timeout: float = 3.0) -> bool:
    """Best-effort check used by /health to surface misconfiguration early."""
    try:
        r = httpx.get(_JWKS_URL, timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


__all__ = [
    "CurrentUser",
    "get_current_user",
    "require_user",
    "require_staff",
    "require_approver",
    "scope_customer_id",
    "oidc_settings",
    "issuer_reachable",
    "ROLE_CUSTOMER",
    "ROLE_SALES",
    "ROLE_MANAGER",
    "time",
]
