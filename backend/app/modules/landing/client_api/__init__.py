"""Client API — partner-facing GrokService contract.

External integrations (notably the Gateway's grok provider) call into
`/api/client/generate` + `/api/client/tasks/{id}/status`. These routes
are a thin adapter on top of the existing Grok job pipeline so the
caller's payload shape (`target`, `ratio`, `count`, …) doesn't have to
match the internal `/v1/jobs/*` schema.
"""

from app.core.module_registry import ModuleManifest
from .router import router

manifest = ModuleManifest(
    name="client_api",
    label="Client API (GrokService contract)",
    router=router,
    tags=("public",),
)
