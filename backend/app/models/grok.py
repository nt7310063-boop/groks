"""Grok-automation models — Profile, GrokProject, Job, JobLog, File + assignments."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ._base import Base, JSONType, TimestampMixin, UUIDType, _uuid


class Profile(Base, TimestampMixin):
    __tablename__ = "profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(UUIDType, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    profile_path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="created")
    encrypted_cookie: Mapped[str | None] = mapped_column(Text)
    encrypted_storage_state: Mapped[str | None] = mapped_column(Text)
    last_login_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    # Multi-tab support: each profile can run N jobs in parallel via separate
    # Chromium tabs. Counter is atomically incremented when worker claims a slot.
    active_jobs: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_concurrent_jobs: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # Image jobs go through the lightweight HTTP API path, so they scale
    # with `max_concurrent_jobs` (one tab per slot is fine).
    # Video jobs still drive Playwright DOM heavily — opening more than
    # ~4 video tabs in the same Chromium reliably crashes it
    # (TargetClosedError). Cap them separately so admins can leave
    # `max_concurrent_jobs` at 12 for image throughput without melting
    # the browser on video. Default 4 mirrors what works empirically.
    max_concurrent_video: Mapped[int] = mapped_column(Integer, nullable=False, default=4, server_default="4")
    # Subset of `active_jobs` that is video. We need this as a separate
    # counter so the slot-acquire UPDATE can enforce both caps atomically
    # without re-querying running jobs.
    active_video_jobs: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # Image-only toggle. When False, the resolver skips this profile for
    # video jobs entirely (regardless of `max_concurrent_video`). Useful
    # for Free-tier Grok accounts that have no video quota, or for
    # dedicating a profile to image-only throughput. Default True keeps
    # pre-0027 behavior — every profile accepts both job types.
    allows_video: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    # Account tier label — free-text VARCHAR so adding 'pro' / 'enterprise'
    # later doesn't need a migration. Common values: "free", "heavy" (=
    # SuperGrok Premium with ~500/day quota), "pro". Used by the admin UI
    # for filtering + badges; does NOT change worker routing.
    tier: Mapped[str] = mapped_column(
        String(20), nullable=False, default="free", server_default="free",
    )

    user: Mapped["User"] = relationship(back_populates="profiles")  # noqa: F821
    jobs: Mapped[list["Job"]] = relationship(back_populates="profile")


class GrokProject(Base, TimestampMixin):
    """A 'project' inside a single Grok account (= one Profile).

    Grok's web UI lets you keep chat history / presets / brand voice
    separated by project. We mirror that as a row here: one Profile
    (browser session) can hold N projects. Each project gets assigned to
    specific tenant domain(s) via ProjectDomainAssignment, so the same
    Profile can serve multiple customers without their data bleeding
    into each other's workspace.

    Identification:
      - `grok_project_id` = the slug Grok uses in its URL
        (https://grok.com/project/<slug>). Worker navigates here before
        each prompt submit.
      - `name` is the human label super_admin sets in our UI.
    """
    __tablename__ = "grok_projects"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("profiles.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    grok_project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)


class ProjectUserAssignment(Base):
    """Per-user assignment of a GrokProject.

    Lets super_admin pin a specific tenant user to a specific project so
    each customer gets their own chat history/preset even when sharing a
    Grok account with other tenants in the same domain. Takes priority
    over the domain-level assignment when both exist.
    """
    __tablename__ = "project_user_assignments"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("grok_projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # Soft-disable. When False the resolver skips this assignment as if
    # the row didn't exist — lets super_admin suspend a user without
    # destroying the assignment row (and its created_at audit trail).
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class ProjectDomainAssignment(Base):
    """Many-to-many: which GrokProject each customer domain can pull from.

    Replaces the old ProfileDomainAssignment — granularity moved one level
    down so a single Grok account can serve multiple tenants in parallel
    via separate projects. Migration 0020 drops the legacy table.

    Per-user pinning is in ProjectUserAssignment and takes priority over
    this domain-wide rule when both apply.

    Only `super_admin` edits these. Per-domain `admin` can read their
    own set; `user` doesn't see this surface.
    """
    __tablename__ = "project_domain_assignments"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("grok_projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    domain_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("domains.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # Soft-disable. When False the resolver and visibility queries treat
    # this row as if it doesn't exist. Used by super_admin to temporarily
    # revoke a tenant's access without losing the assignment config.
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class ProjectToolInstallAssignment(Base):
    """Many-to-many: which GrokProject each tool install can pull from.

    Sibling of ProjectDomainAssignment, but keyed by tool_install_id. Used
    when a job request comes in via the desktop client (X-Tool-Install-Id
    header). Resolver tries this table first for an exact install match;
    falls back to ProjectDomainAssignment if no row exists.

    Only `super_admin` edits these.
    """
    __tablename__ = "project_tool_install_assignments"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("grok_projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tool_install_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("tool_installs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class Job(Base, TimestampMixin):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(UUIDType, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, ForeignKey("api_keys.id", ondelete="SET NULL"))
    profile_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, ForeignKey("profiles.id", ondelete="SET NULL"))
    # When the auto-pick scoped to a project assignment, we record it here
    # so the worker can navigate to grok.com/project/<grok_project_id>
    # before submitting the prompt. NULL for legacy jobs / non-project runs.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("grok_projects.id", ondelete="SET NULL"), index=True,
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    job_type: Mapped[str] = mapped_column(String(50), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    input_payload: Mapped[dict | None] = mapped_column(JSONType)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retry: Mapped[int] = mapped_column(Integer, default=2)
    result_file_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType)
    result_url: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When set in the future, the worker skips this job until that time —
    # used to enforce retry backoff without blocking the worker loop.
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    # User-facing organisation. Both shipped in 0045 — see migration for
    # the GIN index choice. Default-empty so existing rows stay queryable.
    is_favorite: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tags: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)

    user: Mapped["User"] = relationship(back_populates="jobs")  # noqa: F821
    profile: Mapped[Profile | None] = relationship(back_populates="jobs")
    logs: Mapped[list["JobLog"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class JobLog(Base):
    __tablename__ = "job_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    job_id: Mapped[uuid.UUID] = mapped_column(UUIDType, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(50), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    job: Mapped[Job] = relationship(back_populates="logs")


class File(Base):
    __tablename__ = "files"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(UUIDType, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, ForeignKey("jobs.id", ondelete="SET NULL"))
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[str] = mapped_column(String(50), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(100))
    storage_driver: Mapped[str] = mapped_column(String(50), nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    public_url: Mapped[str | None] = mapped_column(Text)
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
