"""Polling worker — multi-tab per profile.

Profile concurrency is gated by `active_jobs / max_concurrent_jobs` counters
(atomic UPDATE). Multiple worker iterations can hold slots on the same profile
simultaneously, each driving its own Chromium tab via Playwright.

State machine per job:
  queued → running → processing_provider → uploading_result → success
                                                            ↘ failed (terminal or retry)
"""

import asyncio
import os
import time
import sys
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import SessionLocal
from app.core.sanitize import scrub_secrets
from app.models import Job, JobLog, Profile, User
from app.modules.admin.notifications import service as notif
from app.modules.grok.files import service as files_service
from app.modules.grok.jobs import service as jobs_service
from app.providers import JobInput, get_provider
from app.workers import webhook

WORKER_ID = os.environ.get("HOSTNAME", "worker") + "-" + str(os.getpid())

# Retry tables come from app.core.config now (JOB_BACKOFF_SECONDS /
# JOB_RATE_LIMIT_BACKOFF / JOB_TERMINAL_ERROR_CODES) so they're tunable
# per-deploy via env without a rebuild. Read once at module import.
from app.core.config import settings as _settings  # noqa: E402

BACKOFF_SECONDS = _settings.job_backoff_seconds
RATE_LIMIT_BACKOFF_SECONDS = _settings.job_rate_limit_backoff
TERMINAL_ERROR_CODES = _settings.job_terminal_error_codes
RUNNING_JOB_STATES = ("running", "processing_provider", "uploading_result")


async def _pick_job(db: AsyncSession, job_type_filter: str | None) -> Job | None:
    # NOTE: retry-cap enforcement lives in process_one's should_retry path —
    # we do NOT filter retry_count here, because the FINAL allowed attempt
    # has retry_count == max_retry at queue time and must still be picked up.
    # Zombie jobs (retry_count > max_retry from old bug history) are reaped
    # by _startup_recovery instead.
    now = datetime.now(timezone.utc)
    stmt = (
        select(Job)
        .where(
            Job.status == "queued",
            (Job.next_attempt_at.is_(None)) | (Job.next_attempt_at <= now),
        )
        .order_by(Job.priority.desc(), Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job_type_filter:
        stmt = stmt.where(Job.job_type == job_type_filter)
    return (await db.execute(stmt)).scalar_one_or_none()


async def _try_acquire_slot(
    db: AsyncSession, profile_id: uuid.UUID, job_type: str = "image",
) -> bool:
    """Atomically bump the right counter if both caps allow it.

    Two independent caps:
      • active_jobs < max_concurrent_jobs (overall — image + video)
      • active_video_jobs < max_concurrent_video (video only)

    Video jobs check both. Non-video jobs check only the first. Both
    increment active_jobs; video additionally increments
    active_video_jobs so it counts twice toward the limits.
    """
    is_video = job_type == "video"
    where_clauses = [
        Profile.id == profile_id,
        Profile.active_jobs < Profile.max_concurrent_jobs,
        Profile.status.in_(["logged_in", "running_job"]),
    ]
    values: dict = {
        "active_jobs": Profile.active_jobs + 1,
        "status": "running_job",
        "last_used_at": datetime.now(timezone.utc),
    }
    if is_video:
        where_clauses.append(Profile.active_video_jobs < Profile.max_concurrent_video)
        values["active_video_jobs"] = Profile.active_video_jobs + 1

    result = await db.execute(
        update(Profile)
        .where(*where_clauses)
        .values(**values)
        .returning(Profile.id)
    )
    return result.scalar_one_or_none() is not None


async def _release_slot(db: AsyncSession, profile_id: uuid.UUID,
                        new_status: str | None = None,
                        error_message: str | None = None,
                        job_type: str = "image") -> None:
    """Decrement active_jobs (clamped to 0). If video, also decrement
    active_video_jobs. If counter reaches 0, set status accordingly."""
    profile = await db.get(Profile, profile_id, with_for_update=True)
    if not profile:
        return
    profile.active_jobs = max(0, profile.active_jobs - 1)
    if job_type == "video":
        profile.active_video_jobs = max(0, profile.active_video_jobs - 1)
    prev_profile_status = profile.status
    if profile.active_jobs == 0:
        profile.status = new_status or "logged_in"
    elif new_status and new_status != "logged_in":
        # Force terminal status (e.g. blocked/need_login) even if other slots are running
        profile.status = new_status
    if error_message is not None:
        # Scrub vendor/provider responses before persisting — they often
        # echo back tokens, query params with keys, etc.
        profile.error_message = scrub_secrets(error_message)
    profile.last_used_at = datetime.now(timezone.utc)

    # Alert admins khi profile chuyển sang terminal status cần can thiệp
    # (need_login / blocked / expired). Bỏ qua khi chỉ chuyển về
    # logged_in / running_job — đó là healthy transitions.
    _NOTIFY_PROFILE_STATES = {"need_login", "blocked", "expired", "quota_exhausted"}
    if (
        profile.status in _NOTIFY_PROFILE_STATES
        and profile.status != prev_profile_status
    ):
        try:
            from app.modules.admin.notifications import service as _notif
            owner = await db.get(User, profile.user_id)
            await _notif.notify_admins_async(
                db,
                domain_id=owner.domain_id if owner else None,
                kind="profile_needs_attention",
                title=f"Profile {profile.name} cần xử lý: {profile.status}",
                body=(profile.error_message or "")[:200],
                target_url=f"/profiles?id={profile.id}",
                severity="warning",
            )
        except Exception:  # noqa: BLE001
            pass


def _profile_status_after_error(error_code: str | None) -> str | None:
    if error_code in {"cookie_expired", "captcha_required"}:
        return "need_login"
    if error_code == "provider_blocked":
        return "blocked"
    return None


async def _maybe_send_webhook(db: AsyncSession, job: Job) -> None:
    user = await db.get(User, job.user_id)
    if not user or not user.webhook_url:
        return
    event = {"success": "job.success", "failed": "job.failed", "cancelled": "job.cancelled"}.get(job.status)
    if not event:
        return
    try:
        delivered = await webhook.deliver(user, job, event)
        db.add(JobLog(job_id=job.id, level="info" if delivered else "warning",
                      message=f"Webhook {event} {'delivered' if delivered else 'failed'}"))
    except Exception as exc:  # noqa: BLE001
        db.add(JobLog(job_id=job.id, level="error",
                      message=f"Webhook exception: {type(exc).__name__}: {exc}"))


async def _watch_for_cancel(job_id: uuid.UUID, target: asyncio.Task,
                            interval: int = 3) -> None:
    """Background poll: every N seconds re-read job.status. If user cancels,
    abort the running provider task. Exits cleanly when the task ends."""
    while not target.done():
        try:
            await asyncio.sleep(interval)
            if target.done():
                return
            async with SessionLocal() as db2:
                fresh = await db2.get(Job, job_id)
                if fresh and fresh.status == "cancelled":
                    target.cancel()
                    return
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            # transient DB error — keep watching, don't tear down the run
            continue


async def process_one(db: AsyncSession, job: Job) -> None:
    # Race window: user may have hit Cancel between claim and process. The
    # claim step set status="running" but cancel() can still write "cancelled".
    # Re-read fresh state once before doing real work.
    await db.refresh(job)
    if job.status == "cancelled":
        db.add(JobLog(job_id=job.id, level="info",
                      message=f"Worker {WORKER_ID} skipped — job already cancelled"))
        await db.commit()
        return

    job.started_at = datetime.now(timezone.utc)
    db.add(JobLog(job_id=job.id, level="info",
                  message=f"Worker {WORKER_ID} picked up job (retry={job.retry_count})"))

    # Auto-rotate: if a prior retry banished the original profile, the
    # profile_id is now NULL. Pick a fresh one from the pool (skipping any
    # already-banned). If the pool is exhausted for this job, fail
    # cleanly instead of crashing in _try_acquire_slot.
    if job.profile_id is None:
        alt = await jobs_service.pick_alternate_profile(db, job)
        if alt is None:
            job.status = "failed"
            job.completed_at = datetime.now(timezone.utc)
            job.error_message = "[no_profile] All profiles exhausted (rate-limited or banned)."
            db.add(JobLog(
                job_id=job.id, level="error",
                message="No alternate profile available after rotation — failing.",
            ))
            await db.commit()
            return
        job.profile_id = alt.id
        db.add(JobLog(
            job_id=job.id, level="info",
            message=f"Rotated to profile {str(alt.id)[:8]} ({alt.name})",
        ))
        await db.flush()

    slot_held: uuid.UUID | None = None
    # Image jobs may go through the pure-HTTP API path (no Chromium tab);
    # we let the provider acquire a DOM slot itself if it actually falls
    # back to Playwright. This lets dozens of /imagine API calls run in
    # parallel through httpx without hitting the artificially-low
    # `max_concurrent_jobs` cap that was sized for Chromium tabs.
    # Video stays on pre-acquire because every video job needs DOM.
    pre_acquire = job.job_type == "video"
    if job.profile_id and pre_acquire:
        # First: try the originally-assigned profile (fast path — keeps
        # session warm cookies / project pinning intact).
        if await _try_acquire_slot(db, job.profile_id, job_type=job.job_type):
            slot_held = job.profile_id
        else:
            # Hot-failover: original profile is at capacity right now.
            # Look for any sibling profile in the pool that has a free
            # slot of the right type and switch to it. The original
            # profile is excluded just for this attempt — not banned —
            # so future jobs still consider it.
            original_pid = job.profile_id
            payload = job.input_payload or {}
            already_banned = list(payload.get("_banned_profiles") or [])
            try_skip = list(set(already_banned + [str(original_pid)]))
            try:
                alt = await jobs_service._resolve_profile_for_job(
                    db,
                    requested_id=None,
                    user_id=job.user_id,
                    provider=job.provider,
                    excluded_profile_ids=try_skip,
                    job_type=job.job_type,
                )
            except Exception:  # noqa: BLE001 — pool empty or any other
                alt = None

            if alt and await _try_acquire_slot(db, alt.id, job_type=job.job_type):
                job.profile_id = alt.id
                slot_held = alt.id
                db.add(JobLog(
                    job_id=job.id, level="info",
                    message=f"Hot-failover: original at capacity, switched to "
                            f"{alt.name} ({str(alt.id)[:8]})",
                ))
            else:
                # No sibling had capacity either — keep the original
                # binding and requeue normally so we don't fragment
                # workload across stale profiles.
                job.status = "queued"
                db.add(JobLog(job_id=job.id, level="warning",
                              message="Pool at capacity, requeue"))
                await db.commit()
                return

    profile: Profile | None = None
    if slot_held:
        profile = await db.get(Profile, slot_held)
    elif job.profile_id:
        # Image path skipped pre-acquire — still load profile so provider
        # has the cookies/CDP path available.
        profile = await db.get(Profile, job.profile_id)
    await db.commit()

    profile_terminal_status: str | None = None
    try:
        provider = get_provider(job.provider)
        job.status = "processing_provider"
        await db.commit()

        # Resolve attachments. Two sources, both honoured:
        #   - reference_images: list[uuid]   (multi-ref, max 4)
        #   - input_image_file_id: uuid      (legacy single-ref)
        # Both can be sent together; we de-dupe by file_id and keep order
        # so the operator's first picker slot maps to Grok's first
        # reference slot. Provider's _attach_files iterates the full
        # list so all of them end up in the Grok chat upload.
        attachments: list = []
        opts = job.input_payload or {}
        ref_ids: list[str] = []
        raw_refs = opts.get("reference_images") or []
        if isinstance(raw_refs, list):
            for r in raw_refs:
                if isinstance(r, str) and r and r not in ref_ids:
                    ref_ids.append(r)
        input_id = opts.get("input_image_file_id")
        if input_id and input_id not in ref_ids:
            ref_ids.append(input_id)

        if ref_ids:
            import httpx
            from app.providers.base import InputAttachment
            from app.modules.grok.files import service as files_service_mod
            from app.models import File as FileModel
            # Cap to 4 — matches the UI picker and stays well under Grok's
            # observed limit of 8 chat attachments before the upload
            # widget starts dropping files.
            for rid in ref_ids[:4]:
                # URL refs (http/https) — fetch bytes directly. Partners
                # often pre-host their source images on a CDN and skip
                # the upload-input round trip; the contract docs both
                # "uuid file_id" and "https://… URL" for reference_images.
                if isinstance(rid, str) and rid.startswith(("http://", "https://")):
                    try:
                        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as cli:
                            r = await cli.get(rid)
                            r.raise_for_status()
                        # Derive a sensible filename from the URL path
                        # (fallback to "ref.jpg" so Grok's upload UI has
                        # something to display).
                        from urllib.parse import urlparse
                        path = urlparse(rid).path
                        name = path.rsplit("/", 1)[-1] or "ref.jpg"
                        if "." not in name:
                            name += ".jpg"
                        mime = r.headers.get("content-type", "image/jpeg").split(";")[0]
                        attachments.append(InputAttachment(
                            name=name, mime=mime, bytes=r.content,
                        ))
                        db.add(JobLog(job_id=job.id, level="info",
                                      message=f"Attached URL ref {name} ({len(r.content)} bytes from {rid[:60]}…)"))
                    except Exception as exc:  # noqa: BLE001
                        db.add(JobLog(job_id=job.id, level="warning",
                                      message=f"URL ref download failed {rid[:60]}: {type(exc).__name__}: {exc}"))
                    continue
                # file_id (UUID) path — original behaviour.
                try:
                    f = await db.get(FileModel, uuid.UUID(rid))
                    if f and f.user_id == job.user_id:
                        data = await files_service_mod.read_file_bytes(f)
                        attachments.append(InputAttachment(
                            name=f.file_name, mime=f.mime_type or "image/png", bytes=data,
                        ))
                        db.add(JobLog(job_id=job.id, level="info",
                                      message=f"Attached input {f.file_name} ({len(data)} bytes)"))
                except Exception as exc:  # noqa: BLE001
                    db.add(JobLog(job_id=job.id, level="warning",
                                  message=f"Failed input image {rid}: {exc}"))

        # Hard wall-clock cap on the whole provider run. If anything
        # inside hangs (Chromium freeze, dead CDP, page.evaluate stuck,
        # etc.) we cut the cord rather than letting the job sit in
        # processing_provider forever. Image: 5 min, video: 8 min.
        hard_cap = 300 if job.job_type != "video" else 480
        # Look up the project slug (if scoped) so the worker hits
        # grok.com/project/<slug> instead of /imagine, keeping each
        # tenant's chat history separated.
        #
        # Cross-profile rotation guard: when an earlier retry rotated the
        # job from profile A → B, `job.project_id` still references A's
        # GrokProject row, whose `grok_project_id` slug exists only in
        # A's Grok account. Passing it to B makes Grok's API return
        # `404 Workspace not found` and forces the slower DOM fallback —
        # which then often times out. Detect the mismatch and either
        # pick a project that belongs to the current profile, or fall
        # back to `None` (worker hits /imagine without a project pin,
        # which is harmless — just loses per-tenant chat separation
        # for this one job).
        grok_project_id: str | None = None
        if job.project_id:
            from app.models import GrokProject
            gp = await db.get(GrokProject, job.project_id)
            if gp and gp.profile_id == job.profile_id:
                grok_project_id = gp.grok_project_id
            elif gp:
                # Mismatch — find ANY project on the current profile.
                alt_gp = (await db.execute(
                    select(GrokProject)
                    .where(GrokProject.profile_id == job.profile_id)
                    .limit(1)
                )).scalar_one_or_none()
                if alt_gp:
                    grok_project_id = alt_gp.grok_project_id
                    db.add(JobLog(
                        job_id=job.id, level="info",
                        message=(
                            f"Project re-mapped after profile rotation: "
                            f"{gp.grok_project_id[:8]}… (profile {str(gp.profile_id)[:8]}) "
                            f"→ {alt_gp.grok_project_id[:8]}… (profile {str(job.profile_id)[:8]})"
                        ),
                    ))
                else:
                    db.add(JobLog(
                        job_id=job.id, level="info",
                        message=(
                            f"Project {gp.grok_project_id[:8]}… doesn't belong to "
                            f"rotated profile {str(job.profile_id)[:8]} and the "
                            f"profile has no sibling project — falling back to /imagine"
                        ),
                    ))

        # Run provider as a task + watchdog that aborts on user cancel.
        provider_task = asyncio.create_task(provider.run(JobInput(
            prompt=job.prompt,
            job_type=job.job_type,
            options=job.input_payload,
            profile_path=profile.profile_path if profile else "",
            attachments=attachments,
            grok_project_id=grok_project_id,
        )))
        watcher = asyncio.create_task(_watch_for_cancel(job.id, provider_task))
        cancelled_mid_run = False
        timed_out = False
        try:
            result = await asyncio.wait_for(
                asyncio.shield(provider_task), timeout=hard_cap,
            )
        except asyncio.CancelledError:
            cancelled_mid_run = True
            result = None
        except asyncio.TimeoutError:
            timed_out = True
            provider_task.cancel()
            try:
                await provider_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            result = None
        finally:
            watcher.cancel()
            try:
                await watcher
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        if timed_out:
            from app.providers.base import JobResult as _JR
            result = _JR(
                success=False, error_code="rate_limited",
                error_message=f"Provider hung past {hard_cap}s hard cap (Chromium overload).",
                retryable=True,
            )

        if cancelled_mid_run:
            job.status = "cancelled"
            job.completed_at = datetime.now(timezone.utc)
            job.error_message = "[cancelled] Cancelled by user during provider run"
            db.add(JobLog(job_id=job.id, level="info",
                          message="Provider task cancelled mid-run"))
            # skip the success/retry paths
            result = None

        if result is None:
            # cancelled_mid_run path — already wrote terminal status above.
            pass
        elif result.success and result.files:
            job.status = "uploading_result"
            saved_ids: list[uuid.UUID] = []
            total_bytes = 0
            for rf in result.files:
                actual_type = (
                    "video" if rf.mime.startswith("video/")
                    else "image" if rf.mime.startswith("image/")
                    else job.job_type
                )
                file_rec = await files_service.save_job_result(
                    db, user_id=job.user_id, job_id=job.id,
                    file_name=rf.name, file_type=actual_type,
                    mime_type=rf.mime, data=rf.bytes,
                )
                saved_ids.append(file_rec.id)
                total_bytes += len(rf.bytes)
            job.result_file_id = saved_ids[0]
            job.result_url = f"/api/files/{saved_ids[0]}/download"
            job.status = "success"
            # Clear the error_message from any earlier retry attempt —
            # a successful retry should not leave the previous failure's
            # message lingering on the row (UI was showing the old
            # `[rate_limited]` text on jobs that ultimately succeeded).
            job.error_message = None
            job.completed_at = datetime.now(timezone.utc)
            db.add(JobLog(job_id=job.id, level="info",
                          message=f"Job success: {len(saved_ids)} file(s), {total_bytes} bytes"))
            await notif.log_notification_async(
                db, user_id=job.user_id, kind="job_completed",
                title=f"Grok {job.job_type} hoàn tất",
                body=(job.prompt or "")[:120],
                target_url=f"/grok/jobs/{job.id}",
                severity="success",
            )
        else:
            error_code = result.error_code or "unknown_error"
            # Scrub before storing — vendor errors can include URLs with
            # tokens, API keys in their reply text, JWTs, etc.
            job.error_message = scrub_secrets(
                f"[{error_code}] {result.error_message or 'unknown'}"
            )
            db.add(JobLog(job_id=job.id, level="error",
                          message=f"Provider error: {job.error_message}",
                          context={"error_code": error_code, "extra": result.extra}))
            profile_terminal_status = _profile_status_after_error(error_code)

            should_retry = (
                result.retryable
                and error_code not in TERMINAL_ERROR_CODES
                and job.retry_count < job.max_retry
            )
            if should_retry:
                job.retry_count += 1

                # Decide rotation first — we need to know whether the
                # retry will reuse the same profile (apply backoff to let
                # it cool down) or jump to a sibling (no backoff needed,
                # the new profile has its own quota).
                ROTATE_CODES = {"rate_limited", "browser_crashed", "timeout"}
                rotate = (
                    error_code in ROTATE_CODES
                    or "TargetClosedError" in (job.error_message or "")
                    or "Target page, context or browser has been closed" in (job.error_message or "")
                )

                if rotate and job.profile_id:
                    # Check pool capacity BEFORE banishing the profile so
                    # we don't end up with banned=[A,B,C] (everyone) and
                    # an unrecoverable job. If we're about to ban the last
                    # sibling, fall through to backoff-on-same-profile
                    # instead.
                    payload = dict(job.input_payload or {})
                    banned = list(payload.get("_banned_profiles") or [])
                    pid_str = str(job.profile_id)
                    # Probe for an alternate profile right now (cheap query).
                    try_skip = list(set(banned + [pid_str]))
                    try:
                        alt = await jobs_service._resolve_profile_for_job(
                            db, requested_id=None, user_id=job.user_id,
                            provider=job.provider,
                            excluded_profile_ids=try_skip,
                            job_type=job.job_type,
                        )
                    except Exception:  # noqa: BLE001
                        alt = None
                    if alt is not None:
                        # Sibling available → rotate with NO backoff.
                        # Worker picks up immediately on next loop tick.
                        if pid_str not in banned:
                            banned.append(pid_str)
                        payload["_banned_profiles"] = banned
                        job.input_payload = payload
                        reason = error_code if error_code in ROTATE_CODES else "tab_crashed"
                        db.add(JobLog(
                            job_id=job.id, level="info",
                            message=f"Rotating away from profile {pid_str[:8]} (reason={reason}), banned={len(banned)}",
                        ))
                        job.profile_id = None
                        # Add minimum backoff when rotating for timeout/rate_limited
                        # to avoid burning all retries in seconds when Grok is
                        # rate-limiting at application level (all profiles affected).
                        if error_code in ("timeout", "rate_limited"):
                            _rotate_delay = 45 if error_code == "timeout" else 15
                            job.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=_rotate_delay)
                            job.status = "queued"
                            db.add(JobLog(job_id=job.id, level="info",
                                          message=f"Rotate to sibling with {_rotate_delay}s cooldown ({job.retry_count}/{job.max_retry}, code={error_code})"))
                        else:
                            job.next_attempt_at = None  # immediate retry on sibling
                            job.status = "queued"
                            db.add(JobLog(job_id=job.id, level="info",
                                          message=f"Retry immediately on sibling profile ({job.retry_count}/{job.max_retry}, code={error_code})"))
                    else:
                        # No sibling available → apply normal backoff
                        # against the SAME profile (give Grok cooldown).
                        table = (RATE_LIMIT_BACKOFF_SECONDS if error_code == "rate_limited"
                                 else BACKOFF_SECONDS)
                        delay = table[min(job.retry_count - 1, len(table) - 1)]
                        job.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
                        job.status = "queued"
                        db.add(JobLog(job_id=job.id, level="info",
                                      message=f"No alt profile — retry on same after ~{delay}s ({job.retry_count}/{job.max_retry}, code={error_code})"))
                else:
                    # Non-rotatable error → backoff on same profile.
                    table = (RATE_LIMIT_BACKOFF_SECONDS if error_code == "rate_limited"
                             else BACKOFF_SECONDS)
                    delay = table[min(job.retry_count - 1, len(table) - 1)]
                    job.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
                    job.status = "queued"
                    db.add(JobLog(job_id=job.id, level="info",
                                  message=f"Retry after ~{delay}s ({job.retry_count}/{job.max_retry}, code={error_code})"))
            else:
                job.status = "failed"
                job.completed_at = datetime.now(timezone.utc)
                await notif.log_notification_async(
                    db, user_id=job.user_id, kind="job_failed",
                    title=f"Grok {job.job_type} lỗi",
                    body=(job.error_message or "")[:160],
                    target_url=f"/grok/jobs/{job.id}",
                    severity="error",
                )

    except Exception as exc:  # noqa: BLE001
        # Same scrub as the provider-error branch — exception text can
        # contain credentials from connection strings / response bodies.
        err_text = scrub_secrets(
            f"Worker exception: {type(exc).__name__}: {exc}"
        )
        job.error_message = err_text
        db.add(JobLog(job_id=job.id, level="error", message=err_text))

        # Treat uncaught Playwright / Chromium crashes the same way the
        # provider-error branch treats `tab_crashed`: rotate to a sibling
        # profile if one's available and retries remain. Without this,
        # any exception that escapes the provider's try/except — most
        # commonly TargetClosedError when Chromium dies mid-setup —
        # marks the job dead on first hit, ignoring max_retry entirely.
        exc_name = type(exc).__name__
        retryable_exc = (
            exc_name in {"TargetClosedError", "Error"}
            or "Target page, context or browser has been closed" in err_text
            or "TargetClosedError" in err_text
            or "browser has been closed" in err_text
        )
        if retryable_exc and job.retry_count < job.max_retry:
            # Try to rotate to a sibling profile — same logic as the
            # provider error branch, condensed inline.
            job.retry_count += 1
            payload = dict(job.input_payload or {})
            banned = list(payload.get("_banned_profiles") or [])
            pid_str = str(job.profile_id) if job.profile_id else None
            alt = None
            if pid_str:
                try_skip = list(set(banned + [pid_str]))
                try:
                    alt = await jobs_service._resolve_profile_for_job(
                        db, requested_id=None, user_id=job.user_id,
                        provider=job.provider,
                        excluded_profile_ids=try_skip,
                        domain_id=getattr(job, "domain_id", None),
                        job_type=job.job_type,
                    )
                except Exception:  # noqa: BLE001
                    alt = None
            if alt is not None and pid_str:
                if pid_str not in banned:
                    banned.append(pid_str)
                payload["_banned_profiles"] = banned
                job.input_payload = payload
                job.profile_id = None
                job.next_attempt_at = None
                job.status = "queued"
                db.add(JobLog(job_id=job.id, level="info",
                              message=f"Worker-level rotate after {exc_name} ({job.retry_count}/{job.max_retry})"))
            else:
                # No alt available — backoff on same profile.
                delay = BACKOFF_SECONDS[min(job.retry_count - 1, len(BACKOFF_SECONDS) - 1)]
                job.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
                job.status = "queued"
                db.add(JobLog(job_id=job.id, level="info",
                              message=f"Worker-level retry on same after ~{delay}s ({job.retry_count}/{job.max_retry})"))
        else:
            # Either non-retryable exception (rare) or out of retries.
            job.status = "failed"
            job.completed_at = datetime.now(timezone.utc)

    finally:
        if slot_held:
            await _release_slot(db, slot_held,
                                new_status=profile_terminal_status,
                                job_type=job.job_type)

    if job.status in {"success", "failed", "cancelled"}:
        await _maybe_send_webhook(db, job)

    # Notify domain admins + super_admin on terminal FAILURE so they
    # can act (re-login profile, refund credit, contact customer).
    # Skip success/cancelled — too noisy. Skip when retry will fire next
    # because the user can't fix anything mid-retry; only the final
    # status='failed' (no more retries) raises an admin alert.
    if job.status == "failed":
        try:
            from app.modules.admin.notifications import service as _notif
            user = await db.get(User, job.user_id)
            domain_id = user.domain_id if user else None
            err = (job.error_message or "")[:120]
            await _notif.notify_admins_async(
                db,
                domain_id=domain_id,
                kind="job_failed",
                title=f"Job thất bại — {user.email if user else 'unknown'}",
                body=f"#{str(job.id)[:8]} ({job.job_type}/{job.provider}): {err}",
                target_url=f"/jobs?id={job.id}",
                severity="warning",
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[worker] notify on fail err: {exc}", flush=True)

    await db.commit()


async def _startup_recovery() -> None:
    """Repair state left behind by a crashed worker:

    1. Jobs stuck in running/processing/uploading → requeue (the previous
       worker is gone; nothing is driving them).
    2. Recompute every profile's `active_jobs` from real running-job count;
       if it drops to 0, restore status from `running_job` to `logged_in`.
    3. Reap orphan VNC containers whose profile was deleted while we were down.
    """
    async with SessionLocal() as db:
        stuck = (await db.execute(
            select(Job).where(Job.status.in_(RUNNING_JOB_STATES))
        )).scalars().all()
        requeued = failed = 0
        now = datetime.now(timezone.utc)
        for j in stuck:
            # If the job already exhausted its retries, send it to terminal
            # rather than letting the loop pick it up forever. The previous
            # worker run was the LAST allowed attempt — count it as failed.
            if j.retry_count >= j.max_retry:
                j.status = "failed"
                j.completed_at = now
                if not j.error_message:
                    j.error_message = "[worker_crashed] Worker died mid-job; retries exhausted"
                db.add(JobLog(job_id=j.id, level="error",
                              message=f"Worker {WORKER_ID} marked stuck job failed (retries exhausted)"))
                failed += 1
            else:
                j.status = "queued"
                j.started_at = None
                # Don't reset next_attempt_at if it was already set in the future.
                db.add(JobLog(job_id=j.id, level="warning",
                              message=f"Worker {WORKER_ID} recovered orphan job (was {j.status})"))
                requeued += 1

        profiles = (await db.execute(select(Profile))).scalars().all()
        all_pids = {str(p.id) for p in profiles}
        for p in profiles:
            live = (await db.execute(
                select(func.count()).select_from(Job)
                .where(Job.profile_id == p.id, Job.status.in_(RUNNING_JOB_STATES))
            )).scalar_one()
            live_video = (await db.execute(
                select(func.count()).select_from(Job)
                .where(
                    Job.profile_id == p.id,
                    Job.status.in_(RUNNING_JOB_STATES),
                    Job.job_type == "video",
                )
            )).scalar_one()
            if p.active_jobs != live:
                p.active_jobs = int(live or 0)
            # Reset video counter too — release path used to skip this
            # before max_concurrent_video landed, so old DBs have stale
            # values that block new video jobs from getting slots.
            if p.active_video_jobs != live_video:
                p.active_video_jobs = int(live_video or 0)
            if p.active_jobs == 0 and p.status == "running_job":
                p.status = "logged_in"
        await db.commit()
        if stuck:
            print(f"[worker] startup-recovery: requeued={requeued} failed={failed}", flush=True)

        # Reap zombies: queued jobs whose retry_count is STRICTLY beyond the
        # cap. retry_count == max_retry is legitimate (final allowed attempt
        # is pending). retry_count > max_retry can only happen from old buggy
        # state where requeues skipped the cap check.
        zombies = (await db.execute(
            select(Job).where(
                Job.status == "queued",
                Job.retry_count > Job.max_retry,
            )
        )).scalars().all()
        for z in zombies:
            z.status = "failed"
            z.completed_at = datetime.now(timezone.utc)
            if not z.error_message:
                z.error_message = "[retries_exhausted] Job exceeded max_retry"
            db.add(JobLog(job_id=z.id, level="error",
                          message=f"Worker {WORKER_ID} reaped zombie queued job (retry={z.retry_count}/{z.max_retry})"))
        if zombies:
            await db.commit()
            print(f"[worker] startup-recovery: reaped {len(zombies)} zombie queued job(s)", flush=True)

    try:
        from app.browser import vnc_manager
        reaped = vnc_manager.reap_orphans(all_pids)
        if reaped:
            print(f"[worker] reaped orphan VNC: {reaped}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[worker] orphan reap failed: {exc}", flush=True)


async def loop(job_type_filter: str | None = None) -> None:
    print(f"[worker] {WORKER_ID} starting (job_type={job_type_filter or 'any'})", flush=True)
    try:
        await _startup_recovery()
    except Exception as exc:  # noqa: BLE001
        print(f"[worker] startup-recovery failed: {exc}", flush=True)
    # Concurrent processing: when we claim a job, run process_one as a background
    # task so the loop can pick up another job immediately. Total parallelism is
    # bounded by sum of max_concurrent_jobs across all profiles.
    in_flight: set[asyncio.Task] = set()
    MAX_IN_FLIGHT = int(os.environ.get("WORKER_MAX_IN_FLIGHT", "20"))
    _last_reconcile = time.monotonic()
    _RECONCILE_INTERVAL = 60  # seconds

    while True:
        try:
            # Periodic counter reconciliation — fixes stale active_jobs counters
            # that leak when jobs crash without properly releasing their slot.
            if time.monotonic() - _last_reconcile >= _RECONCILE_INTERVAL:
                _last_reconcile = time.monotonic()
                try:
                    async with SessionLocal() as _rdb:
                        _profiles = (await _rdb.execute(select(Profile))).scalars().all()
                        for _p in _profiles:
                            _live = (await _rdb.execute(
                                select(func.count()).select_from(Job)
                                .where(Job.profile_id == _p.id, Job.status.in_(RUNNING_JOB_STATES))
                            )).scalar_one()
                            _live_vid = (await _rdb.execute(
                                select(func.count()).select_from(Job)
                                .where(Job.profile_id == _p.id, Job.status.in_(RUNNING_JOB_STATES), Job.job_type == "video")
                            )).scalar_one()
                            if _p.active_jobs != _live or _p.active_video_jobs != _live_vid:
                                print(f"[worker] reconcile {_p.name}: active_jobs {_p.active_jobs}->{_live}, video {_p.active_video_jobs}->{_live_vid}", flush=True)
                                _p.active_jobs = int(_live)
                                _p.active_video_jobs = int(_live_vid)
                                if _live == 0 and _p.status == "running_job":
                                    _p.status = "logged_in"
                        await _rdb.commit()
                except Exception as _exc:
                    print(f"[worker] reconcile error: {_exc}", flush=True)

            # Reap finished tasks
            in_flight = {t for t in in_flight if not t.done()}
            if len(in_flight) >= MAX_IN_FLIGHT:
                await asyncio.sleep(1)
                continue

            claimed_id: uuid.UUID | None = None
            async with SessionLocal() as db:
                async with db.begin():
                    job = await _pick_job(db, job_type_filter)
                    if job:
                        job.status = "running"
                        claimed_id = job.id
            if not claimed_id:
                # Idle poll: 0.5s shaves up to 1.5s off the time-to-pickup
                # for fresh jobs vs the previous 2s. At 16 workers × 2 req/s
                # = 32 req/s steady-state DB load when the queue is empty,
                # which is negligible compared to a single SELECT...LIMIT 1.
                await asyncio.sleep(0.5)
                continue

            async def _run(jid: uuid.UUID):
                import random
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        async with SessionLocal() as db2:
                            j = await db2.get(Job, jid)
                            if j:
                                await process_one(db2, j)
                        break  # Success!
                    except Exception as exc:  # noqa: BLE001
                        exc_str = str(exc)
                        is_deadlock = "deadlock" in exc_str.lower() or "deadlockdetectederror" in exc_str.lower()
                        if is_deadlock and attempt < max_retries - 1:
                            backoff = random.uniform(0.5, 2.0)
                            print(f"[worker] deadlock detected for job {jid}, retrying in {backoff:.2f}s (attempt {attempt+1}/{max_retries})", flush=True)
                            await asyncio.sleep(backoff)
                            continue
                        
                        print(f"[worker] background task _run failed for job {jid}: {exc}", flush=True)
                        try:
                            async with SessionLocal() as db_heal:
                                async with db_heal.begin():
                                    job = await db_heal.get(Job, jid)
                                    if job and job.status in {"running", "processing_provider", "uploading_result"}:
                                        job.status = "failed"
                                        job.error_message = f"[worker_crash] Task crashed: {type(exc).__name__}: {exc}"
                                        if job.profile_id:
                                            p = await db_heal.get(Profile, job.profile_id)
                                            if p:
                                                p.active_jobs = max(0, p.active_jobs - 1)
                                                if job.job_type == "video":
                                                    p.active_video_jobs = max(0, p.active_video_jobs - 1)
                                                if p.active_jobs == 0 and p.status == "running_job":
                                                    p.status = "logged_in"
                        except Exception as heal_exc:  # noqa: BLE001
                            print(f"[worker] background task _run healing failed: {heal_exc}", flush=True)
                        break

            task = asyncio.create_task(_run(claimed_id))
            in_flight.add(task)
        except Exception as exc:  # noqa: BLE001
            print(f"[worker] loop error: {type(exc).__name__}: {exc}", flush=True)
            await asyncio.sleep(5)


if __name__ == "__main__":
    job_type = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(loop(job_type))
