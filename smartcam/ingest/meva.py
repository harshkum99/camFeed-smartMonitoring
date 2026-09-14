"""The MEVA public dataset, set up as a site we can import into and ask questions about.

Everything that ties imported footage to a tenant, a site and a camera lives here and is fixed in
code: identifiers are derived, not read from the footage, so nothing inside a video file can
choose which customer its rows belong to.

**Recorder timezone.** MEVA was collected at Muscatatuck, Indiana, on US Eastern time, and the
clip filenames are local wall-clock time. 11 March 2018 is the day US clocks went forward, so that
day is 23 hours long and 11:55 local is 15:55 UTC. Importing with any other timezone places every
track hours away from when it happened, silently — which is why the profile carries it and the
importer refuses a mismatch rather than accepting whatever it is given.

**Only visible-light cameras are mapped.** MEVA's 352x240 cameras (G474–G479) are thermal IR, and
nothing in our detector benchmark says anything about thermal footage. An unmapped camera is never
imported, so it cannot produce numbers nobody has validated.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

NAMESPACE = uuid.NAMESPACE_URL


def _uuid(name: str, ns: uuid.UUID = NAMESPACE) -> str:
    return str(uuid.uuid5(ns, name))


@dataclass(frozen=True)
class SiteProfile:
    key: str
    tenant_id: str
    tenant_name: str
    legal_basis: str
    site_id: str
    site_name: str
    tz: str
    attribution: str
    examples: list[str]
    #: filename camera key -> (camera_id, display name)
    cameras: dict[str, tuple[str, str]] = field(default_factory=dict)
    source_prefix: str | None = None


MEVA_TENANT = _uuid("smartcam:tenant:meva-public")
MEVA_SITE = _uuid("smartcam:site:meva-muscatatuck")

MEVA_ATTRIBUTION = (
    '"Multiview Extended Video with Activities" (MEVA) dataset by Kitware Inc. and the '
    "Intelligence Advanced Research Projects Activity (IARPA), licensed under CC BY 4.0 "
    "(https://creativecommons.org/licenses/by/4.0/). Source: mevadata.org."
)


def _camera(key: str, name: str) -> tuple[str, tuple[str, str]]:
    return key, (_uuid(f"camera:{key}", uuid.UUID(MEVA_SITE)), name)


def meva_profile() -> SiteProfile:
    return SiteProfile(
        key="meva",
        tenant_id=MEVA_TENANT,
        tenant_name="MEVA public footage (CC BY 4.0)",
        # MEVA's subjects were recruited participants in a staged collection. 'consent' is the
        # closest of the bases the schema allows; it is recorded, not relied on for anything.
        legal_basis="consent",
        site_id=MEVA_SITE,
        site_name="MEVA Muscatatuck, Indiana (recorded)",
        tz="America/New_York",
        attribution=MEVA_ATTRIBUTION,
        examples=[
            "How many people were at Admin G329 between 11:55 and 12:00 today?",
            "Show me anyone at Admin G326 between 13:50 and 13:55 today",
            "How many people were at the admin cameras between 12:00 and 13:50 today?",
            "Was anyone at Bus G340 between 11:55 and 12:00 today?",
            "How many people were not wearing a helmet at Admin G329 today?",
        ],
        cameras=dict([
            _camera("admin.G326", "Admin G326 indoor"),
            _camera("admin.G329", "Admin G329 indoor"),
            _camera("bus.G340", "Bus G340 outdoor"),
        ]),
        source_prefix="s3://mevadata-public-01/drops-123-r13/",
    )


PROFILES = {"meva": meva_profile}
