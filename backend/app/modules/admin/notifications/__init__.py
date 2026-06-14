"""Admin notifications — in-app notification queue for the header bell."""

from app.core.module_registry import ModuleManifest
from .router import router

manifest = ModuleManifest(
    name="admin_notifications",
    label="Admin notifications",
    router=router,
    tags=("admin", "notifications"),
)
