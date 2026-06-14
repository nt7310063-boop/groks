"""Public domain config endpoint used by the frontend store."""

from app.core.module_registry import ModuleManifest
from .router import router

manifest = ModuleManifest(
    name="domain_config",
    label="Domain config",
    router=router,
    tags=("domains",),
)
