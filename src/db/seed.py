from __future__ import annotations

from sqlalchemy.orm import Session

from src.auth import hash_password
from src.config import Settings
from src.db.models import User


def ensure_admin_user(db: Session, settings: Settings) -> None:
    existing = db.query(User).filter(User.username == settings.admin_username).first()
    if existing:
        return
    db.add(
        User(
            username=settings.admin_username,
            password_hash=hash_password(settings.admin_password),
            display_name="Администратор",
            is_admin=True,
        )
    )
    db.commit()
