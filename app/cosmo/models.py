"""Tables for the CosmoMedic app layer (`/api/v1/app`).

They follow docs/data/data-model.json of the frontend repository: same ids
(`ast-01`, `log-0001`), same nested objects (stored as JSON) and the same
mission (`orbit-3`). The demo content is loaded from the frontend's mock-db.json.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import UTCDateTime, utcnow


class AppMission(Base):
    __tablename__ = "app_missions"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)  # "orbit-3"
    name: Mapped[str] = mapped_column(String(80))
    destination: Mapped[str] = mapped_column(String(20))
    start_date: Mapped[str] = mapped_column(String(10))  # ISO date
    length_days: Mapped[int] = mapped_column(Integer)
    today: Mapped[int] = mapped_column(Integer)  # mission day used as "today" in the demo
    comm_delay_sec: Mapped[int] = mapped_column(Integer)
    goals: Mapped[dict] = mapped_column(JSON, default=dict)
    environments: Mapped[list] = mapped_column(JSON, default=list)


class AppAstronaut(Base):
    __tablename__ = "app_astronauts"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)  # "ast-01"
    mission_id: Mapped[str] = mapped_column(ForeignKey("app_missions.id"))
    name: Mapped[str] = mapped_column(String(120))
    crew_id: Mapped[str] = mapped_column(String(40), unique=True)  # login id, e.g. ORB3-ANA
    role: Mapped[str] = mapped_column(String(20), default="astronaut")
    profile: Mapped[dict] = mapped_column(JSON, default=dict)
    baseline: Mapped[dict] = mapped_column(JSON, default=dict)
    devices: Mapped[dict] = mapped_column(JSON, default=dict)
    avatar: Mapped[dict] = mapped_column(JSON, default=dict)
    onboarding_done: Mapped[bool] = mapped_column(Boolean, default=False)
    daily_reminders: Mapped[bool] = mapped_column(Boolean, default=True)


class AppLog(Base):
    __tablename__ = "app_logs"
    __table_args__ = (Index("ix_app_logs_astronaut_day", "astronaut_id", "mission_day"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # "log-0001"
    astronaut_id: Mapped[str] = mapped_column(ForeignKey("app_astronauts.id"))
    type: Mapped[str] = mapped_column(String(20))
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime())
    mission_day: Mapped[int] = mapped_column(Integer)
    environment_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    coins_earned: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(10))  # seed | app
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class AppAlert(Base):
    __tablename__ = "app_alerts"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # "alr-01"
    astronaut_id: Mapped[str] = mapped_column(ForeignKey("app_astronauts.id"), index=True)
    type: Mapped[str] = mapped_column(String(20))  # poor_sleep | low_mood | bone_loss | fitness_drop | menu_fatigue | injury
    level: Mapped[str] = mapped_column(String(10))  # low | medium | high
    text: Mapped[str] = mapped_column(Text)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime())
    mission_day: Mapped[int] = mapped_column(Integer)
    sent_to_ground: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(10), default="active")  # active | resolved
    source: Mapped[str] = mapped_column(String(10))  # seed | rules


class AppGroundMessage(Base):
    __tablename__ = "app_ground_messages"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # "gm-001"
    astronaut_id: Mapped[str] = mapped_column(ForeignKey("app_astronauts.id"), index=True)
    sender: Mapped[str] = mapped_column(String(10))  # astronaut | ground  (JSON field "from")
    author: Mapped[str | None] = mapped_column(String(60), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    urgent: Mapped[bool] = mapped_column(Boolean, default=False)
    sent_at: Mapped[datetime] = mapped_column(UTCDateTime())
    estimated_arrival_at: Mapped[datetime] = mapped_column(UTCDateTime())
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    attachments: Mapped[list] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(10))  # seed | app


class AppJournalEntry(Base):
    __tablename__ = "app_journal"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    astronaut_id: Mapped[str] = mapped_column(ForeignKey("app_astronauts.id"), index=True)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime())
    summary: Mapped[str] = mapped_column(Text)
    mood_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_message_ids: Mapped[list] = mapped_column(JSON, default=list)


class AppShopItem(Base):
    __tablename__ = "app_shop_items"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    category: Mapped[str] = mapped_column(String(20))  # helmet | suit | pet | window | wall | floor
    name: Mapped[str] = mapped_column(String(80))
    price: Mapped[int] = mapped_column(Integer)
    asset: Mapped[str] = mapped_column(String(200))


class AppCalendarEvent(Base):
    __tablename__ = "app_calendar"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    astronaut_id: Mapped[str] = mapped_column(ForeignKey("app_astronauts.id"), index=True)
    date: Mapped[datetime] = mapped_column(UTCDateTime())
    title: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))
    linked_screen: Mapped[str | None] = mapped_column(String(40), nullable=True)


class AppEnvironmentReading(Base):
    __tablename__ = "app_environment_readings"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    environment_id: Mapped[str] = mapped_column(String(20), index=True)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime())
    temperature_c: Mapped[float] = mapped_column()
    pressure_kpa: Mapped[float] = mapped_column()
    light_lux: Mapped[float] = mapped_column()
    noise_db: Mapped[float] = mapped_column()
    radiation_usv_h: Mapped[float] = mapped_column()
    crew_present: Mapped[int] = mapped_column(Integer)


class AppChatMessage(Base):
    """Conversation with Orbi. Only "journal" mode is stored; "private" mode saves nothing."""

    __tablename__ = "app_chat_messages"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # "msg-001"
    astronaut_id: Mapped[str] = mapped_column(ForeignKey("app_astronauts.id"), index=True)
    author: Mapped[str] = mapped_column(String(10))  # orbi | astronaut
    text: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime())
    mode: Mapped[str] = mapped_column(String(10), default="journal")
    source: Mapped[str] = mapped_column(String(10), default="app")  # seed | app | rules


class AppFood(Base):
    """Station food catalog, used for meal swaps and to build meal plans. Values per serving."""

    __tablename__ = "app_foods"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # "food-rice"
    name: Mapped[str] = mapped_column(String(120))
    serving_g: Mapped[float] = mapped_column()
    kcal: Mapped[float] = mapped_column()
    protein_g: Mapped[float] = mapped_column()
    carbs_g: Mapped[float] = mapped_column()
    fat_g: Mapped[float] = mapped_column()


class AppPlanMeal(Base):
    """One planned meal of one astronaut on one mission day, set by the medical team."""

    __tablename__ = "app_meal_plan"
    __table_args__ = (Index("ix_app_meal_plan_astronaut_day", "astronaut_id", "mission_day"),)

    id: Mapped[str] = mapped_column(String(60), primary_key=True)  # "pm-ast-01-d12-lunch"
    astronaut_id: Mapped[str] = mapped_column(ForeignKey("app_astronauts.id"))
    mission_day: Mapped[int] = mapped_column(Integer)
    slot: Mapped[str] = mapped_column(String(12))  # breakfast | lunch | dinner | snack
    time: Mapped[str] = mapped_column(String(5))  # "08:00"
    name: Mapped[str] = mapped_column(String(160))
    items: Mapped[list] = mapped_column(JSON, default=list)
    kcal: Mapped[float] = mapped_column()
    protein_g: Mapped[float] = mapped_column()
    carbs_g: Mapped[float] = mapped_column()
    fat_g: Mapped[float] = mapped_column()
