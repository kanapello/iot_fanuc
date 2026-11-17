"""Access model: roles granted on nodes of the hierarchy and the scope computed from them.

    organization -> site -> area -> machine

A grant on a node covers everything below it. What each role may do is fixed
here in one table; there is no configurable permission matrix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property

# Permissions
MACHINE_VIEW = "machine.view"  # machines, live state
OPERATOR_INPUT = "operator.input"  # downtime reason, scrap, setup mark
ANALYTICS_VIEW = "analytics.view"  # timeline, OEE, reports
JOURNAL_EDIT = "journal.edit"  # correct downtime reasons and scrap
REFERENCE_VIEW = "reference.view"  # shifts, reasons, norms, rules
NORMS_EDIT = "norms.edit"  # cycle norms / programs
REFERENCE_EDIT = "reference.edit"  # shifts, reasons, rules
STRUCTURE_EDIT = "structure.edit"  # sites, areas, machines, collectors, kiosk tokens
USERS_MANAGE = "users.manage"  # users and their roles
COLLECTORS_VIEW = "collectors.view"  # collector health

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "operator": frozenset({MACHINE_VIEW, OPERATOR_INPUT}),
    "master": frozenset({MACHINE_VIEW, OPERATOR_INPUT, ANALYTICS_VIEW, JOURNAL_EDIT, REFERENCE_VIEW}),
    "engineer": frozenset({MACHINE_VIEW, ANALYTICS_VIEW, REFERENCE_VIEW, NORMS_EDIT, COLLECTORS_VIEW}),
    "admin": frozenset(
        {
            MACHINE_VIEW,
            OPERATOR_INPUT,
            ANALYTICS_VIEW,
            JOURNAL_EDIT,
            REFERENCE_VIEW,
            NORMS_EDIT,
            REFERENCE_EDIT,
            STRUCTURE_EDIT,
            USERS_MANAGE,
            COLLECTORS_VIEW,
        }
    ),
    # employee of the service company: machines, states, alarms and running hours of every customer
    "service": frozenset({MACHINE_VIEW, ANALYTICS_VIEW, COLLECTORS_VIEW}),
    # device tokens
    "kiosk": frozenset({MACHINE_VIEW, OPERATOR_INPUT}),  # operator screen of one machine
    "display": frozenset({MACHINE_VIEW}),  # TV dashboard of one area
}
USER_ROLES = ("operator", "master", "engineer", "admin", "service")
ALL_PERMISSIONS = frozenset().union(*ROLE_PERMISSIONS.values())


@dataclass(frozen=True, slots=True)
class Grant:
    role: str
    org_id: int | None = None  # None only for "service": every organization
    site_id: int | None = None
    area_id: int | None = None
    machine_id: int | None = None  # kiosk token of one machine

    def allows(self, permission: str) -> bool:
        return permission in ROLE_PERMISSIONS.get(self.role, frozenset())


@dataclass(frozen=True, slots=True)
class MachineNode:
    id: int
    org_id: int
    site_id: int
    area_id: int | None


@dataclass(frozen=True)
class Scope:
    """Who is asking and what they may reach. Built once per request, passed explicitly."""

    name: str
    kind: str  # user | device | system
    user_id: int | None = None
    superadmin: bool = False
    grants: tuple[Grant, ...] = ()
    machines: tuple[MachineNode, ...] = ()  # every machine of the installation
    sites: dict[int, int] = field(default_factory=dict)  # site_id -> org_id
    areas: dict[int, int] = field(default_factory=dict)  # area_id -> site_id

    # ------------------------------------------------------------ machines

    def _covers(self, grant: Grant, machine: MachineNode) -> bool:
        if grant.machine_id is not None:
            return grant.machine_id == machine.id
        return (
            (grant.org_id is None or grant.org_id == machine.org_id)
            and (grant.site_id is None or grant.site_id == machine.site_id)
            and (grant.area_id is None or grant.area_id == machine.area_id)
        )

    @cached_property
    def _by_permission(self) -> dict[str, frozenset[int]]:
        result = {}
        for permission in ALL_PERMISSIONS:
            result[permission] = frozenset(
                m.id
                for m in self.machines
                if self.superadmin or any(g.allows(permission) and self._covers(g, m) for g in self.grants)
            )
        return result

    def machine_ids(self, permission: str = MACHINE_VIEW) -> frozenset[int]:
        return self._by_permission.get(permission, frozenset())

    def can_machine(self, permission: str, machine_id: int) -> bool:
        return machine_id in self.machine_ids(permission)

    # ------------------------------------------------------------ nodes

    def can_node(self, permission: str, org_id: int, site_id: int | None = None, area_id: int | None = None) -> bool:
        """Authority over a node itself: a grant on this node or above it."""
        if self.superadmin:
            return True
        return any(
            g.allows(permission)
            and g.machine_id is None
            and (g.org_id is None or g.org_id == org_id)
            and (g.site_id is None or g.site_id == site_id)
            and (g.area_id is None or g.area_id == area_id)
            for g in self.grants
        )

    def can_read_node(self, permission: str, org_id: int, site_id: int | None = None) -> bool:
        """Read a node: authority over it, or over something below it (organization defaults apply to its sites)."""
        if self.can_node(permission, org_id, site_id):
            return True
        return any(
            g.allows(permission)
            and g.machine_id is None
            and g.org_id == org_id
            and (site_id is None or g.site_id == site_id)
            for g in self.grants
        )

    def has(self, permission: str) -> bool:
        return self.superadmin or any(g.allows(permission) for g in self.grants)

    @cached_property
    def visible_site_ids(self) -> frozenset[int]:
        """Sites the principal has anything to do with."""
        if self.superadmin:
            return frozenset(self.sites)
        visible = {m.site_id for m in self.machines if m.id in self.machine_ids(MACHINE_VIEW)}
        for grant in self.grants:
            if grant.machine_id is not None or grant.area_id is not None:
                continue
            visible |= {
                site
                for site, org in self.sites.items()
                if (grant.org_id is None or grant.org_id == org) and (grant.site_id is None or grant.site_id == site)
            }
        return frozenset(visible)

    @cached_property
    def visible_org_ids(self) -> frozenset[int]:
        orgs = {self.sites[s] for s in self.visible_site_ids}
        orgs |= {g.org_id for g in self.grants if g.org_id is not None}
        return frozenset(orgs)

    def visible_area_ids(self) -> frozenset[int]:
        if self.superadmin:
            return frozenset(self.areas)
        result = {m.area_id for m in self.machines if m.area_id is not None and m.id in self.machine_ids(MACHINE_VIEW)}
        for area, site in self.areas.items():
            org = self.sites.get(site)
            if org is not None and any(
                g.machine_id is None
                and (g.org_id is None or g.org_id == org)
                and (g.site_id is None or g.site_id == site)
                and (g.area_id is None or g.area_id == area)
                for g in self.grants
            ):
                result.add(area)
        return frozenset(result)
