"""QR passport API (ADR 0009): where a sticker leads, reissuing the code, PDF stickers.

The link on a sticker carries only the code of the machine. Opening it needs a signed-in user;
a wrong code and a machine out of the caller's reach give the same 404, so the answer does not
tell whether such a machine exists.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.admin import passport, stickers
from app.api.deps import FORBIDDEN, NOT_FOUND, CurrentScope, Session, SettingsDep
from app.core.access import (
    ANALYTICS_VIEW,
    GEOMETRY_VIEW,
    MACHINE_VIEW,
    OPERATOR_INPUT,
    REPAIRS_REQUEST,
    STRUCTURE_EDIT,
    Scope,
)
from app.core.config import Settings

router = APIRouter(prefix="/api/v1", tags=["passport"])


def base_url(request: Request, settings: Settings) -> str:
    """Where the web interface is reachable: MM_PUBLIC_URL, else the address the request came to."""
    if settings.public_url:
        return settings.public_url

    def first(header: str) -> str:  # a chain of proxies may list several values: the first is the client's
        return request.headers.get(header, "").split(",")[0].strip()

    proto = first("x-forwarded-proto") or request.url.scheme
    host = first("x-forwarded-host") or first("host") or request.url.netloc
    return f"{proto}://{host}"


def _administer_machine(scope: Scope, machine_id: int) -> None:
    """Stickers and codes are part of the structure: whoever edits the machines of its site (as in the admin UI)."""
    node = next((m for m in scope.machines if m.id == machine_id), None)
    if node is None or not scope.can_machine(MACHINE_VIEW, machine_id):
        raise NOT_FOUND
    if not scope.can_node(STRUCTURE_EDIT, node.org_id, node.site_id):
        raise FORBIDDEN


def _pdf(content: bytes, filename: str) -> Response:
    return Response(
        content, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@router.get("/passport/{code}")
async def open_passport(code: str, scope: CurrentScope, session: Session) -> dict[str, Any]:
    """Where the sticker leads the caller, by role.

    master, engineer, admin: the machine page (status, timeline, repair request); service: the
    machine card of the service; operator: the operator screen. Nothing else: 404.
    """
    machine_id = await passport.machine_by_code(session, code)
    if machine_id is None or not scope.can_machine(MACHINE_VIEW, machine_id):
        raise NOT_FOUND
    if scope.can_machine(GEOMETRY_VIEW, machine_id):
        view = "service"
    elif scope.can_machine(REPAIRS_REQUEST, machine_id) and scope.can_machine(ANALYTICS_VIEW, machine_id):
        view = "machine"
    elif scope.can_machine(OPERATOR_INPUT, machine_id):
        view = "operator"
    else:
        raise NOT_FOUND
    return {"machine_id": machine_id, "view": view}


@router.post("/admin/machines/{machine_id}/passport")
async def reissue_passport(machine_id: int, scope: CurrentScope, session: Session) -> dict[str, str]:
    """A new code for the machine: the old sticker stops working at once."""
    _administer_machine(scope, machine_id)
    return {"passport_code": await passport.reissue(session, machine_id)}


@router.get("/admin/machines/{machine_id}/passport.pdf")
async def machine_sticker(
    machine_id: int, request: Request, scope: CurrentScope, session: Session, settings: SettingsDep
) -> Response:
    """The sticker of one machine (90 x 50 mm): QR with the link, machine, site, code."""
    _administer_machine(scope, machine_id)
    (row,) = await passport.sticker_rows(session, [machine_id])
    return _pdf(stickers.single(row, base_url(request, settings)), f"passport-{row['code']}.pdf")


@router.get("/admin/areas/{area_id}/passports.pdf")
async def area_stickers(
    area_id: int, request: Request, scope: CurrentScope, session: Session, settings: SettingsDep
) -> Response:
    """Stickers of every active machine of an area, ten to an A4 page."""
    site_id = scope.areas.get(area_id)
    if site_id is None or area_id not in scope.visible_area_ids():
        raise NOT_FOUND
    if not scope.can_node(STRUCTURE_EDIT, scope.sites[site_id], site_id):
        raise FORBIDDEN
    machine_ids = [m.id for m in scope.machines if m.area_id == area_id]
    rows = await passport.sticker_rows(session, machine_ids, active_only=True)
    if not rows:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "the area has no active machines")
    title = f"Наклейки QR: {rows[0]['area_name']}"
    return _pdf(stickers.sheet(rows, base_url(request, settings), title), f"passports-area-{area_id}.pdf")
