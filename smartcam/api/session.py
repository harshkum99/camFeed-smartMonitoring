"""Who is asking, and what they are allowed to see.

Deliberately a seam rather than a real auth system yet, but the *shape* is the important part
and it is fixed now: `tenant_id`, `site_id` and the permitted camera list come from the session
and are never read from a request body. Every layer below assumes that — the filter compiler
injects tenant and site itself precisely so a crafted request cannot reach another customer's
footage — and a PoC that got this backwards would have to be unpicked later, through code that
by then assumes it is safe.

For now identity comes from headers or a single-tenant default. Swapping that for real
authentication is one function; swapping it after a hundred call sites have learned to pass a
tenant_id around is not.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from fastapi import Header, HTTPException

DEFAULT_TENANT = os.environ.get("SMARTCAM_TENANT", "11111111-1111-1111-1111-111111111111")
DEFAULT_SITE = os.environ.get("SMARTCAM_SITE", "aaaaaaaa-0000-0000-0000-000000000001")


@dataclass(frozen=True)
class Session:
    tenant_id: str
    site_id: str
    actor: str
    #: None means "every camera at this site". A list is a hard ceiling: a question may narrow
    #: within it but never widen past it.
    camera_ids: list[str] | None = None
    roles: frozenset[str] = field(default_factory=lambda: frozenset({"operator"}))

    def require(self, role: str) -> None:
        if role not in self.roles:
            raise HTTPException(403, f"this action needs the '{role}' role")


async def current_session(
    x_smartcam_actor: str | None = Header(default=None),
    x_smartcam_tenant: str | None = Header(default=None),
    x_smartcam_site: str | None = Header(default=None),
    x_smartcam_roles: str | None = Header(default=None),
) -> Session:
    """Resolve the caller.

    The headers exist so the console and the tests can act as different people. They are NOT a
    security boundary and must not become one — when real auth arrives it replaces this function
    entirely, and nothing above it changes.
    """
    return Session(
        tenant_id=x_smartcam_tenant or DEFAULT_TENANT,
        site_id=x_smartcam_site or DEFAULT_SITE,
        actor=x_smartcam_actor or "console",
        roles=frozenset((x_smartcam_roles or "operator,admin").split(",")),
    )
