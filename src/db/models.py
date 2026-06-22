from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(128), default="")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    yandex_login: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    metrika_counter_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    appmetrica_application_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    appmetrica_tracking_id: Mapped[str] = mapped_column(String(64), default="")
    telegram_chat_id: Mapped[str] = mapped_column(String(64), default="")
    max_chat_id: Mapped[str] = mapped_column(String(64), default="")
    spend_alert_threshold: Mapped[float] = mapped_column(Float, default=0.0)
    monthly_budget: Mapped[float] = mapped_column(Float, default=0.0)
    directologist: Mapped[str] = mapped_column(String(32), default="Ксюша")
    attribution_model: Mapped[str] = mapped_column(String(16), default="AUTO")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    goals: Mapped[list[ClientGoal]] = relationship(
        back_populates="client",
        cascade="all, delete-orphan",
    )
    appmetrica_goals: Mapped[list[ClientAppMetricaGoal]] = relationship(
        back_populates="client",
        cascade="all, delete-orphan",
    )


class ClientAppMetricaGoal(Base):
    __tablename__ = "client_appmetrica_goals"
    __table_args__ = (UniqueConstraint("client_id", "event_key", name="uq_client_appmetrica_goal"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"))
    event_key: Mapped[str] = mapped_column(String(256))
    event_label: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default="")  # install | purchase | ""

    client: Mapped[Client] = relationship(back_populates="appmetrica_goals")


class ClientGoal(Base):
    __tablename__ = "client_goals"
    __table_args__ = (UniqueConstraint("client_id", "goal_id", name="uq_client_goal"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"))
    goal_id: Mapped[int] = mapped_column(Integer)
    goal_name: Mapped[str] = mapped_column(String(256))
    is_selected: Mapped[bool] = mapped_column(Boolean, default=False)

    client: Mapped[Client] = relationship(back_populates="goals")
