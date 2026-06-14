import uuid
from datetime import datetime

from fastapi import APIRouter, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select

from app.core.deps import CurrentUser, DbSession
from app.models import Notification

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

class NotificationOut(BaseModel):
    id: uuid.UUID
    kind: str
    title: str
    body: str | None
    target_url: str | None
    severity: str
    read_at: datetime | None
    created_at: datetime

    class Config:
        from_attributes = True

class NotificationListOut(BaseModel):
    items: list[NotificationOut]
    unread_count: int
    total: int

@router.get("", response_model=NotificationListOut)
async def list_notifications(
    user: CurrentUser,
    db: DbSession,
    unread: int | None = Query(default=None, ge=0, le=1),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> NotificationListOut:
    base = select(Notification).where(Notification.user_id == user.id)
    if unread == 1:
        base = base.where(Notification.read_at.is_(None))

    total = (await db.execute(
        select(func.count()).select_from(base.subquery())
    )).scalar_one()
    unread_count = (await db.execute(
        select(func.count()).select_from(
            select(Notification).where(
                Notification.user_id == user.id,
                Notification.read_at.is_(None),
            ).subquery()
        )
    )).scalar_one()

    stmt = base.order_by(Notification.created_at.desc()).offset(offset).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return NotificationListOut(
        items=list(rows),
        unread_count=unread_count,
        total=total,
    )

@router.post("/{notification_id}/read", response_model=NotificationOut)
async def mark_notification_read(
    notification_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
) -> Notification:
    notif = await db.get(Notification, notification_id)
    if notif and notif.user_id == user.id:
        if notif.read_at is None:
            notif.read_at = datetime.utcnow()
            await db.commit()
            await db.refresh(notif)
        return notif
    # Keep behavior simple for now: return 404 if not found or not owned.
    from app.core.exceptions import NotFound
    raise NotFound("notification")

@router.post("/read-all", response_model=dict)
async def mark_all_notifications_read(
    user: CurrentUser,
    db: DbSession,
) -> dict:
    rows = (await db.execute(
        select(Notification).where(
            Notification.user_id == user.id,
            Notification.read_at.is_(None),
        )
    )).scalars().all()
    now = datetime.utcnow()
    for notif in rows:
        notif.read_at = now
    await db.commit()
    return {"updated": len(rows)}
