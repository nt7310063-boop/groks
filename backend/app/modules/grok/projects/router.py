"""GrokProject CRUD + per-project domain assignment.

A `Profile` corresponds to one Grok account; a `GrokProject` is a
workspace inside that account. Mapping each tenant domain to its own
project keeps chat history, presets, and brand voice separated.

Endpoints:
  GET    /api/grok-projects?profile_id=<uuid>     list (admin scope)
  POST   /api/grok-projects                       create
  PATCH  /api/grok-projects/{id}                  rename / re-id
  DELETE /api/grok-projects/{id}                  remove (cascades assignments)
  GET    /api/grok-projects/{id}/domains          list assigned domains
  PUT    /api/grok-projects/{id}/domains          replace assignment set

Scope:
  - super_admin: full CRUD on any project.
  - admin tenant: list/read projects but cannot mutate (assignments are
    a super_admin-only surface — same rule as the legacy /profiles/
    domains endpoint).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.deps import CurrentUser, DbSession, SuperAdminUser
from app.core.exceptions import InvalidPayload, NotFound, PermissionDenied
from app.models import (
    Domain, GrokProject, Profile,
    ProjectDomainAssignment, ProjectToolInstallAssignment, ProjectUserAssignment,
    ToolInstall, User,
)
from app.modules.admin.audit import service as audit

router = APIRouter(prefix="/api/grok-projects", tags=["grok-projects"])


# ─── Schemas ───────────────────────────────────────────────────────────────


def _sanitize_slug(raw: str) -> str:
    """Strip anything after the slug — `?tab=...`, `#anchor`, trailing `/`.

    Users tend to paste the FULL URL after `/project/`, which leaves a
    query string attached. That breaks navigation later (`grok.com/project/<id>?tab=...`
    becomes `grok.com/project/<id>%3Ftab%3D...` after URL-encoding).
    """
    s = (raw or "").strip()
    # Remove protocol/host if pasted full URL
    if "/project/" in s:
        s = s.split("/project/", 1)[1]
    # Strip everything after the slug
    for sep in ("?", "#", "/"):
        if sep in s:
            s = s.split(sep, 1)[0]
    return s.strip()


class ProjectCreate(BaseModel):
    profile_id: uuid.UUID
    grok_project_id: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None


class ProjectUpdate(BaseModel):
    grok_project_id: str | None = Field(default=None, max_length=255)
    name: str | None = Field(default=None, max_length=255)
    description: str | None = None


class ProjectOut(BaseModel):
    id: uuid.UUID
    profile_id: uuid.UUID
    grok_project_id: str
    name: str
    description: str | None
    created_at: datetime
    # Quick summary so the UI doesn't have to fetch /domains separately
    # just to show "→ assigned to 2 tenants".
    domain_count: int = 0
    # Parallel counter for desktop installs assigned to this project.
    # Same purpose — list endpoint precomputes it so the row can render
    # the "X tool install(s) assigned" link without another fetch.
    tool_install_count: int = 0

    class Config:
        from_attributes = True


class ProjectDomainsOut(BaseModel):
    project_id: uuid.UUID
    # All currently-assigned domains (enabled + disabled together).
    # FE treats this as the "is assigned" checkbox state.
    domain_ids: list[uuid.UUID]
    # Subset of `domain_ids` whose `enabled` flag is FALSE. Resolver
    # ignores these as if they weren't assigned. FE shows them as a
    # disabled toggle on the same row.
    disabled_domain_ids: list[uuid.UUID] = []


class ProjectDomainsUpdate(BaseModel):
    domain_ids: list[uuid.UUID]
    # Optional subset that should be persisted with enabled=FALSE. Must
    # be a subset of domain_ids; entries outside `domain_ids` are
    # ignored. Missing → all assignments enabled (legacy behavior).
    disabled_domain_ids: list[uuid.UUID] = []


class ProjectToolInstallsOut(BaseModel):
    project_id: uuid.UUID
    tool_install_ids: list[uuid.UUID]
    disabled_tool_install_ids: list[uuid.UUID] = []


class ProjectToolInstallsUpdate(BaseModel):
    tool_install_ids: list[uuid.UUID]
    disabled_tool_install_ids: list[uuid.UUID] = []


class UserInDomainOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    status: str


class ProjectUsersOut(BaseModel):
    project_id: uuid.UUID
    user_ids: list[uuid.UUID]
    disabled_user_ids: list[uuid.UUID] = []


class ProjectUsersUpdate(BaseModel):
    user_ids: list[uuid.UUID]
    disabled_user_ids: list[uuid.UUID] = []


# ─── Helpers ───────────────────────────────────────────────────────────────


async def _domain_counts(db, project_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Bulk-count assignments so list endpoints stay O(1) round-trips."""
    if not project_ids:
        return {}
    from sqlalchemy import func
    rows = (
        await db.execute(
            select(
                ProjectDomainAssignment.project_id,
                func.count(ProjectDomainAssignment.domain_id),
            )
            .where(ProjectDomainAssignment.project_id.in_(project_ids))
            .group_by(ProjectDomainAssignment.project_id)
        )
    ).all()
    return {pid: cnt for pid, cnt in rows}


async def _tool_install_counts(db, project_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not project_ids:
        return {}
    from sqlalchemy import func
    rows = (await db.execute(
        select(
            ProjectToolInstallAssignment.project_id,
            func.count(ProjectToolInstallAssignment.tool_install_id),
        )
        .where(ProjectToolInstallAssignment.project_id.in_(project_ids))
        .group_by(ProjectToolInstallAssignment.project_id)
    )).all()
    return {pid: cnt for pid, cnt in rows}


def _serialize(p: GrokProject, count: int = 0, tool_count: int = 0) -> ProjectOut:
    return ProjectOut(
        id=p.id, profile_id=p.profile_id,
        grok_project_id=p.grok_project_id, name=p.name,
        description=p.description, created_at=p.created_at,
        domain_count=count,
        tool_install_count=tool_count,
    )


# ─── CRUD ──────────────────────────────────────────────────────────────────


@router.get("", response_model=list[ProjectOut])
async def list_projects(
    user: CurrentUser, db: DbSession,
    profile_id: uuid.UUID | None = Query(default=None),
) -> list[ProjectOut]:
    """List projects.
      - super_admin: every project (filterable by profile_id)
      - admin tenant: projects whose at least one assignment matches their
        domain. They use this to see what they're using.
    """
    q = select(GrokProject)
    if profile_id is not None:
        q = q.where(GrokProject.profile_id == profile_id)

    if user.role != "super_admin":
        # Restrict to projects assigned to admin's domain.
        q = q.join(
            ProjectDomainAssignment,
            ProjectDomainAssignment.project_id == GrokProject.id,
        ).where(ProjectDomainAssignment.domain_id == user.domain_id).distinct()

    q = q.order_by(GrokProject.created_at.asc())
    rows = list((await db.execute(q)).scalars().all())
    project_ids = [r.id for r in rows]
    counts = await _domain_counts(db, project_ids)
    tool_counts = await _tool_install_counts(db, project_ids)
    return [_serialize(r, counts.get(r.id, 0), tool_counts.get(r.id, 0)) for r in rows]


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreate, _super: SuperAdminUser, db: DbSession,
) -> ProjectOut:
    profile = await db.get(Profile, payload.profile_id)
    if not profile:
        raise NotFound("profile")
    # Uniqueness — same Grok project slug shouldn't be registered twice on the
    # same profile (would race the worker into the same URL anyway).
    dup = (
        await db.execute(
            select(GrokProject).where(
                GrokProject.profile_id == payload.profile_id,
                GrokProject.grok_project_id == payload.grok_project_id,
            )
        )
    ).scalar_one_or_none()
    if dup:
        raise InvalidPayload(
            f"Project '{payload.grok_project_id}' đã đăng ký cho profile này"
        )
    p = GrokProject(
        profile_id=payload.profile_id,
        grok_project_id=_sanitize_slug(payload.grok_project_id),
        name=payload.name.strip(),
        description=payload.description,
    )
    db.add(p)
    await db.flush()
    await audit.log_action(
        db, user_id=_super.id, action="grok_project_created",
        target_type="grok_project", target_id=p.id,
        metadata={
            "profile_id": str(payload.profile_id),
            "grok_project_id": p.grok_project_id,
            "name": p.name,
        },
    )
    await db.commit()
    await db.refresh(p)
    return _serialize(p, 0)


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: uuid.UUID, payload: ProjectUpdate,
    _super: SuperAdminUser, db: DbSession,
) -> ProjectOut:
    p = await db.get(GrokProject, project_id)
    if not p:
        raise NotFound("grok_project")
    changes: dict = {}
    if payload.grok_project_id is not None:
        p.grok_project_id = _sanitize_slug(payload.grok_project_id)
        changes["grok_project_id"] = p.grok_project_id
    if payload.name is not None:
        p.name = payload.name.strip()
        changes["name"] = p.name
    if payload.description is not None:
        p.description = payload.description
        changes["description"] = "updated"
    await audit.log_action(
        db, user_id=_super.id, action="grok_project_updated",
        target_type="grok_project", target_id=p.id, metadata=changes,
    )
    await db.commit()
    await db.refresh(p)
    counts = await _domain_counts(db, [p.id])
    return _serialize(p, counts.get(p.id, 0))


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_project(
    project_id: uuid.UUID, _super: SuperAdminUser, db: DbSession,
) -> None:
    p = await db.get(GrokProject, project_id)
    if not p:
        raise NotFound("grok_project")
    await audit.log_action(
        db, user_id=_super.id, action="grok_project_deleted",
        target_type="grok_project", target_id=p.id,
        metadata={"name": p.name, "grok_project_id": p.grok_project_id},
    )
    await db.delete(p)
    await db.commit()


# ─── Per-project domain assignment ────────────────────────────────────────


@router.get("/{project_id}/domains", response_model=ProjectDomainsOut)
async def get_project_domains(
    project_id: uuid.UUID, user: CurrentUser, db: DbSession,
) -> ProjectDomainsOut:
    p = await db.get(GrokProject, project_id)
    if not p:
        raise NotFound("grok_project")
    if user.role not in ("super_admin", "admin"):
        raise PermissionDenied()
    rows = (
        await db.execute(
            select(
                ProjectDomainAssignment.domain_id,
                ProjectDomainAssignment.enabled,
            )
            .where(ProjectDomainAssignment.project_id == project_id)
        )
    ).all()
    all_ids = [did for did, _ in rows]
    disabled_ids = [did for did, ena in rows if not ena]
    return ProjectDomainsOut(
        project_id=project_id,
        domain_ids=all_ids,
        disabled_domain_ids=disabled_ids,
    )


@router.put("/{project_id}/domains", response_model=ProjectDomainsOut)
async def set_project_domains(
    project_id: uuid.UUID, payload: ProjectDomainsUpdate,
    _super: SuperAdminUser, db: DbSession,
) -> ProjectDomainsOut:
    """Replace the set of tenant domains that can pull from this project.

    Pass an empty list to revoke all assignments — project becomes
    super_admin-only (still usable by the owner directly).
    """
    p = await db.get(GrokProject, project_id)
    if not p:
        raise NotFound("grok_project")

    if payload.domain_ids:
        found = (
            await db.execute(
                select(Domain.id).where(Domain.id.in_(payload.domain_ids))
            )
        ).scalars().all()
        missing = set(payload.domain_ids) - set(found)
        if missing:
            raise InvalidPayload(
                f"Unknown domain_ids: {sorted(str(m) for m in missing)}"
            )

    # Delete-then-insert. Set is small (handful per project) so the simple
    # approach beats a diff-based upsert in readability.
    await db.execute(
        ProjectDomainAssignment.__table__.delete()
        .where(ProjectDomainAssignment.project_id == project_id)
    )
    disabled_set = {d for d in payload.disabled_domain_ids if d in set(payload.domain_ids)}
    for did in payload.domain_ids:
        db.add(ProjectDomainAssignment(
            project_id=project_id,
            domain_id=did,
            enabled=did not in disabled_set,
        ))

    await audit.log_action(
        db, user_id=_super.id, action="grok_project_domains_set",
        target_type="grok_project", target_id=project_id,
        metadata={
            "name": p.name,
            "domain_ids": [str(d) for d in payload.domain_ids],
            "disabled_domain_ids": [str(d) for d in disabled_set],
        },
    )
    await db.commit()
    return ProjectDomainsOut(
        project_id=project_id,
        domain_ids=list(payload.domain_ids),
        disabled_domain_ids=list(disabled_set),
    )


# ─── Per-project tool-install assignment ──────────────────────────────────
# Sibling of the domain endpoints above. Used to scope projects to
# specific desktop installs (kiosks). Logic mirrors the domain flow:
# delete-then-insert, soft-disable via `enabled=false`.


@router.get("/{project_id}/tool-installs", response_model=ProjectToolInstallsOut)
async def get_project_tool_installs(
    project_id: uuid.UUID, user: CurrentUser, db: DbSession,
) -> ProjectToolInstallsOut:
    p = await db.get(GrokProject, project_id)
    if not p:
        raise NotFound("grok_project")
    if user.role not in ("super_admin", "admin"):
        raise PermissionDenied()
    rows = (await db.execute(
        select(
            ProjectToolInstallAssignment.tool_install_id,
            ProjectToolInstallAssignment.enabled,
        ).where(ProjectToolInstallAssignment.project_id == project_id)
    )).all()
    return ProjectToolInstallsOut(
        project_id=project_id,
        tool_install_ids=[tid for tid, _ in rows],
        disabled_tool_install_ids=[tid for tid, ena in rows if not ena],
    )


@router.put("/{project_id}/tool-installs", response_model=ProjectToolInstallsOut)
async def set_project_tool_installs(
    project_id: uuid.UUID, payload: ProjectToolInstallsUpdate,
    _super: SuperAdminUser, db: DbSession,
) -> ProjectToolInstallsOut:
    p = await db.get(GrokProject, project_id)
    if not p:
        raise NotFound("grok_project")
    if payload.tool_install_ids:
        found = (await db.execute(
            select(ToolInstall.id).where(ToolInstall.id.in_(payload.tool_install_ids))
        )).scalars().all()
        missing = set(payload.tool_install_ids) - set(found)
        if missing:
            raise InvalidPayload(
                f"Unknown tool_install_ids: {sorted(str(m) for m in missing)}"
            )

    await db.execute(
        ProjectToolInstallAssignment.__table__.delete()
        .where(ProjectToolInstallAssignment.project_id == project_id)
    )
    disabled_set = {
        d for d in payload.disabled_tool_install_ids if d in set(payload.tool_install_ids)
    }
    for tid in payload.tool_install_ids:
        db.add(ProjectToolInstallAssignment(
            project_id=project_id,
            tool_install_id=tid,
            enabled=tid not in disabled_set,
        ))

    await audit.log_action(
        db, user_id=_super.id, action="grok_project_tool_installs_set",
        target_type="grok_project", target_id=project_id,
        metadata={
            "name": p.name,
            "tool_install_ids": [str(t) for t in payload.tool_install_ids],
            "disabled_tool_install_ids": [str(t) for t in disabled_set],
        },
    )
    await db.commit()
    return ProjectToolInstallsOut(
        project_id=project_id,
        tool_install_ids=list(payload.tool_install_ids),
        disabled_tool_install_ids=list(disabled_set),
    )


# ─── Users-in-domain helper (for the project editor UI) ───────────────────


@router.get("/_users-by-domain/{domain_id}", response_model=list[UserInDomainOut])
async def list_users_in_domain(
    domain_id: uuid.UUID, _super: SuperAdminUser, db: DbSession,
) -> list[UserInDomainOut]:
    """Lookup users in a specific domain so the project editor can show a
    per-user assignment picker after the domain is selected."""
    rows = (
        await db.execute(
            select(User)
            .where(User.domain_id == domain_id, User.status == "active")
            .order_by(User.role.desc(), User.email.asc())
        )
    ).scalars().all()
    return [
        UserInDomainOut(id=u.id, email=u.email, role=u.role, status=u.status)
        for u in rows
    ]


# ─── Per-user pinning ─────────────────────────────────────────────────────


@router.get("/{project_id}/users", response_model=ProjectUsersOut)
async def get_project_users(
    project_id: uuid.UUID, user: CurrentUser, db: DbSession,
) -> ProjectUsersOut:
    p = await db.get(GrokProject, project_id)
    if not p:
        raise NotFound("grok_project")
    if user.role not in ("super_admin", "admin"):
        raise PermissionDenied()
    rows = (
        await db.execute(
            select(
                ProjectUserAssignment.user_id,
                ProjectUserAssignment.enabled,
            )
            .where(ProjectUserAssignment.project_id == project_id)
        )
    ).all()
    all_ids = [uid for uid, _ in rows]
    disabled_ids = [uid for uid, ena in rows if not ena]
    return ProjectUsersOut(
        project_id=project_id,
        user_ids=all_ids,
        disabled_user_ids=disabled_ids,
    )


class ProjectAutoProvisionIn(BaseModel):
    profile_id: uuid.UUID
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    domain_ids: list[uuid.UUID] = Field(default_factory=list)
    user_ids: list[uuid.UUID] = Field(default_factory=list)


class DiscoveredProject(BaseModel):
    """One project row as Grok itself reports it.

    `imported` flags whether this slug is already in our `grok_projects`
    table for the same profile — the FE uses it to disable the
    "Import" button so admin doesn't create duplicate rows.
    """
    grok_project_id: str
    name: str
    description: str | None = None
    imported: bool = False


@router.get("/discover", response_model=list[DiscoveredProject])
async def discover_projects(
    profile_id: uuid.UUID,
    _super: SuperAdminUser,
    db: DbSession,
) -> list[DiscoveredProject]:
    """List every Grok workspace/project this profile currently owns.

    Implementation: connect to the profile's VNC Chromium via CDP, open
    a page, run `fetch('/rest/workspaces?…')` inside the page context so
    the request carries the user's cookies + Chrome's TLS fingerprint
    (which is what beats Cloudflare). Return the parsed list, marking
    each row as already-imported if it matches an existing
    GrokProject(profile_id, grok_project_id).

    Admin clicks one of these in the UI → POST /api/grok-projects to
    persist a row, no manual slug copy.
    """
    profile = await db.get(Profile, profile_id)
    if not profile:
        raise NotFound("profile")
    if profile.status not in ("logged_in", "running_job"):
        raise InvalidPayload(
            f"Profile chưa logged_in (status={profile.status}). "
            "Auto-login trước khi discover."
        )

    import re
    import httpx
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout

    cdp_endpoint = f"http://grokflow-vnc-{str(profile.id).replace('-','')[:12]}:9223"
    try:
        async with httpx.AsyncClient(timeout=10) as cli:
            resp = await cli.get(f"{cdp_endpoint}/json/version")
            ws_url = resp.json().get("webSocketDebuggerUrl", "")
    except Exception as exc:  # noqa: BLE001
        raise InvalidPayload(
            f"Không kết nối được CDP ({type(exc).__name__}): {exc}",
        )
    if not ws_url:
        raise InvalidPayload("Chromium chưa trả wsEndpoint — đợi vài giây.")
    host = cdp_endpoint.replace("http://", "").rstrip("/")
    ws_url = re.sub(r"ws://[^/]+", f"ws://{host}", ws_url)

    raw_payload: dict | list | None = None
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(ws_url, timeout=12_000)
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await ctx.new_page()
            try:
                # We just need any same-origin page so fetch() lands as
                # `https://grok.com/rest/...`. Root is fastest to load.
                await page.goto(
                    "https://grok.com/", wait_until="domcontentloaded", timeout=25_000,
                )
                try:
                    raw_payload = await page.evaluate(
                        """async () => {
                            const r = await fetch(
                                '/rest/workspaces?pageSize=200&orderBy=ORDER_BY_LAST_USE_TIME',
                                { credentials: 'include' },
                            );
                            return { status: r.status, body: await r.text() };
                        }"""
                    )
                except PWTimeout as exc:
                    raise InvalidPayload(f"Fetch /rest/workspaces timeout: {exc}")
            finally:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
    except InvalidPayload:
        raise
    except Exception as exc:  # noqa: BLE001
        raise InvalidPayload(
            f"CDP/Playwright failure ({type(exc).__name__}): {exc}",
        )

    if not isinstance(raw_payload, dict) or raw_payload.get("status") != 200:
        status_code = (raw_payload or {}).get("status") if isinstance(raw_payload, dict) else None
        snippet = (raw_payload or {}).get("body", "")[:200] if isinstance(raw_payload, dict) else ""
        raise InvalidPayload(
            f"Grok trả {status_code} cho /rest/workspaces: {snippet!r}",
        )

    import json as _json
    try:
        data = _json.loads(raw_payload["body"])
    except _json.JSONDecodeError as exc:
        raise InvalidPayload(f"/rest/workspaces không phải JSON: {exc}")

    # Response shape (captured): {"workspaces": [...], "pageToken": ...}
    rows = data.get("workspaces") or data.get("items") or data.get("results") or []
    if not isinstance(rows, list):
        raise InvalidPayload(f"Shape lạ — không tìm thấy list workspaces: keys={list(data.keys())}")

    # Existing imported slugs so we can disable repeat-import in the UI.
    already = set(
        (await db.execute(
            select(GrokProject.grok_project_id).where(GrokProject.profile_id == profile_id)
        )).scalars().all()
    )

    out: list[DiscoveredProject] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        slug = (
            row.get("workspaceId") or row.get("id") or row.get("workspace_id")
            or row.get("projectId") or row.get("project_id")
        )
        name = row.get("title") or row.get("name") or row.get("displayName")
        if not slug or not name:
            continue
        out.append(DiscoveredProject(
            grok_project_id=str(slug),
            name=str(name),
            description=row.get("description"),
            imported=str(slug) in already,
        ))
    return out


@router.post("/auto-provision", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def auto_provision_project(
    payload: ProjectAutoProvisionIn,
    _super: SuperAdminUser,
    db: DbSession,
) -> ProjectOut:
    """Drive the profile's Chromium (via CDP) to create a Grok project,
    capture the resulting URL slug, and persist GrokProject + assignments
    in one shot. The profile MUST have a running VNC container (status
    logged_in or running_job) — otherwise we have no CDP endpoint to
    connect to.

    Selector strategy (best-effort against grok.com as of late 2025):
      1. Goto https://grok.com
      2. Click "+ New Project" in the left sidebar
      3. Type the name into the create-project modal input
      4. Press Enter / click "Create"
      5. Wait for URL to change to /project/<slug>
      6. Extract <slug> from the URL

    If any step times out we raise a clear 502 so the FE can fall back
    to manual entry.
    """
    profile = await db.get(Profile, payload.profile_id)
    if not profile:
        raise NotFound("profile")
    if profile.status not in ("logged_in", "running_job"):
        raise InvalidPayload(
            f"Profile chưa logged_in (status={profile.status}). "
            "Auto-login trước khi auto-provision."
        )

    # Late imports — playwright + httpx are heavier than the rest of this
    # router file's deps, only used here.
    import re
    import httpx
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout

    cdp_endpoint = f"http://grokflow-vnc-{str(profile.id).replace('-','')[:12]}:9223"

    # Chromium reports its WS endpoint with `localhost:9222` regardless of
    # the host:port we hit. Fetch + rewrite the ws URL to the container's
    # actual address so Playwright connects to the right Chromium. Same
    # trick the worker uses in grok_provider.py.
    try:
        async with httpx.AsyncClient(timeout=10) as cli:
            resp = await cli.get(f"{cdp_endpoint}/json/version")
            ws_url = resp.json().get("webSocketDebuggerUrl", "")
    except Exception as exc:  # noqa: BLE001
        raise InvalidPayload(
            f"Không thể kết nối CDP của profile ({type(exc).__name__}): {exc}. "
            "Kiểm tra VNC container đang chạy chưa."
        )
    if not ws_url:
        raise InvalidPayload(
            "Chromium không trả về wsEndpoint. Profile có thể đang khởi động — đợi vài giây rồi thử lại."
        )
    host = cdp_endpoint.replace("http://", "").rstrip("/")
    ws_url = re.sub(r"ws://[^/]+", f"ws://{host}", ws_url)

    slug: str | None = None
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(ws_url, timeout=12_000)
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await ctx.new_page()
            try:
                await page.goto("https://grok.com/", wait_until="domcontentloaded", timeout=25_000)
                # Wait for the React shell to mount. The sidebar nav links
                # are a reliable signal — if "Imagine" is visible, the
                # SPA is alive.
                try:
                    await page.locator("text=Imagine").first.wait_for(timeout=15_000)
                except PWTimeout:
                    # Maybe a sign-in wall — surface a clear error.
                    raise InvalidPayload(
                        "Grok sidebar không load được — kiểm tra profile đã login chưa."
                    )
                # Give the rest of the sidebar (Projects section, "+ New
                # Project" row) one more beat to hydrate.
                await page.wait_for_timeout(800)

                # Selectors for the "New Project" button. Grok lays it out
                # as a row with a "+" icon and text — try every shape we
                # might encounter, ordered cheap-first.
                # Find the CLOSEST clickable element wrapping the "New
                # Project" text. The text= matcher often hits a span/
                # label that's not itself clickable; walking up to the
                # nearest <a>/<button>/[role] is what actually fires the
                # SPA's route change.
                target_handle = await page.evaluate_handle(
                    """() => {
                        const lower = s => (s || '').trim().toLowerCase();
                        const all = Array.from(document.querySelectorAll('a, button, [role="button"], [role="link"]'));
                        // 1) Prefer ones whose own text is exactly New Project / + New Project
                        const exact = all.find(el => {
                            const t = lower(el.innerText);
                            return t === 'new project' || t === '+ new project' || t === 'new project +';
                        });
                        if (exact) return exact;
                        // 2) Any clickable whose text *contains* 'new project'
                        const sub = all.find(el => lower(el.innerText).includes('new project'));
                        if (sub) return sub;
                        // 3) An aria-label hint
                        const aria = document.querySelector('[aria-label*="new project" i], [aria-label*="create project" i]');
                        return aria || null;
                    }"""
                )
                element = target_handle.as_element() if target_handle else None
                if element is None:
                    sidebar_text = await page.evaluate(
                        "() => document.body.innerText.split('\\n').slice(0, 40).join(' | ')"
                    )
                    raise InvalidPayload(
                        f"Không tìm thấy clickable cho 'New Project'. UI có thể đã đổi. "
                        f"Sidebar text: {sidebar_text[:400]}"
                    )
                new_btn = element  # ElementHandle; click() works directly
                # Race two outcomes after the click:
                #   A) URL changes to /project/<slug>  → Grok creates the
                #      project immediately (no modal flow). Capture the slug.
                #   B) A naming modal/input appears   → fill name + submit
                #      and then wait for the URL change.
                # We can't predict which flow Grok will use (and it may
                # change). Try A first with a shorter wait, fall back to B.
                clicked_at_url = page.url
                # Try every click strategy we know — Grok uses React
                # synthetic events that sometimes ignore programmatic
                # clicks. mouse.click(x,y) on the bounding box center is
                # the closest thing to a real user click.
                try:
                    await new_btn.scroll_into_view_if_needed(timeout=3_000)
                except Exception:  # noqa: BLE001
                    pass

                clicked_ok = False
                box = None
                try:
                    box = await new_btn.bounding_box()
                except Exception:  # noqa: BLE001
                    pass

                # 1) Real mouse click via CDP at the element's center.
                if box and box.get("width") and box.get("height"):
                    try:
                        cx = box["x"] + box["width"] / 2
                        cy = box["y"] + box["height"] / 2
                        await page.mouse.move(cx, cy)
                        await page.mouse.click(cx, cy, delay=40)
                        clicked_ok = True
                    except Exception:  # noqa: BLE001
                        pass

                # 2) Native .click() with full Playwright actionability.
                if not clicked_ok:
                    try:
                        await new_btn.click(timeout=5_000)
                        clicked_ok = True
                    except Exception:  # noqa: BLE001
                        pass

                # 3) Force click (skips actionability).
                if not clicked_ok:
                    try:
                        await new_btn.click(force=True, timeout=3_000)
                        clicked_ok = True
                    except Exception:  # noqa: BLE001
                        pass

                # 4) dispatchEvent — last resort, fires React listeners.
                if not clicked_ok:
                    try:
                        await new_btn.evaluate(
                            "el => el.dispatchEvent(new MouseEvent('click', "
                            "{bubbles: true, cancelable: true, view: window}))"
                        )
                    except Exception:  # noqa: BLE001
                        await new_btn.evaluate("el => el.click()")

                slug_captured = False
                # ── Path A: wait for URL to switch within 8s ──
                try:
                    await page.wait_for_url("**/project/**", timeout=8_000)
                    final_url = page.url
                    m = re.search(r"/project/([^/?#]+)", final_url)
                    if m:
                        slug = m.group(1)
                        slug_captured = True
                except PWTimeout:
                    pass

                # ── Path B: modal-flow fallback ──
                if not slug_captured:
                    input_candidates = [
                        "[role='dialog'] input[type='text']:visible",
                        "[role='dialog'] input:not([type='hidden']):visible",
                        "[role='dialog'] textarea:visible",
                        "input[placeholder*='roject' i]:visible",
                        "input[placeholder*='name' i]:visible",
                    ]
                    name_input = None
                    for sel in input_candidates:
                        loc = page.locator(sel).first
                        try:
                            await loc.wait_for(timeout=2_000, state="visible")
                            name_input = loc
                            break
                        except PWTimeout:
                            continue
                    if name_input is not None:
                        await name_input.fill(payload.name)
                        create_btn = page.locator(
                            "[role='dialog'] button:has-text('Create'), "
                            "[role='dialog'] button:has-text('Tạo'), "
                            "[role='dialog'] button[type='submit']"
                        ).first
                        if await create_btn.count() > 0:
                            await create_btn.click()
                        else:
                            await name_input.press("Enter")
                        try:
                            await page.wait_for_url("**/project/**", timeout=15_000)
                            m = re.search(r"/project/([^/?#]+)", page.url)
                            if m:
                                slug = m.group(1)
                                slug_captured = True
                        except PWTimeout:
                            pass

                # ── Try to rename the project on Grok side so it's
                # findable in the sidebar (best-effort; failures don't
                # abort the whole flow since we still have the slug). ──
                if slug_captured:
                    try:
                        # Common title-edit patterns: contenteditable header,
                        # an input near the top of the project page, or a
                        # "Rename" menu item. Try fast + give up quickly.
                        title_loc = page.locator(
                            "[contenteditable='true']:visible, "
                            "h1[role='textbox']:visible, "
                            "input[aria-label*='title' i]:visible"
                        ).first
                        await title_loc.wait_for(timeout=3_000, state="visible")
                        await title_loc.click()
                        await page.keyboard.press("Control+A")
                        await page.keyboard.type(payload.name)
                        await page.keyboard.press("Enter")
                    except Exception:  # noqa: BLE001
                        # Naming Grok-side is optional — GrokFlow shows our
                        # own `name` in the UI anyway, slug is enough.
                        pass
            finally:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await browser.disconnect()
                except Exception:  # noqa: BLE001
                    pass
    except PWTimeout as exc:
        raise InvalidPayload(
            f"Auto-provision timeout — Grok UI may have changed. "
            f"Tạo thủ công đi: {exc}"
        )
    except Exception as exc:  # noqa: BLE001
        raise InvalidPayload(
            f"Auto-provision failed ({type(exc).__name__}): {exc}. "
            "Kiểm tra VNC profile rồi thử lại."
        )

    if not slug:
        # Capture rich diagnostic so the next iteration of selectors can
        # be informed. Snapshot the page URL + a chunk of innerText —
        # base64-tagged in the error so super_admin can paste back.
        diag = "(no diagnostic captured)"
        try:
            async with async_playwright() as pw:  # re-connect briefly
                browser = await pw.chromium.connect_over_cdp(ws_url, timeout=8_000)
                ctx = browser.contexts[0]
                # Find a page still on grok.com (we may have multiple).
                grok_page = None
                for p in ctx.pages:
                    if "grok.com" in p.url:
                        grok_page = p
                        break
                if grok_page:
                    url_now = grok_page.url
                    body = await grok_page.evaluate(
                        "() => document.body.innerText.split('\\n').filter(s => s.trim()).slice(0, 40).join(' | ')"
                    )
                    diag = f"current_url={url_now} | first_lines={body[:600]}"
                await browser.disconnect()
        except Exception as exc:  # noqa: BLE001
            diag = f"diag-capture-error: {exc}"
        raise InvalidPayload(
            f"Click button OK nhưng URL không chuyển sang /project/<slug>. "
            f"Có thể Grok hiện confirmation modal khác hoặc UI đã đổi. "
            f"DIAG: {diag}"
        )

    # Same uniqueness guard as manual create.
    dup = (
        await db.execute(
            select(GrokProject).where(
                GrokProject.profile_id == payload.profile_id,
                GrokProject.grok_project_id == slug,
            )
        )
    ).scalar_one_or_none()
    if dup:
        raise InvalidPayload(
            f"Slug '{slug}' đã tồn tại — Grok có thể đã có project trùng tên."
        )

    p = GrokProject(
        profile_id=payload.profile_id,
        grok_project_id=slug,
        name=payload.name.strip(),
        description=payload.description,
    )
    db.add(p)
    await db.flush()

    # Apply domain + user assignments in the same transaction so the
    # caller doesn't see a half-provisioned project.
    for did in payload.domain_ids:
        db.add(ProjectDomainAssignment(project_id=p.id, domain_id=did))
    for uid in payload.user_ids:
        db.add(ProjectUserAssignment(project_id=p.id, user_id=uid))

    await audit.log_action(
        db, user_id=_super.id, action="grok_project_auto_provisioned",
        target_type="grok_project", target_id=p.id,
        metadata={
            "profile_id": str(payload.profile_id),
            "slug": slug,
            "name": p.name,
            "domain_count": len(payload.domain_ids),
            "user_count": len(payload.user_ids),
        },
    )
    await db.commit()
    await db.refresh(p)
    return _serialize(p, len(payload.domain_ids))


@router.put("/{project_id}/users", response_model=ProjectUsersOut)
async def set_project_users(
    project_id: uuid.UUID, payload: ProjectUsersUpdate,
    _super: SuperAdminUser, db: DbSession,
) -> ProjectUsersOut:
    """Replace the per-user pin set for this project. Each user listed
    here will use this project regardless of any domain-wide assignment.

    Pass an empty list to revoke all per-user pins (domain-wide rule
    takes over).
    """
    p = await db.get(GrokProject, project_id)
    if not p:
        raise NotFound("grok_project")

    if payload.user_ids:
        found = (
            await db.execute(
                select(User.id).where(User.id.in_(payload.user_ids))
            )
        ).scalars().all()
        missing = set(payload.user_ids) - set(found)
        if missing:
            raise InvalidPayload(
                f"Unknown user_ids: {sorted(str(m) for m in missing)}"
            )

    await db.execute(
        ProjectUserAssignment.__table__.delete()
        .where(ProjectUserAssignment.project_id == project_id)
    )
    disabled_set = {u for u in payload.disabled_user_ids if u in set(payload.user_ids)}
    for uid in payload.user_ids:
        db.add(ProjectUserAssignment(
            project_id=project_id,
            user_id=uid,
            enabled=uid not in disabled_set,
        ))

    await audit.log_action(
        db, user_id=_super.id, action="grok_project_users_set",
        target_type="grok_project", target_id=project_id,
        metadata={
            "name": p.name,
            "user_ids": [str(u) for u in payload.user_ids],
            "disabled_user_ids": [str(u) for u in disabled_set],
        },
    )
    await db.commit()
    return ProjectUsersOut(
        project_id=project_id,
        user_ids=list(payload.user_ids),
        disabled_user_ids=list(disabled_set),
    )
