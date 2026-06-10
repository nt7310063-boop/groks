"""Stop VNC containers for profiles idle longer than threshold to free RAM.

Run periodically (cron every hour or compose service with sleep loop):
    python -m app.workers.idle_cleanup [--idle-hours 6]

Effects:
- Find profiles with active_jobs == 0 AND last_used_at older than threshold.
- Call vnc_manager.stop_for_profile() on each.
- Set profile.status = need_login (admin must Auto-login again next time).
"""

import argparse
import asyncio
import os
from datetime import datetime, timedelta, timezone

from pathlib import Path

from sqlalchemy import delete, func, select

from app.browser import vnc_manager
from app.core.config import settings
from app.core.database import SessionLocal
from app.models import File as FileModel, Job, JobLog, Profile

# Same set as workers.run.RUNNING_JOB_STATES — duplicated here to avoid an
# import cycle (run.py imports from this module's siblings).
_RUNNING_JOB_STATES = ("running", "processing_provider", "uploading_result")


async def heal_stuck_jobs() -> int:
    """Find jobs stuck in in-flight states for too long and mark them as failed.
    This releases stuck slots and lets heal_stuck_profiles clean up the profile counters."""
    fixed = 0
    async with SessionLocal() as db:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=15)
        stuck_jobs = (await db.execute(
            select(Job).where(
                Job.status.in_(["running", "processing_provider", "uploading_result"]),
                Job.updated_at < cutoff
            )
        )).scalars().all()
        for job in stuck_jobs:
            job.status = "failed"
            job.error_message = "[timeout] Stuck in in-flight state (recovered by cleanup)"
            fixed += 1
            print(f"[idle-cleanup] marked stuck job {job.id} as failed (created_at={job.created_at})", flush=True)
        if fixed:
            await db.commit()
    return fixed


async def heal_stuck_profiles() -> int:
    """Find profiles stuck in `running_job` with no real running jobs and
    repair them in-place (no worker restart needed).

    A profile gets stuck if the slot-release path failed silently
    (transaction rollback, exception in finally, worker SIGKILL'd
    mid-release). The startup-recovery in workers.run only runs on
    worker boot, so a stuck profile can stay Running forever between
    restarts. This periodic heal closes that gap.

    Strategy: for each profile in `running_job`, count its jobs that are
    actually in a running state. If 0 → reset counters and status to
    `logged_in`. Also covers the case where active_jobs is non-zero but
    no real running jobs exist (counter drift)."""
    fixed = 0
    async with SessionLocal() as db:
        stuck = (await db.execute(
            select(Profile).where(Profile.status == "running_job")
        )).scalars().all()
        for p in stuck:
            live = (await db.execute(
                select(func.count()).select_from(Job).where(
                    Job.profile_id == p.id,
                    Job.status.in_(_RUNNING_JOB_STATES),
                )
            )).scalar_one() or 0
            live_video = (await db.execute(
                select(func.count()).select_from(Job).where(
                    Job.profile_id == p.id,
                    Job.status.in_(_RUNNING_JOB_STATES),
                    Job.job_type == "video",
                )
            )).scalar_one() or 0
            if live == 0:
                p.active_jobs = 0
                p.active_video_jobs = 0
                p.status = "logged_in"
                fixed += 1
                print(f"[idle-cleanup] healed stuck profile {p.name} ({p.id}) — counters reset", flush=True)
            else:
                # Real jobs still in-flight — just sync the counters in case they drifted.
                if p.active_jobs != live:
                    p.active_jobs = int(live)
                if p.active_video_jobs != live_video:
                    p.active_video_jobs = int(live_video)
        if fixed or stuck:
            await db.commit()
    return fixed


async def cleanup(idle_hours: float) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=idle_hours)
    stopped = 0
    # Heal stuck jobs first, so that heal_stuck_profiles will find live = 0 and release the profile
    try:
        healed_jobs = await heal_stuck_jobs()
        if healed_jobs:
            print(f"[idle-cleanup] healed {healed_jobs} stuck job(s)", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[idle-cleanup] heal_stuck_jobs failed: {exc}", flush=True)
    # Heal stuck profiles first so the rest of cleanup sees accurate state.
    try:
        healed = await heal_stuck_profiles()
        if healed:
            print(f"[idle-cleanup] healed {healed} stuck profile(s)", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[idle-cleanup] heal_stuck_profiles failed: {exc}", flush=True)
    async with SessionLocal() as db:
        # Reap orphan VNC containers (profile deleted but container still running).
        all_pids = {str(p.id) for p in (
            await db.execute(select(Profile))
        ).scalars().all()}
        try:
            reaped = vnc_manager.reap_orphans(all_pids)
            if reaped:
                print(f"[idle-cleanup] reaped orphan VNC: {reaped}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[idle-cleanup] orphan reap failed: {exc}", flush=True)

        # JobLog TTL: keep ~30 days. Logs of long-finished jobs are debug-only.
        log_ttl_days = float(os.environ.get("JOBLOG_TTL_DAYS", "30"))
        log_cutoff = datetime.now(timezone.utc) - timedelta(days=log_ttl_days)
        try:
            res = await db.execute(
                delete(JobLog).where(JobLog.created_at < log_cutoff)
            )
            if res.rowcount:
                print(f"[idle-cleanup] pruned {res.rowcount} JobLog rows older than {log_ttl_days}d", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[idle-cleanup] joblog prune failed: {exc}", flush=True)
        await db.commit()

        # File TTL: delete generated/uploaded files older than N days.
        # Default 60 days — long enough for users to download, short
        # enough that the storage volume doesn't grow forever. The DB
        # row is removed too, and the on-disk blob is best-effort
        # unlinked from local storage.
        file_ttl_days = float(os.environ.get("FILE_TTL_DAYS", "60"))
        file_cutoff = datetime.now(timezone.utc) - timedelta(days=file_ttl_days)
        try:
            old_files = (await db.execute(
                select(FileModel).where(FileModel.created_at < file_cutoff)
            )).scalars().all()
            removed = 0
            bytes_freed = 0
            for f in old_files:
                # Best-effort unlink for local-storage driver only.
                if f.storage_driver == "local":
                    try:
                        path = Path(settings.LOCAL_STORAGE_PATH) / f.storage_path
                        if path.exists():
                            sz = path.stat().st_size
                            path.unlink()
                            bytes_freed += sz
                    except Exception:  # noqa: BLE001
                        pass
                await db.delete(f)
                removed += 1
            if removed:
                await db.commit()
                mb = bytes_freed / 1024 / 1024
                print(f"[idle-cleanup] pruned {removed} files older than {file_ttl_days}d ({mb:.1f} MB freed)", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[idle-cleanup] file prune failed: {exc}", flush=True)

        # Profiles likely backed by a running container
        rows = (await db.execute(
            select(Profile).where(
                Profile.active_jobs == 0,
                Profile.status.in_(["logged_in", "running_job", "opening"]),
            )
        )).scalars().all()
        for p in rows:
            last = p.last_used_at or p.last_login_check_at or p.created_at
            if last and last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if last and last >= cutoff:
                continue
            info = vnc_manager.get_for_profile(str(p.id))
            if not info:
                continue
            print(f"[idle-cleanup] stopping {p.name} ({p.id}), last_used={last}", flush=True)
            try:
                vnc_manager.stop_for_profile(str(p.id))
                p.status = "need_login"
                p.error_message = "Auto-stopped (idle)"
                stopped += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  stop failed: {exc}", flush=True)
        if stopped:
            await db.commit()

    # Self-heal the nginx VNC map. Docker IPAM reassigns container IPs
    # whenever sibling services are recreated by `docker compose up -d
    # --build` (the deploy script does this for backend/worker), and the
    # map written before that reshuffle then points at dead IPs → 502 on
    # /vnc/*. Refreshing once per cleanup tick (≈60s) closes that gap
    # without us having to predict every recreate event.
    try:
        from app.services.nginx_sync import refresh_vnc_map
        refresh_vnc_map()
    except Exception as exc:  # noqa: BLE001
        print(f"[idle-cleanup] vnc map refresh failed: {exc}", flush=True)

    return stopped


async def vnc_map_loop(interval_seconds: int) -> None:
    """Cheap, fast self-heal loop for the nginx VNC map.

    Runs much more often than the full cleanup tick (default 15s vs 1h)
    because the map needs to stay in sync with Docker IPAM reshuffles —
    a stale entry means /vnc/<short>/ returns 502 for the user. Doesn't
    touch the DB or any container state; just rewrites the map file."""
    from app.services.nginx_sync import refresh_vnc_map
    print(f"[idle-cleanup] vnc map loop: refresh every {interval_seconds}s", flush=True)
    while True:
        try:
            refresh_vnc_map()
        except Exception as exc:  # noqa: BLE001
            print(f"[idle-cleanup] vnc map refresh failed: {exc}", flush=True)
        await asyncio.sleep(interval_seconds)


async def loop_forever(interval_seconds: int, idle_hours: float,
                       map_refresh_seconds: int = 15) -> None:
    print(f"[idle-cleanup] loop started: every {interval_seconds}s, idle threshold {idle_hours}h", flush=True)

    async def _cleanup_loop() -> None:
        while True:
            try:
                n = await cleanup(idle_hours)
                if n:
                    print(f"[idle-cleanup] stopped {n} idle container(s)", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[idle-cleanup] error: {exc}", flush=True)
            await asyncio.sleep(interval_seconds)

    # Run both loops concurrently — the map loop is cheap and tight,
    # the cleanup loop is heavy and slow.
    await asyncio.gather(
        _cleanup_loop(),
        vnc_map_loop(map_refresh_seconds),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--idle-hours", type=float,
                        default=float(os.environ.get("IDLE_CLEANUP_HOURS", "6")))
    parser.add_argument("--once", action="store_true", help="Run one pass and exit (for cron)")
    parser.add_argument("--interval", type=int,
                        default=int(os.environ.get("IDLE_CLEANUP_INTERVAL", "3600")))
    parser.add_argument("--map-refresh", type=int,
                        default=int(os.environ.get("VNC_MAP_REFRESH_SEC", "15")))
    args = parser.parse_args()
    if args.once:
        n = asyncio.run(cleanup(args.idle_hours))
        print(f"stopped {n}")
    else:
        asyncio.run(loop_forever(args.interval, args.idle_hours, args.map_refresh))


if __name__ == "__main__":
    main()
