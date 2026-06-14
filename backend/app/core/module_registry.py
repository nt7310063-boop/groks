"""Backend module registry — single source of truth for which feature
modules are loaded into the FastAPI app.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable

from fastapi import APIRouter, FastAPI


LifespanHook = Callable[[FastAPI], Awaitable[None]]


@dataclass(frozen=True)
class ModuleManifest:
    name: str
    label: str
    router: APIRouter
    startup: tuple[LifespanHook, ...] = field(default_factory=tuple)
    shutdown: tuple[LifespanHook, ...] = field(default_factory=tuple)
    tags: tuple[str, ...] = field(default_factory=tuple)


def _load_modules() -> list[ModuleManifest]:
    """Import every module's manifest lazily.
    """
    from app.modules.auth import manifest as auth
    from app.modules.auth.api_keys import manifest as api_keys
    from app.modules.admin.notifications import manifest as admin_notifications
    from app.modules.admin.domain_config import manifest as domain_config
    from app.modules.sdk import manifest as sdk
    from app.modules.grok.files import manifest as grok_files
    from app.modules.grok.jobs import manifest as grok_jobs
    from app.modules.grok.profiles import manifest as grok_profiles
    from app.modules.grok.projects import manifest as grok_projects
    from app.modules.prompt_history import manifest as prompt_history
    from app.modules.landing.client_api import manifest as client_api

    return [
        auth,
        api_keys,
        admin_notifications,
        domain_config,
        grok_profiles,
        grok_jobs,
        grok_files,
        grok_projects,
        sdk,
        prompt_history,
        client_api,
    ]


_MODULES: list[ModuleManifest] | None = None


def get_modules() -> list[ModuleManifest]:
    """Memoised accessor so tests can call this multiple times cheaply."""
    global _MODULES
    if _MODULES is None:
        _MODULES = _load_modules()
    return _MODULES


def register_all(app: FastAPI, modules: Iterable[ModuleManifest] | None = None) -> None:
    for m in modules if modules is not None else get_modules():
        app.include_router(m.router)


def collect_startup_hooks(
    modules: Iterable[ModuleManifest] | None = None,
) -> list[LifespanHook]:
    out: list[LifespanHook] = []
    for m in modules if modules is not None else get_modules():
        out.extend(m.startup)
    return out


def collect_shutdown_hooks(
    modules: Iterable[ModuleManifest] | None = None,
) -> list[LifespanHook]:
    out: list[LifespanHook] = []
    for m in modules if modules is not None else get_modules():
        out.extend(m.shutdown)
    return out
