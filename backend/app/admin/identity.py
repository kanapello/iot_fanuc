"""Users, roles on hierarchy nodes, kiosk tokens; building the access scope of a request."""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import service as admin
from app.core.access import Grant, MachineNode, Scope
from app.core.config import Settings
from app.core.security import (
    create_session_token,
    hash_password,
    hash_token,
    new_device_token,
    read_session_token,
    verify_password,
)


async def login(session: AsyncSession, settings: Settings, username: str, password: str) -> str | None:
    row = (
        await session.execute(
            text("SELECT id, password_hash FROM users WHERE login = :login AND is_active"), {"login": username}
        )
    ).first()
    if row is None or not verify_password(password, row.password_hash):
        return None
    return create_session_token(settings, row.id)


async def load_scope(session: AsyncSession, settings: Settings, token: str | None) -> Scope | None:
    """The single place where a token becomes a set of reachable machines and roles."""
    if not token:
        return None
    name, kind, user_id, superadmin = "", "user", None, False
    grants: list[Grant] = []
    if token.startswith("dev_"):
        row = (
            await session.execute(
                text(
                    "SELECT name, machine_id, area_id FROM device_tokens "
                    "WHERE token_hash = :hash AND revoked_at IS NULL"
                ),
                {"hash": hash_token(token)},
            )
        ).first()
        if row is None:
            return None
        name, kind = row.name, "device"
        if row.machine_id is not None:
            grants.append(Grant("kiosk", machine_id=row.machine_id))
        else:
            grants.append(Grant("display", area_id=row.area_id))
    else:
        user_id = read_session_token(settings, token)
        if user_id is None:
            return None
        user = (
            await session.execute(
                text("SELECT login, is_superadmin FROM users WHERE id = :id AND is_active"), {"id": user_id}
            )
        ).first()
        if user is None:
            return None
        name, superadmin = user.login, user.is_superadmin
        rows = await session.execute(
            text("SELECT role, org_id, site_id, area_id FROM memberships WHERE user_id = :id"), {"id": user_id}
        )
        grants = [Grant(r.role, r.org_id, r.site_id, r.area_id) for r in rows]

    tree = await admin.hierarchy(session)
    sites = {s["id"]: s["org_id"] for s in tree["sites"]}
    areas = {a["id"]: a["site_id"] for a in tree["areas"]}
    # a display token names only its area: complete the node so that it cannot match areas elsewhere
    grants = [
        Grant(g.role, sites.get(areas.get(g.area_id, -1), -1), areas.get(g.area_id, -1), g.area_id)
        if g.role == "display"
        else g
        for g in grants
    ]
    return Scope(
        name=name,
        kind=kind,
        user_id=user_id,
        superadmin=superadmin,
        grants=tuple(grants),
        machines=tuple(MachineNode(m["id"], m["org_id"], m["site_id"], m["area_id"]) for m in tree["machines"]),
        sites=sites,
        areas=areas,
    )


# ---------------------------------------------------------------- users and memberships


async def list_users(session: AsyncSession, org_ids: Collection[int] | None) -> list[dict[str, Any]]:
    """Users with their roles. ``org_ids`` = organizations the caller manages; None = everyone (superadmin).

    An organization administrator sees only users who hold a role in that
    organization, and of those roles only the ones inside it.
    """
    everything = org_ids is None
    rows = await session.execute(
        text(
            "SELECT u.id, u.login, u.full_name, u.is_active, u.is_superadmin, "
            "COALESCE(json_agg(json_build_object('id', m.id, 'role', m.role, 'org_id', m.org_id, "
            "'site_id', m.site_id, 'area_id', m.area_id) ORDER BY m.id) FILTER (WHERE m.id IS NOT NULL), '[]') "
            "AS memberships FROM users u LEFT JOIN memberships m ON m.user_id = u.id "
            "AND (:everything OR m.org_id = ANY(:orgs)) "
            "WHERE :everything OR EXISTS ("
            "SELECT 1 FROM memberships x WHERE x.user_id = u.id AND x.org_id = ANY(:orgs)) "
            "GROUP BY u.id ORDER BY u.login"
        ),
        {"everything": everything, "orgs": list(org_ids or [])},
    )
    return [dict(r._mapping) for r in rows]


async def get_user(session: AsyncSession, user_id: int) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text("SELECT id, login, full_name, is_active, is_superadmin FROM users WHERE id = :id"), {"id": user_id}
        )
    ).first()
    return dict(row._mapping) if row else None


async def user_memberships(session: AsyncSession, user_ids: Collection[int]) -> dict[int, list[dict[str, Any]]]:
    """Every role of the given users, in any organization: who may change an account depends on all of them."""
    rows = await session.execute(
        text(
            "SELECT id, user_id, role, org_id, site_id, area_id FROM memberships "
            "WHERE user_id = ANY(:users) ORDER BY id"
        ),
        {"users": list(user_ids)},
    )
    result: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        result.setdefault(row.user_id, []).append(dict(row._mapping))
    return result


async def users_with_role(session: AsyncSession, role: str) -> list[dict[str, Any]]:
    """Active users holding ``role`` somewhere (the executors of repair requests hold "service")."""
    rows = await session.execute(
        text(
            "SELECT u.id, u.login, u.full_name FROM users u WHERE u.is_active AND EXISTS ("
            "SELECT 1 FROM memberships m WHERE m.user_id = u.id AND m.role = :role) ORDER BY u.full_name, u.login"
        ),
        {"role": role},
    )
    return [dict(r._mapping) for r in rows]


async def create_user(session: AsyncSession, login_name: str, password: str, full_name: str) -> int:
    return int(
        (
            await session.execute(
                text("INSERT INTO users (login, password_hash, full_name) VALUES (:login, :hash, :name) RETURNING id"),
                {"login": login_name, "hash": hash_password(password), "name": full_name},
            )
        ).scalar_one()
    )


async def update_user(
    session: AsyncSession, user_id: int, full_name: str | None, password: str | None, is_active: bool | None
) -> None:
    await session.execute(
        text(
            "UPDATE users SET full_name = COALESCE(:name, full_name), is_active = COALESCE(:active, is_active), "
            "password_hash = COALESCE(:hash, password_hash) WHERE id = :id"
        ),
        {"id": user_id, "name": full_name, "active": is_active, "hash": hash_password(password) if password else None},
    )


async def add_membership(
    session: AsyncSession, user_id: int, role: str, org_id: int | None, site_id: int | None, area_id: int | None
) -> int:
    return int(
        (
            await session.execute(
                text(
                    "INSERT INTO memberships (user_id, role, org_id, site_id, area_id) "
                    "VALUES (:user, :role, :org, :site, :area) RETURNING id"
                ),
                {"user": user_id, "role": role, "org": org_id, "site": site_id, "area": area_id},
            )
        ).scalar_one()
    )


async def get_membership(session: AsyncSession, membership_id: int) -> dict[str, Any] | None:
    row = (await session.execute(text("SELECT * FROM memberships WHERE id = :id"), {"id": membership_id})).first()
    return dict(row._mapping) if row else None


async def delete_membership(session: AsyncSession, membership_id: int) -> None:
    await session.execute(text("DELETE FROM memberships WHERE id = :id"), {"id": membership_id})


# ---------------------------------------------------------------- kiosk tokens


async def list_device_tokens(
    session: AsyncSession, machine_ids: Collection[int], area_ids: Collection[int]
) -> list[dict[str, Any]]:
    """Tokens bound to machines / areas inside the caller's scope. The token itself is never returned."""
    rows = await session.execute(
        text(
            "SELECT t.id, t.name, t.machine_id, m.name AS machine_name, t.area_id, a.name AS area_name, "
            "t.created_at, t.revoked_at FROM device_tokens t LEFT JOIN machines m ON m.id = t.machine_id "
            "LEFT JOIN areas a ON a.id = t.area_id "
            "WHERE t.machine_id = ANY(:machines) OR t.area_id = ANY(:areas) ORDER BY t.id"
        ),
        {"machines": list(machine_ids), "areas": list(area_ids)},
    )
    return [dict(r._mapping) for r in rows]


async def issue_device_token(
    session: AsyncSession,
    name: str,
    machine_id: int | None,
    area_id: int | None,
    created_by: int | None,
    token: str | None = None,
) -> tuple[int, str]:
    """Create a token for exactly one machine or one area. The plain value is shown once."""
    token = token or new_device_token()
    token_id = (
        await session.execute(
            text(
                "INSERT INTO device_tokens (token_hash, name, machine_id, area_id, created_by) "
                "VALUES (:hash, :name, :machine, :area, :user) ON CONFLICT (token_hash) DO NOTHING RETURNING id"
            ),
            {"hash": hash_token(token), "name": name, "machine": machine_id, "area": area_id, "user": created_by},
        )
    ).scalar_one_or_none()
    return int(token_id or 0), token


async def get_device_token(session: AsyncSession, token_id: int) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text("SELECT id, machine_id, area_id FROM device_tokens WHERE id = :id"), {"id": token_id}
        )
    ).first()
    return dict(row._mapping) if row else None


async def revoke_device_token(session: AsyncSession, token_id: int) -> None:
    await session.execute(
        text("UPDATE device_tokens SET revoked_at = COALESCE(revoked_at, :now) WHERE id = :id"),
        {"id": token_id, "now": datetime.now(UTC)},
    )


# ---------------------------------------------------------------- first start


async def bootstrap(session: AsyncSession, settings: Settings) -> None:
    """Create the superadmin from the environment; in development also demo users and kiosk tokens."""
    exists = await session.execute(text("SELECT 1 FROM users WHERE login = :l"), {"l": settings.admin_username})
    if exists.first() is None:
        await session.execute(
            text(
                "INSERT INTO users (login, password_hash, full_name, is_superadmin) "
                "VALUES (:login, :hash, 'Суперадминистратор', true)"
            ),
            {"login": settings.admin_username, "hash": hash_password(settings.admin_password)},
        )
    if not settings.demo_data:
        return
    org = (await session.execute(text("SELECT id FROM organizations WHERE code = 'demo'"))).scalar_one_or_none()
    if org is None:
        return
    rows = await session.execute(text("SELECT code, id FROM sites WHERE org_id = :o"), {"o": org})
    site: dict[str, int] = {r.code: r.id for r in rows}
    plant1 = site.get("plant1")
    area1 = (
        await session.execute(text("SELECT min(id) FROM areas WHERE site_id = :s"), {"s": plant1})
    ).scalar_one_or_none()
    demo_users = [
        ("demo-admin", "Администратор предприятия", "admin", org, None, None),
        ("demo-engineer", "Технолог площадки 1", "engineer", org, plant1, None),
        ("demo-master", "Мастер участка", "master", org, plant1, area1),
        ("demo-operator", "Оператор участка", "operator", org, plant1, area1),
        ("demo-service", "Сервисный инженер", "service", None, None, None),
    ]
    for login_name, full_name, role, org_id, site_id, area_id in demo_users:
        found = await session.execute(text("SELECT 1 FROM users WHERE login = :l"), {"l": login_name})
        if found.first() is None:
            user_id = await create_user(session, login_name, settings.demo_password, full_name)
            await add_membership(session, user_id, role, org_id, site_id, area_id)
    machine = (
        await session.execute(text("SELECT id FROM machines WHERE site_id = :s AND code = 'sim02'"), {"s": plant1})
    ).scalar_one_or_none()
    if machine is not None:
        await issue_device_token(session, "Демо: экран оператора sim02", machine, None, None, "dev_demo-kiosk-sim02")
    if area1 is not None:
        await issue_device_token(session, "Демо: ТВ участка 1", None, area1, None, "dev_demo-display-area1")
