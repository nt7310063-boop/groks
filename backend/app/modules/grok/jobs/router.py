import uuid

from fastapi import APIRouter, File as FastapiFile, Header, Query, Response, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.core.deps import CurrentUser, DbSession
from app.core.exceptions import EntitlementBlocked, InvalidPayload, NotFound
from app.models import File, Job, JobLog
from app.modules.entitlements.service import (
    EntitlementDenied,
    assert_concurrent_jobs,
    assert_job_options,
    assert_quota,
    get_effective_entitlements,
)
from app.modules.grok.files import service as files_service
from app.services.domain_quota import check_and_reserve as reserve_domain_quota

from . import service
from .schemas import JobCreate, JobLogOut, JobOut, JobUpdate

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class JobInputUploadOut(BaseModel):
    file_id: uuid.UUID
    file_name: str
    mime_type: str
    file_size: int


@router.get("", response_model=list[JobOut])
async def list_jobs(
    user: CurrentUser,
    db: DbSession,
    response: Response,
    status_filter: str | None = Query(default=None, alias="status"),
    provider: str | None = Query(default=None),
    job_type: str | None = Query(default=None),
    q: str | None = Query(default=None, description="Search prompt substring"),
    limit: int = Query(default=20, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[Job]:
    """Paginated jobs list. Total count returned via X-Total-Count header so
    the frontend can render pagination controls without a 2nd request."""
    base = select(Job).where(Job.user_id == user.id)
    if status_filter:
        base = base.where(Job.status == status_filter)
    if provider:
        base = base.where(Job.provider == provider)
    if job_type:
        base = base.where(Job.job_type == job_type)
    if q:
        base = base.where(Job.prompt.ilike(f"%{q}%"))

    total = (await db.execute(
        select(func.count()).select_from(base.subquery())
    )).scalar_one()
    response.headers["X-Total-Count"] = str(total)
    response.headers["Access-Control-Expose-Headers"] = "X-Total-Count"

    stmt = base.order_by(Job.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post("", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def create_job(
    payload: JobCreate, user: CurrentUser, db: DbSession,
    x_tool_install_id: str | None = Header(default=None, alias="X-Tool-Install-Id"),
) -> Job:
    options = dict(payload.options or {})
    if payload.size: options["size"] = payload.size
    if payload.model: options["model"] = payload.model
    if payload.style: options["style"] = payload.style
    if payload.n != 1: options["n"] = payload.n
    if payload.seed is not None: options["seed"] = payload.seed
    if payload.input_image_file_id: options["input_image_file_id"] = str(payload.input_image_file_id)
    if payload.reference_images:
        options["reference_images"] = [str(r) for r in payload.reference_images]

    # If the caller references input file(s), validate each exists + belongs
    # to them BEFORE we burn a quota check and create the job row. Avoids
    # the orphan case where a job ends up pointing at a deleted/missing
    # file_id. Single-ref (input_image_file_id) and multi-ref
    # (reference_images) are both checked; the worker later merges them.
    from app.models import File as FileModel
    file_ids_to_check: list[uuid.UUID] = []
    if payload.input_image_file_id:
        file_ids_to_check.append(payload.input_image_file_id)
    if payload.reference_images:
        file_ids_to_check.extend(payload.reference_images)
    for fid in file_ids_to_check:
        f = await db.get(FileModel, fid)
        if not f or f.user_id != user.id:
            raise InvalidPayload(f"reference file {fid} không tồn tại hoặc không thuộc về bạn")

    eff = await get_effective_entitlements(db, user)
    try:
        assert_job_options(
            eff,
            job_type=payload.job_type,
            has_input_image=(
                payload.input_image_file_id is not None
                or bool(payload.reference_images)
            ),
            options=options,
        )
        await assert_concurrent_jobs(db, user, eff)
        await assert_quota(db, user, eff)
    except EntitlementDenied as e:
        raise EntitlementBlocked(e.code, e.message)

    # Daily-quota gate: tool install quota overrides domain quota when set,
    # otherwise falls back to domain. Plan-level entitlements above govern
    # WHAT the user can do; this gate caps THROUGHPUT (e.g. reseller bought
    # 500/day, sub-resold 100/day to kiosk Khách 1). No-op for super_admin
    # (domain_id+tool_install_id both NULL) or when no scope has a cap.
    # Check Duplicate Request (5s Debounce - Option A: return existing job)
    from datetime import datetime, timedelta
    recent_duplicate = (await db.execute(
        select(Job)
        .where(
            Job.user_id == user.id,
            Job.prompt == payload.prompt,
            Job.created_at >= datetime.utcnow() - timedelta(seconds=5)
        )
        .order_by(Job.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()

    if recent_duplicate is not None:
        print(f"[duplicate-gate] duplicate job detected, returning existing: {recent_duplicate.id}", flush=True)
        return recent_duplicate

    await reserve_domain_quota(db, user.domain_id, user.tool_install_id)

    return await service.create_job(
        db,
        user_id=user.id,
        provider=payload.provider,
        job_type=payload.job_type,
        prompt=payload.prompt,
        profile_id=payload.profile_id,
        project_id=payload.project_id,
        options=options or None,
        tool_install_id_str=x_tool_install_id,
    )


@router.post("/upload-input", response_model=JobInputUploadOut, status_code=status.HTTP_201_CREATED)
async def upload_input(
    user: CurrentUser,
    db: DbSession,
    file: UploadFile = FastapiFile(...),
) -> JobInputUploadOut:
    """Upload a reference image for image-to-image / video jobs.

    Pre-flight validation: probe Grok's `/rest/app-chat/upload-file` with the
    bytes before persisting locally so a moderated image (NSFW / minor /
    violence / etc.) returns 400 IMMEDIATELY instead of waiting for the
    user to submit a job + 180s retry timeout. If Grok rejects, we delete
    nothing because we haven't saved anything yet.

    Validation requires at least one `logged_in` Grok profile so we have
    cookies to authenticate the probe. If no profile is online, we skip
    validation and accept the upload — better to save than to block when
    the validation rail is unavailable.
    """
    if not file.content_type or not file.content_type.startswith("image/"):
        raise InvalidPayload("Only image/* uploads are accepted as input")
    raw = await file.read()
    if len(raw) > 20_000_000:
        raise InvalidPayload("Input image too large (>20MB)")

    # ── Pre-flight Grok moderation check ──
    # Borrow any active profile's cookies to probe the moderation endpoint.
    from app.models import Profile
    from app.providers.grok_provider import GrokProvider
    from app.providers.grok_api_client import GrokAPIError
    from app.providers.base import JobInput

    probe_profile = (await db.execute(
        select(Profile)
        .where(Profile.provider == "grok", Profile.status == "logged_in")
        .order_by(Profile.last_used_at.desc().nulls_last())
        .limit(1)
    )).scalar_one_or_none()

    print(f"[upload-probe] start user={user.id} file={file.filename} ({len(raw)}b)", flush=True)
    if probe_profile is not None:
        print(f"[upload-probe] using profile={probe_profile.name} ({probe_profile.id})", flush=True)
        provider = GrokProvider()
        try:
            session = await provider._build_api_session(JobInput(
                prompt="", job_type="image", options=None,
                profile_path=probe_profile.profile_path,
            ))
        except Exception as exc:  # noqa: BLE001 — never let probe crash the upload
            print(f"[upload-probe] _build_api_session raised: {type(exc).__name__}: {exc}", flush=True)
            session = None
        if session is None:
            print("[upload-probe] session=None → skipping moderation check, saving as-is", flush=True)
        else:
            client, _pid, _tag = session
            print(f"[upload-probe] session ready, calling Grok upload-file…", flush=True)
            # Hard timeout on the probe — large files + a busy probe
            # profile can stall this call for tens of seconds while the
            # user sits staring at a spinner; many users assume the page
            # is broken and reload (we see this as nginx 499 in the
            # access log). 15s is generous enough for a 20 MB upload
            # over the WARP socks proxy but short enough that the user
            # doesn't give up. On timeout we skip the moderation check
            # and save the file as-is — the actual job will still get
            # a moderation verdict from Grok at submit time.
            import asyncio as _asyncio
            try:
                meta = await _asyncio.wait_for(
                    client.upload_file(
                        content=raw,
                        filename=file.filename or "input.png",
                        mime=file.content_type,
                    ),
                    timeout=15.0,
                )
                print(f"[upload-probe] Grok accepted: fileMetadataId={meta.get('fileMetadataId', '?')[:12]}…", flush=True)
            except _asyncio.TimeoutError:
                print("[upload-probe] timeout (>15s) — skipping moderation check, saving as-is", flush=True)
            except GrokAPIError as exc:
                print(f"[upload-probe] Grok REJECTED: code={exc.code} msg={exc.message}", flush=True)
                if exc.code == "content_moderated":
                    raise InvalidPayload(
                        "Ảnh vi phạm chính sách Grok (content moderation). "
                        "Đổi ảnh khác — không lưu, không tạo job.",
                    )
                # Other API errors (network glitch, cookie_expired on probe
                # profile) shouldn't block the upload — let the actual job
                # retry on a fresh profile/session.
            except Exception as exc:  # noqa: BLE001
                print(f"[upload-probe] unexpected exception: {type(exc).__name__}: {exc}", flush=True)
    else:
        print("[upload-probe] no logged_in profile available — skipping check", flush=True)

    rec = await files_service.save_job_result(
        db,
        user_id=user.id,
        job_id=None,  # detached upload — gets associated when used by a job
        file_name=file.filename or "input.png",
        file_type="input",
        mime_type=file.content_type,
        data=raw,
    )
    await db.commit()
    return JobInputUploadOut(
        file_id=rec.id,
        file_name=rec.file_name,
        mime_type=rec.mime_type or file.content_type,
        file_size=rec.file_size or len(raw),
    )


@router.get("/{job_id}", response_model=JobOut)
async def get_job(job_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Job:
    return await service.assert_job_owner(db, job_id, user.id, user.role == "admin")


@router.post("/{job_id}/retry", response_model=JobOut)
async def retry_job(job_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Job:
    job = await service.assert_job_owner(db, job_id, user.id, user.role == "admin")
    if job.status not in {"failed", "cancelled", "expired"}:
        raise InvalidPayload(f"Cannot retry job in status {job.status}")
    job.status = "queued"
    job.retry_count += 1
    # Manual retry should always proceed — bump the cap so the worker's
    # `retry_count < max_retry` filter doesn't immediately reject this job.
    if job.max_retry < job.retry_count:
        job.max_retry = job.retry_count + 1
    job.error_message = None
    job.next_attempt_at = None  # manual retry → immediately eligible
    job.completed_at = None
    db.add(JobLog(job_id=job.id, level="info",
                  message=f"Job retry requested (now {job.retry_count}/{job.max_retry})"))
    await db.commit()
    await db.refresh(job)
    return job


class BulkCancelPayload(BaseModel):
    ids: list[uuid.UUID]


@router.post("/cancel-all")
async def cancel_all_jobs(user: CurrentUser, db: DbSession):
    from sqlalchemy import update
    from app.models import Profile
    
    query = select(Job).where(
        Job.status.in_(["pending", "queued", "running", "processing_provider", "uploading_result"])
    )
    if user.role != "admin":
        query = query.where(Job.user_id == user.id)
        
    jobs = (await db.execute(query)).scalars().all()
    
    cancelled_count = 0
    for job in jobs:
        job.status = "cancelled"
        db.add(JobLog(job_id=job.id, level="info", message="Job cancelled via Cancel All"))
        cancelled_count += 1
        
    if cancelled_count > 0:
        await db.commit()
        # Recalculate profile active jobs
        await db.execute(
            update(Profile).values(
                active_jobs=select(func.count(Job.id)).where(Job.profile_id == Profile.id, Job.status.in_(["running", "processing_provider", "uploading_result"])).scalar_subquery(),
                active_video_jobs=select(func.count(Job.id)).where(Job.profile_id == Profile.id, Job.status.in_(["running", "processing_provider", "uploading_result"]), Job.job_type == "video").scalar_subquery()
            )
        )
        await db.execute(
            update(Profile).values(status="logged_in").where(Profile.status == "running_job", Profile.active_jobs == 0)
        )
        await db.commit()
        
    return {"cancelled": cancelled_count}


@router.post("/bulk-cancel")
async def bulk_cancel_jobs(
    payload: BulkCancelPayload, user: CurrentUser, db: DbSession
):
    from sqlalchemy import update
    from app.models import Profile
    
    query = select(Job).where(
        Job.id.in_(payload.ids),
        Job.status.in_(["pending", "queued", "running", "processing_provider", "uploading_result"])
    )
    if user.role != "admin":
        query = query.where(Job.user_id == user.id)
        
    jobs = (await db.execute(query)).scalars().all()
    
    cancelled_count = 0
    for job in jobs:
        job.status = "cancelled"
        db.add(JobLog(job_id=job.id, level="info", message="Job cancelled via Bulk Cancel"))
        cancelled_count += 1
        
    if cancelled_count > 0:
        await db.commit()
        # Recalculate profile active jobs
        await db.execute(
            update(Profile).values(
                active_jobs=select(func.count(Job.id)).where(Job.profile_id == Profile.id, Job.status.in_(["running", "processing_provider", "uploading_result"])).scalar_subquery(),
                active_video_jobs=select(func.count(Job.id)).where(Job.profile_id == Profile.id, Job.status.in_(["running", "processing_provider", "uploading_result"]), Job.job_type == "video").scalar_subquery()
            )
        )
        await db.execute(
            update(Profile).values(status="logged_in").where(Profile.status == "running_job", Profile.active_jobs == 0)
        )
        await db.commit()
        
    return {"cancelled": cancelled_count}


@router.post("/{job_id}/cancel", response_model=JobOut)
async def cancel_job(job_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Job:
    job = await service.assert_job_owner(db, job_id, user.id, user.role == "admin")
    if job.status in {"success", "failed", "cancelled"}:
        raise InvalidPayload(f"Cannot cancel job in status {job.status}")
    job.status = "cancelled"
    db.add(JobLog(job_id=job.id, level="info", message="Job cancelled by user"))
    await db.commit()
    await db.refresh(job)
    return job


@router.patch("/{job_id}", response_model=JobOut)
async def edit_job(
    job_id: uuid.UUID, payload: JobUpdate, user: CurrentUser, db: DbSession,
) -> Job:
    """Edit a job's prompt before it starts running. Only `pending` and
    `queued` jobs are editable — once the worker picks it up the prompt
    has already been submitted to Grok."""
    job = await service.assert_job_owner(db, job_id, user.id, user.role == "admin")
    if job.status not in {"pending", "queued"}:
        raise InvalidPayload(
            f"Chỉ sửa được job đang chờ (pending/queued). Job này đang {job.status}."
        )
    changed = []
    if payload.prompt is not None and payload.prompt != job.prompt:
        job.prompt = payload.prompt
        changed.append("prompt")
    if payload.options is not None:
        merged = dict(job.input_payload or {})
        merged.update(payload.options)
        job.input_payload = merged
        changed.append("options")
    if not changed:
        return job
    db.add(JobLog(job_id=job.id, level="info",
                  message=f"Job edited by user: {','.join(changed)}"))
    await db.commit()
    await db.refresh(job)
    return job


class JobTagsIn(BaseModel):
    """Replaces the full tag set on a job. To add one tag without nuking
    the others, the client should read tags first, append, then PUT."""
    tags: list[str] = Field(default_factory=list, max_length=20)


@router.put("/{job_id}/tags", response_model=JobOut)
async def set_job_tags(
    job_id: uuid.UUID, payload: JobTagsIn, user: CurrentUser, db: DbSession,
) -> Job:
    job = await service.assert_job_owner(db, job_id, user.id, user.role == "admin")
    # Dedupe + strip whitespace + drop empty. Keep client-supplied order
    # so the UI's drag-reorder gesture is preserved on save.
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in payload.tags:
        t = (raw or "").strip()
        if not t or t in seen or len(t) > 50:
            continue
        seen.add(t)
        cleaned.append(t)
    job.tags = cleaned
    await db.commit()
    await db.refresh(job)
    return job


@router.post("/{job_id}/favorite", response_model=JobOut)
async def toggle_favorite(
    job_id: uuid.UUID, user: CurrentUser, db: DbSession,
) -> Job:
    """Flip the favorite bit. Idempotent on either branch — the value
    reflects the *post-toggle* state so the UI can rely on the response
    instead of double-tracking optimistically."""
    job = await service.assert_job_owner(db, job_id, user.id, user.role == "admin")
    job.is_favorite = not job.is_favorite
    await db.commit()
    await db.refresh(job)
    return job


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_job(job_id: uuid.UUID, user: CurrentUser, db: DbSession) -> None:
    """Delete a job permanently. Only terminal-status jobs can be deleted —
    in-flight jobs must be cancelled first to release their slot."""
    job = await service.assert_job_owner(db, job_id, user.id, user.role == "admin")
    if job.status in {"running", "processing_provider", "uploading_result"}:
        raise InvalidPayload(
            "Job đang chạy không thể xóa. Bấm Cancel trước rồi mới xóa được."
        )
    await db.delete(job)
    await db.commit()


class BulkDeleteIn(BaseModel):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=500)


class BulkDeleteOut(BaseModel):
    deleted: int
    skipped_in_flight: int
    skipped_not_owned: int


@router.post("/bulk-delete", response_model=BulkDeleteOut)
async def bulk_delete_jobs(
    payload: BulkDeleteIn, user: CurrentUser, db: DbSession,
) -> BulkDeleteOut:
    """Delete many jobs in one request.

    Per-row authz: each job must belong to the caller (admins/super_admin
    bypass via `assert_job_owner`). Rows that don't pass are silently
    skipped — the response reports how many fell into each bucket so the
    FE can show "X deleted, Y skipped".

    In-flight jobs (running / processing / uploading) are NEVER deleted
    here. Caller can pass them in the list — they end up in
    `skipped_in_flight` and the rest are still processed (vs aborting
    the whole batch on the first running job).
    """
    deleted = 0
    skipped_in_flight = 0
    skipped_not_owned = 0
    IN_FLIGHT = {"running", "processing_provider", "uploading_result"}
    is_admin = user.role in ("admin", "super_admin")
    for jid in payload.ids:
        try:
            job = await service.assert_job_owner(db, jid, user.id, is_admin)
        except Exception:  # noqa: BLE001 — NotFound or PermissionDenied
            skipped_not_owned += 1
            continue
        if job.status in IN_FLIGHT:
            skipped_in_flight += 1
            continue
        await db.delete(job)
        deleted += 1
    await db.commit()
    return BulkDeleteOut(
        deleted=deleted,
        skipped_in_flight=skipped_in_flight,
        skipped_not_owned=skipped_not_owned,
    )


@router.get("/{job_id}/logs", response_model=list[JobLogOut])
async def get_job_logs(job_id: uuid.UUID, user: CurrentUser, db: DbSession) -> list[JobLog]:
    await service.assert_job_owner(db, job_id, user.id, user.role == "admin")
    result = await db.execute(select(JobLog).where(JobLog.job_id == job_id).order_by(JobLog.created_at))
    return list(result.scalars().all())


class JobFileOut(BaseModel):
    id: uuid.UUID
    file_name: str
    file_type: str
    mime_type: str | None
    file_size: int | None
    download_url: str


@router.get("/{job_id}/files", response_model=list[JobFileOut])
async def list_job_files(job_id: uuid.UUID, user: CurrentUser, db: DbSession) -> list[JobFileOut]:
    """Return all output files for this job (Grok may produce 1-4 image variations)."""
    await service.assert_job_owner(db, job_id, user.id, user.role == "admin")
    rows = (await db.execute(
        select(File).where(File.job_id == job_id, File.file_type != "input").order_by(File.created_at)
    )).scalars().all()
    return [
        JobFileOut(
            id=f.id,
            file_name=f.file_name,
            file_type=f.file_type,
            mime_type=f.mime_type,
            file_size=f.file_size,
            download_url=f"/api/files/{f.id}/download",
        ) for f in rows
    ]
