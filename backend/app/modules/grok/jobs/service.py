import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import InvalidPayload, NotFound, PermissionDenied
from app.models import (
    Job, JobLog, GrokProject, Profile,
    ProjectDomainAssignment, ProjectToolInstallAssignment, ProjectUserAssignment,
    ToolInstall, User,
)
from app.modules.admin.audit import service as audit


async def _resolve_profile_for_job(
    db: AsyncSession,
    *,
    requested_id: uuid.UUID | None,
    user_id: uuid.UUID,
    provider: str,
    requester_domain_id: uuid.UUID | None = None,
    requester_tool_install_id: uuid.UUID | None = None,
    excluded_profile_ids: list[uuid.UUID] | list[str] | None = None,
    job_type: str = "image",
) -> Profile | None:
    """Customer cannot use their own profile — always pick from admin pool.

    If `requested_id` provided: validate it belongs to admin and is logged_in / running_job.
    Else: auto-pick the least-recently-used logged_in admin profile for this provider.
    """
    # "Admin pool" includes both per-domain `admin` and platform `super_admin`
    # — the role hierarchy treats super_admin as a superset of admin, so any
    # profile super_admin owns should also be available as a shared pool entry.
    # Without this, a super_admin doing the initial Auto-login (the most
    # common bootstrapping path) ends up with logged_in profiles that NOBODY
    # — not even themselves — can use as a job runner.
    ADMIN_ROLES = ("admin", "super_admin")

    # A profile is visible to a tenant in `requester_domain_id` if EITHER
    #   (a) the profile's owner is in that domain (legacy direct ownership), OR
    #   (b) the profile has at least one GrokProject assigned to that domain
    #       via project_domain_assignments.
    # When requester_domain_id is None (super_admin path) the visibility
    # filter is skipped entirely.
    assigned_to_domain = (
        select(GrokProject.profile_id)
        .join(ProjectDomainAssignment, ProjectDomainAssignment.project_id == GrokProject.id)
        .where(
            ProjectDomainAssignment.domain_id == requester_domain_id,
            ProjectDomainAssignment.enabled.is_(True),
        )
        .scalar_subquery()
        if requester_domain_id is not None
        else None
    )
    # Parallel for tool installs. When the request comes from a desktop
    # client (X-Tool-Install-Id header), the profile pool is restricted to
    # profiles whose GrokProjects are assigned to THIS install. Install
    # scope is STRICTER than domain scope — there's no owner-in-domain
    # fallback (installs don't own profiles), so a tool-scoped user with
    # zero assignments sees zero profiles. Admin must explicitly grant.
    assigned_to_install = (
        select(GrokProject.profile_id)
        .join(ProjectToolInstallAssignment, ProjectToolInstallAssignment.project_id == GrokProject.id)
        .where(
            ProjectToolInstallAssignment.tool_install_id == requester_tool_install_id,
            ProjectToolInstallAssignment.enabled.is_(True),
        )
        .scalar_subquery()
        if requester_tool_install_id is not None
        else None
    )

    if requested_id:
        profile = await db.get(Profile, requested_id)
        if not profile:
            raise NotFound("profile")
        owner = await db.get(User, profile.user_id)
        if not owner or owner.role not in ADMIN_ROLES:
            raise PermissionDenied("Profile is not in the admin pool")
        if profile.provider != provider:
            raise InvalidPayload(f"Profile provider mismatch: profile={profile.provider}, requested={provider}")
        if profile.status not in {"logged_in", "running_job"}:
            raise InvalidPayload(f"Profile not ready (status={profile.status}). Ask admin to refresh.")
        # Image-only guard for explicit picks: refuse video on a profile
        # that admin has flagged as image-only. We do this BEFORE the
        # tenant visibility check so the user gets a clear "video not
        # allowed" message instead of a generic permission error when
        # both fail simultaneously.
        if job_type == "video" and not profile.allows_video:
            raise InvalidPayload(
                "Profile này chỉ tạo ảnh (image-only) — chọn profile khác cho job video.",
            )
        # Tenant visibility check — same rule as auto-pick, via project assignments.
        if requester_domain_id is not None and owner.domain_id != requester_domain_id:
            visible = (
                await db.execute(
                    select(GrokProject.id)
                    .join(ProjectDomainAssignment, ProjectDomainAssignment.project_id == GrokProject.id)
                    .where(
                        GrokProject.profile_id == profile.id,
                        ProjectDomainAssignment.domain_id == requester_domain_id,
                        ProjectDomainAssignment.enabled.is_(True),
                    )
                    .limit(1)
                )
            ).first()
            if not visible:
                raise PermissionDenied("Profile not assigned to your domain")
        # Tool-install visibility check — for desktop clients, profile must
        # be reachable via a GrokProject assigned to THIS install.
        if requester_tool_install_id is not None:
            visible = (await db.execute(
                select(GrokProject.id)
                .join(ProjectToolInstallAssignment, ProjectToolInstallAssignment.project_id == GrokProject.id)
                .where(
                    GrokProject.profile_id == profile.id,
                    ProjectToolInstallAssignment.tool_install_id == requester_tool_install_id,
                    ProjectToolInstallAssignment.enabled.is_(True),
                )
                .limit(1)
            )).first()
            if not visible:
                raise PermissionDenied("Profile chưa được gán cho máy desktop này")
        return profile

    # Auto-pick: any logged_in / running_job admin-pool profile for this provider.
    # We deliberately do NOT filter by `active_jobs < max_concurrent_jobs` here
    # — that's a runtime-concurrency check, not a queue-admission gate. Jobs
    # for a fully-loaded profile should QUEUE behind in-flight ones, not be
    # rejected. The worker's _try_acquire_slot enforces concurrency at run
    # time. We just pick the least-loaded profile to spread the queue.
    where_clauses = [
        User.role.in_(ADMIN_ROLES),
        Profile.provider == provider,
        Profile.status.in_(["logged_in", "running_job"]),
    ]
    # Image-only profiles drop out of the pool for video jobs. Image jobs
    # still see them (that's their whole purpose).
    if job_type == "video":
        where_clauses.append(Profile.allows_video.is_(True))
    # Heavy-tier profiles are RESERVED for video, but only as a SOFT
    # preference (see the ORDER BY below), not a hard filter. Image jobs
    # de-prioritize heavy profiles so they stay free for the video queue,
    # yet a heavy profile is still usable as a FALLBACK when it is the only
    # logged-in profile left. Hard-excluding here used to make image
    # auto-pick fail with 422 whenever the pool was down to heavy-only.
    if assigned_to_install is not None:
        # Strictest scope: install. No owner fallback — admin must
        # explicitly assign a project to this install for it to show up.
        where_clauses.append(Profile.id.in_(assigned_to_install))
    elif assigned_to_domain is not None:
        where_clauses.append(
            (User.domain_id == requester_domain_id)
            | Profile.id.in_(assigned_to_domain)
        )
    # Honor profile rotation requests — when a worker retries a job whose
    # original profile hit a quota / rate-limit, it passes the failed
    # profile id(s) here so the resolver picks a sibling instead.
    if excluded_profile_ids:
        excluded_uuids = [
            uuid.UUID(str(p)) if not isinstance(p, uuid.UUID) else p
            for p in excluded_profile_ids
        ]
        where_clauses.append(Profile.id.not_in(excluded_uuids))
    # For video jobs, order by active_video_jobs ASC so the pool spreads
    # video load evenly across profiles (each video tab eats ~600MB-1.2GB
    # Chromium RAM, so getting one video on profile A and the next on
    # profile B is way better than stacking both on the same Chromium).
    # For image jobs, active_jobs (DOM-only count after the slot refactor)
    # still picks the freshest profile.
    load_column = (
        Profile.active_video_jobs if job_type == "video" else Profile.active_jobs
    )
    # Tier ordering (soft preference, applied to BOTH job types):
    #   - video jobs PREFER heavy (higher quota, more reliable) -> heavy first
    #   - image jobs AVOID heavy (keep it free for the video queue) -> heavy
    #     last, but still selected as a fallback when nothing else is up.
    from sqlalchemy import case as sa_case
    if job_type == "video":
        tier_priority = sa_case((Profile.tier == "heavy", 0), else_=1)
    else:
        tier_priority = sa_case((Profile.tier == "heavy", 1), else_=0)

    stmt = (
        select(Profile)
        .join(User, User.id == Profile.user_id)
        .where(*where_clauses)
        .order_by(
            tier_priority.asc(),
            load_column.asc(),
            func.coalesce(Profile.last_used_at, Profile.created_at).asc(),
        )
        .limit(1)
    )
    profile = (await db.execute(stmt)).scalar_one_or_none()
    if profile:
        # Bump last_used_at NOW so back-to-back create_job calls don't all
        # land on the same profile while waiting for the worker to pick up
        # the first one. Without this, 10 jobs queued in a burst all see
        # active_jobs=0 across the pool and the ORDER BY tie-breaker keeps
        # picking the first profile by id → starving the others. The worker
        # also updates last_used_at when it acquires a slot, which is fine —
        # both writes are monotonic so they don't fight.
        profile.last_used_at = datetime.now(timezone.utc)
        await db.flush()
        return profile
    raise InvalidPayload(
        f"Không có profile {provider} nào logged_in. Admin cần Auto-login profile trước."
    )


async def pick_alternate_profile(
    db: AsyncSession,
    job: Job,
) -> Profile | None:
    """Find a fresh profile for a job whose previous one hit rate-limit.

    Reads `_banned_profiles` from the job's input_payload (worker writes
    failed profile ids there before clearing job.profile_id). Returns
    None if the pool is exhausted — caller should mark the job failed.
    """
    payload = job.input_payload or {}
    banned = payload.get("_banned_profiles") or []
    owner = await db.get(User, job.user_id)
    requester_domain_id = (
        owner.domain_id if owner and owner.role != "super_admin" else None
    )
    try:
        return await _resolve_profile_for_job(
            db,
            requested_id=None,
            user_id=job.user_id,
            provider=job.provider,
            requester_domain_id=requester_domain_id,
            excluded_profile_ids=banned,
            # Critical: forward job_type so video-job rotation skips
            # image-only profiles + uses the video load-balancing column.
            # Without this, a video job rotated mid-flight could land on
            # an image-only profile and fail again immediately.
            job_type=job.job_type,
        )
    except InvalidPayload:
        # Pool exhausted (all profiles either down or already banned for
        # this job). Caller decides what to do — usually mark failed.
        return None


async def create_job(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    provider: str,
    job_type: str,
    prompt: str,
    profile_id: uuid.UUID | None,
    options: dict[str, Any] | None,
    project_id: uuid.UUID | None = None,
    api_key_id: uuid.UUID | None = None,
    tool_install_id_str: str | None = None,
) -> Job:
    # Look up requester's domain — used to filter the pool down to profiles
    # super_admin has loaned to this tenant + legacy same-domain profiles.
    # super_admin themselves are unscoped (pass None) so they can use any
    # profile in their own bootstrap workflow.
    requester = await db.get(User, user_id)
    requester_domain_id = (
        requester.domain_id
        if requester and requester.role != "super_admin"
        else None
    )
    # Resolve the desktop install (if the request came in via X-Tool-Install-Id
    # header on the HTTP handler). super_admin still bypasses install scoping
    # so ops can run jobs from the kiosk during debugging.
    requester_tool_install_id: uuid.UUID | None = None
    if tool_install_id_str and requester and requester.role != "super_admin":
        install = (await db.execute(
            select(ToolInstall).where(ToolInstall.tool_id == tool_install_id_str)
        )).scalar_one_or_none()
        if install and install.status == "active":
            requester_tool_install_id = install.id

    profile = await _resolve_profile_for_job(
        db, requested_id=profile_id, user_id=user_id, provider=provider,
        requester_domain_id=requester_domain_id,
        requester_tool_install_id=requester_tool_install_id,
        job_type=job_type,
    )

    # Pick the specific GrokProject the worker should use. Priority:
    #   0) explicit `project_id` from the request — admin / Playground form
    #      picked a project manually; honor it verbatim (must belong to the
    #      resolved profile, otherwise 400)
    #   1) per-user pin (project_user_assignments) — tenant user gets
    #      THEIR project even when sharing the profile with others
    #   2) domain-wide assignment — every user in this tenant uses it
    #   3) super_admin bootstrap: first project on the profile
    # Profile has no projects yet → leave NULL → worker falls back to
    # grok.com/imagine root URL (legacy behaviour).
    picked_project: GrokProject | None = None
    # 0) explicit override
    if project_id is not None:
        explicit = await db.get(GrokProject, project_id)
        if explicit is None:
            raise InvalidPayload(f"GrokProject {project_id} không tồn tại")
        if explicit.profile_id != profile.id:
            raise InvalidPayload(
                "project_id không thuộc profile được chọn — pick lại project hoặc bỏ profile_id để auto-resolve.",
            )
        picked_project = explicit
    # 1) per-user pin
    user_pin_q = (
        select(GrokProject)
        .join(ProjectUserAssignment, ProjectUserAssignment.project_id == GrokProject.id)
        .where(
            GrokProject.profile_id == profile.id,
            ProjectUserAssignment.user_id == user_id,
            ProjectUserAssignment.enabled.is_(True),
        )
        .order_by(GrokProject.created_at.asc())
        .limit(1)
    )
    if picked_project is None:
        picked_project = (await db.execute(user_pin_q)).scalar_one_or_none()

    # 2a) tool-install-wide fallback (desktop kiosk path). When a request
    # comes from a registered desktop install we prefer the install's
    # assignments over the domain's, since installs are the more specific
    # scope. Falls through to (2b) domain if no install assignment exists.
    if picked_project is None and requester_tool_install_id is not None:
        install_q = (
            select(GrokProject)
            .join(ProjectToolInstallAssignment, ProjectToolInstallAssignment.project_id == GrokProject.id)
            .where(
                GrokProject.profile_id == profile.id,
                ProjectToolInstallAssignment.tool_install_id == requester_tool_install_id,
                ProjectToolInstallAssignment.enabled.is_(True),
            )
            .order_by(GrokProject.created_at.asc())
            .limit(1)
        )
        picked_project = (await db.execute(install_q)).scalar_one_or_none()

    # 2b) domain-wide fallback (tenant web path)
    if picked_project is None and requester_domain_id is not None:
        domain_q = (
            select(GrokProject)
            .join(ProjectDomainAssignment, ProjectDomainAssignment.project_id == GrokProject.id)
            .where(
                GrokProject.profile_id == profile.id,
                ProjectDomainAssignment.domain_id == requester_domain_id,
                ProjectDomainAssignment.enabled.is_(True),
            )
            .order_by(GrokProject.created_at.asc())
            .limit(1)
        )
        picked_project = (await db.execute(domain_q)).scalar_one_or_none()

    # 3) super_admin bootstrap: any project on this profile
    if picked_project is None and requester_domain_id is None:
        any_q = (
            select(GrokProject)
            .where(GrokProject.profile_id == profile.id)
            .order_by(GrokProject.created_at.asc())
            .limit(1)
        )
        picked_project = (await db.execute(any_q)).scalar_one_or_none()

    job = Job(
        user_id=user_id,
        api_key_id=api_key_id,
        profile_id=profile.id,
        project_id=picked_project.id if picked_project else None,
        provider=provider,
        job_type=job_type,
        prompt=prompt,
        input_payload=options or {},
        status="queued",
    )
    db.add(job)
    await db.flush()
    db.add(JobLog(job_id=job.id, level="info",
                  message=f"Job queued (profile={profile.name})"))
    # Audit so the /audit-logs page per-domain tab surfaces Grok activity.
    # Truncate the prompt to keep secrets / huge inputs out of audit_metadata.
    await audit.log_action(
        db, user_id=user_id, action="grok_job_created",
        target_type="job", target_id=job.id,
        metadata={
            "provider": provider,
            "job_type": job_type,
            "profile": profile.name,
            "profile_id": str(profile.id),
            "prompt_preview": (prompt or "")[:120],
        },
    )
    await db.commit()
    await db.refresh(job)
    return job


async def assert_job_owner(db: AsyncSession, job_id: uuid.UUID, user_id: uuid.UUID, is_admin: bool) -> Job:
    job = await db.get(Job, job_id)
    if not job:
        raise NotFound("job")
    if job.user_id != user_id and not is_admin:
        raise PermissionDenied()
    return job
