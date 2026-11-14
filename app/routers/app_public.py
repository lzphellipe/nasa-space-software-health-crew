"""CosmoMedic app: login with Crew ID + PIN, mission, shop and leaderboard."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.auth.security import create_access_token, verify_password
from app.cosmo import logic, views
from app.cosmo.models import AppAstronaut, AppShopItem
from app.db import get_db
from app.models import User
from app.schemas import APIModel

router = APIRouter(prefix="/api/v1/app", tags=["app: public"])


class CrewLoginIn(APIModel):
    crew_id: str = Field(min_length=1, max_length=40, examples=["ORB3-ANA"])
    pin: str = Field(min_length=1, max_length=20, examples=["2026"])


@router.post("/auth/login", summary="Sign in with Crew ID + PIN")
def crew_login(body: CrewLoginIn, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == body.crew_id.strip().lower()))
    if user is None or not user.active or not verify_password(body.pin, user.password_hash):
        raise HTTPException(401, "Invalid Crew ID or PIN")
    astronaut = db.get(AppAstronaut, user.astronaut_id) if user.astronaut_id else None
    token, expires = create_access_token(user.username, user.role, user.astronaut_id)
    return {"accessToken": token, "tokenType": "bearer", "expiresIn": expires,
            "astronaut": {"id": astronaut.id, "name": astronaut.name, "crewId": astronaut.crew_id,
                          "onboardingDone": astronaut.onboarding_done} if astronaut else None,
            "next": "/home" if astronaut and astronaut.onboarding_done else "/onboarding"}


@router.get("/mission", summary="Mission, goals and habitat modules")
def get_mission(db: Session = Depends(get_db)):
    m = logic.mission(db)
    if m is None:
        raise HTTPException(503, "CosmoMedic data not loaded")
    return views.mission_out(m, logic.today(db, m))


@router.get("/shop", summary="Shop items")
def shop(category: Literal["helmet", "suit", "pet", "window", "wall", "floor"] | None = None, db: Session = Depends(get_db)):
    stmt = select(AppShopItem).order_by(AppShopItem.category, AppShopItem.price)
    if category:
        stmt = stmt.where(AppShopItem.category == category)
    return [{"id": i.id, "category": i.category, "name": i.name, "price": i.price, "asset": i.asset}
            for i in db.scalars(stmt)]


@router.get("/leaderboard", summary="Ranking by number of logs or by coins")
def leaderboard(by: Literal["logs", "coins"] = "logs", user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return {"by": by, "note": "Daily logs: one point per saved log. Coins: +10 per log, minus purchases.",
            "ranking": views.leaderboard(db, by, user.astronaut_id)}
