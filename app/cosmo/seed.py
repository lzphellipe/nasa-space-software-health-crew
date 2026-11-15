"""Load the CosmoMedic demo data (the frontend's docs/data/mock-db.json) into the database."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.security import hash_password
from app.config import SEED_DIR, Settings
from app.cosmo import logic, nutrition, telemetry
from app.cosmo.models import (
    AppAlert,
    AppAstronaut,
    AppCalendarEvent,
    AppChatMessage,
    AppDeviceReading,
    AppFood,
    AppPlanMeal,
    AppEnvironmentReading,
    AppGroundMessage,
    AppJournalEntry,
    AppLog,
    AppMission,
    AppShopItem,
)
from app.cosmo.service import maybe_note, sync_alerts
from app.models import User

log = logging.getLogger(__name__)
MOCK_DB = SEED_DIR / "cosmomedic" / "mock-db.json"


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def seed_if_empty(session: Session, settings: Settings) -> bool:
    if session.scalar(select(func.count()).select_from(AppMission)):
        return False
    db = json.loads(MOCK_DB.read_text(encoding="utf-8"))
    m = db["mission"]
    mission = AppMission(
        id=m["id"], name=m["name"], destination=m["destination"], start_date=m["startDate"],
        length_days=m["lengthDays"], today=m["today"], comm_delay_sec=m["commDelaySec"],
        goals=m["goals"], environments=m["environments"],
    )
    session.add(mission)
    session.flush()

    for a in db["astronauts"]:
        session.add(AppAstronaut(
            id=a["id"], mission_id=mission.id, name=a["name"], crew_id=a["crewId"], role=a.get("role", "astronaut"),
            profile=a["profile"], baseline=a["baseline"], devices=a["devices"], avatar=a["avatar"],
            onboarding_done=a.get("onboardingDone", False),
        ))
        username = a["crewId"].lower()
        if session.scalar(select(User).where(User.username == username)) is None:
            session.add(User(username=username, display_name=a["name"], role="astronaut",
                             astronaut_id=a["id"], password_hash=hash_password(settings.app_demo_pin)))
    session.flush()

    for l in db["logs"]:
        session.add(AppLog(
            id=l["id"], astronaut_id=l["astronautId"], type=l["type"], timestamp=ts(l["timestamp"]),
            mission_day=l["missionDay"], environment_id=l.get("environmentId"), note=l.get("note"),
            coins_earned=l.get("coinsEarned", 0), data=l["data"], source="seed",
        ))
    start = datetime.fromisoformat(mission.start_date)
    for a in db["ai_chat"]["alerts"]:
        stamp = ts(a["timestamp"])
        session.add(AppAlert(
            id=a["id"], astronaut_id=a["astronautId"], type=a["type"], level=a["level"], text=a["text"],
            evidence=a["evidence"], timestamp=stamp, mission_day=(stamp.date() - start.date()).days + 1,
            sent_to_ground=a["sentToGround"], status="active", source="seed",
        ))
    for j in db["ai_chat"]["journal"]:
        session.add(AppJournalEntry(
            id=j["id"], astronaut_id=j["astronautId"], timestamp=ts(j["timestamp"]), summary=j["summary"],
            mood_level=j.get("moodLevel"), source_message_ids=j.get("sourceMessageIds", []),
        ))
    for g in db["ground_messages"]:
        sent, eta = ts(g["sentAt"]), ts(g["estimatedArrivalAt"])
        session.add(AppGroundMessage(
            id=g["id"], astronaut_id=g["astronautId"], sender=g["from"], text=g["text"], urgent=g["urgent"],
            sent_at=sent, estimated_arrival_at=eta, read_at=eta + timedelta(minutes=5) if g["status"] == "read" else None,
            attachments=g.get("attachments", []), source="seed",
        ))
    for s in db["shop"]:
        session.add(AppShopItem(id=s["id"], category=s["category"], name=s["name"], price=s["price"], asset=s["asset"]))
    for c in db["calendar"]:
        session.add(AppCalendarEvent(id=c["id"], astronaut_id=c["astronautId"], date=ts(c["date"]), title=c["title"],
                                     kind=c["kind"], linked_screen=c.get("linkedScreen")))
    for e in db["environment_readings"]:
        session.add(AppEnvironmentReading(
            id=e["id"], environment_id=e["environmentId"], timestamp=ts(e["timestamp"]),
            temperature_c=e["temperatureC"], pressure_kpa=e["pressureKpa"], light_lux=e["lightLux"],
            noise_db=e["noiseDb"], radiation_usv_h=e["radiationUsvH"], crew_present=e["crewPresent"],
        ))
    session.flush()

    # Apply the rules for today on top of the demo alerts (without calling the webhook).
    day = logic.today(session, mission)
    for astronaut in session.scalars(select(AppAstronaut)):
        sync_alerts(session, astronaut, day, send=False)
        active = list(session.scalars(select(AppAlert).where(
            AppAlert.astronaut_id == astronaut.id, AppAlert.status == "active")))
        maybe_note(session, astronaut, day, active)
    session.commit()
    log.info("Loaded the CosmoMedic demo data (%s, %d logs)", mission.id, len(db["logs"]))
    return True


# --- data added after the first release: applied on every start, only where missing --------------

EXTRA_WINDOWS = [
    ("window-beach", "Beach", 400),
    ("window-field", "Countryside", 350),
    ("window-mountains", "Mountains", 450),
    ("window-city", "City lights", 500),
]
TEST_ASTRONAUT_ID = "ast-test"
TEST_CREW_ID = "TEST"
TEST_COINS = 999_999
GROUND_CREW_ID = "GROUND"


def _ensure_user(session: Session, username: str, name: str, role: str, astronaut_id: str | None, pin: str) -> None:
    if session.scalar(select(User).where(User.username == username)) is None:
        session.add(User(username=username, display_name=name, role=role, astronaut_id=astronaut_id,
                         password_hash=hash_password(pin)))


def ensure_extras(session: Session, settings: Settings) -> None:
    """Idempotent: shop windows, TEST astronaut, GROUND user, Orbi chat history, food catalog, meal plans."""
    mission = logic.mission(session)
    if mission is None:
        return
    db = json.loads(MOCK_DB.read_text(encoding="utf-8"))

    for item_id, name, price in EXTRA_WINDOWS:
        if session.get(AppShopItem, item_id) is None:
            session.add(AppShopItem(id=item_id, category="window", name=name, price=price,
                                    asset=f"assets/shop/{item_id}.png"))

    # Test account: every shop item is affordable, onboarding already done. Kept out of the crew table and ranking.
    if session.get(AppAstronaut, TEST_ASTRONAUT_ID) is None:
        template = db["astronauts"][0]
        session.add(AppAstronaut(
            id=TEST_ASTRONAUT_ID, mission_id=mission.id, name="Test Pilot", crew_id=TEST_CREW_ID, role="test",
            profile=template["profile"], baseline=template["baseline"], devices=template["devices"],
            avatar={**template["avatar"], "name": "Bolt", "coins": TEST_COINS}, onboarding_done=True,
        ))
        session.flush()
    _ensure_user(session, TEST_CREW_ID.lower(), "Test Pilot", "astronaut", TEST_ASTRONAUT_ID, settings.app_test_pin)
    # Mission Control account for the ground dashboard, through the same Crew ID + PIN login.
    _ensure_user(session, GROUND_CREW_ID.lower(), "Mission Control", "medical", None, settings.app_ground_pin)

    if not session.scalar(select(func.count()).select_from(AppChatMessage)):
        for msg in db["ai_chat"].get("messages", []):
            session.add(AppChatMessage(id=msg["id"], astronaut_id=msg["astronautId"], author=msg["author"],
                                       text=msg["text"], timestamp=ts(msg["timestamp"]),
                                       mode=msg.get("mode", "journal"), source="seed"))

    for food_id, name, serving, kcal, protein, carbs, fat in nutrition.FOODS:
        if session.get(AppFood, food_id) is None:
            session.add(AppFood(id=food_id, name=name, serving_g=serving, kcal=kcal, protein_g=protein,
                                carbs_g=carbs, fat_g=fat))
    session.flush()

    # Simulated device sync (last 24 h) so the Health Overview has data; real devices replace it.
    if not session.scalar(select(func.count()).select_from(AppDeviceReading)):
        day = logic.today(session, mission)
        now = datetime.now(timezone.utc)
        for astronaut in session.scalars(select(AppAstronaut).where(AppAstronaut.role != "test")):
            for device, items in telemetry.simulate(astronaut, day, now).items():
                telemetry.ingest(session, astronaut, day, "simulator", device, items, simulated=True)

    for astronaut in session.scalars(select(AppAstronaut)):
        has_plan = session.scalar(select(func.count()).select_from(AppPlanMeal)
                                  .where(AppPlanMeal.astronaut_id == astronaut.id))
        if not has_plan:
            nutrition.seed_default_plan(session, astronaut.id, 1, mission.length_days)
    session.commit()
