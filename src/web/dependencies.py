from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from src.config import Settings
from src.db.database import get_db
from src.db.models import User

TEMPLATES_DIR = __import__("pathlib").Path(__file__).resolve().parent / "templates"
STATIC_DIR = __import__("pathlib").Path(__file__).resolve().parent / "static"


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    user = db.get(User, user_id)
    if not user:
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user
