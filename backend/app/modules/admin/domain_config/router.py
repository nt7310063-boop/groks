from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.core.deps import DbSession
from app.models import Domain

router = APIRouter(prefix="/api/domains", tags=["domains"])

class DomainConfig(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    hostname: str
    label: str
    description: str | None = None
    status: str
    allow_landing: bool
    allow_register: bool
    allow_login: bool
    allow_all_pages: bool
    allowed_pages: list[str]
    brand_name: str | None = None
    require_playground_key: bool
    maintenance_mode: bool
    maintenance_message: str | None = None
    maintenance_starts_at: str | None = None
    maintenance_announcement: str | None = None
    login_template: str
    allowed_profile_actions: list[str]


def _fallback_domain(host: str) -> DomainConfig:
    return DomainConfig(
        hostname=host,
        label=host,
        status="active",
        allow_landing=True,
        allow_register=True,
        allow_login=True,
        allow_all_pages=False,
        allowed_pages=[],
        require_playground_key=False,
        maintenance_mode=False,
        login_template="default",
        allowed_profile_actions=["auto_login", "upload_cookies", "stop_vnc", "disable", "delete"],
    )


@router.get("/config", response_model=DomainConfig)
async def get_domain_config(host: str, db: DbSession) -> DomainConfig:
    q = await db.execute(select(Domain).where(Domain.hostname == host))
    domain = q.scalar_one_or_none()
    if domain is None:
        q = await db.execute(select(Domain).where(Domain.hostname == "*"))
        domain = q.scalar_one_or_none()
    if domain is None:
        return _fallback_domain(host)
    return DomainConfig.model_validate(domain)
