"""Orbi reflection (journal read with the numbers) and the consultation it can suggest."""

import uuid
from datetime import datetime, timezone

import pytest

API = "/api/v1/app"


def login(client, crew_id, pin):
    body = client.post(f"{API}/auth/login", json={"crewId": crew_id, "pin": pin}).json()
    return {"Authorization": f"Bearer {body['accessToken']}"}


def at_day(day, hour=21):
    """Timestamp on a mission day of Orbit-3 (day 1 = 2026-11-03)."""
    return datetime(2026, 11, 2 + day, hour, 0, tzinfo=timezone.utc)


@pytest.fixture()
def h(client):
    """TEST astronaut with no journal, logs, alerts, readings or consultations."""
    from sqlalchemy import delete

    from app import db
    from app.cosmo.models import (AppAlert, AppChatMessage, AppConfirmation, AppDeviceReading, AppJournalEntry,
                                  AppLog, AppSyncOp)
    from app.models import SupportRequest

    with db.SessionLocal() as s:
        for model in (AppJournalEntry, AppChatMessage, AppLog, AppAlert, AppConfirmation, AppDeviceReading, AppSyncOp,
                      SupportRequest):
            s.execute(delete(model).where(model.astronaut_id == "ast-test"))
        s.commit()
    return login(client, "TEST", "0000")


def add(*rows):
    from app import db

    with db.SessionLocal() as s:
        s.add_all(rows)
        s.commit()


def journal(day, text, mood=None):
    from app.cosmo.models import AppJournalEntry

    return AppJournalEntry(id=f"jrn-{uuid.uuid4().hex[:8]}", astronaut_id="ast-test", timestamp=at_day(day),
                           summary=text, mood_level=mood, source_message_ids=[])


def sleep(day, minutes):
    from app.cosmo.models import AppLog

    return AppLog(id=f"log-{uuid.uuid4().hex[:8]}", astronaut_id="ast-test", type="sleep", timestamp=at_day(day, 7),
                  mission_day=day, coins_earned=0, source="app",
                  data={"durationMin": minutes, "quality": 2, "bedtime": "23:30", "wakeTime": "04:00", "wakeUps": "3+"})


@pytest.fixture(autouse=True)
def ai_off(monkeypatch):
    monkeypatch.setenv("ORBI_AI_ENABLED", "false")


def test_quiet_week_suggests_nothing(client, h):
    r = client.get(f"{API}/me/orbi/reflection", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["journal"] == [] and body["consultation"]["suggested"] is False
    assert body["messageSource"] == "rules" and "journal" in body["narrative"]
    assert "AI_GENERATED" not in r.headers["X-Data-Provenance"]
    assert client.get(f"{API}/me/home", headers=h).json()["consultationSuggestion"]["suggested"] is False


def test_journal_and_numbers_agree_on_sleep_and_orbi_suggests_a_consultation(client, h):
    add(journal(10, "Could not sleep again, woke up three times.", 2), journal(11, "So tired today, slept badly.", 2),
        sleep(10, 260), sleep(11, 250), sleep(12, 270))
    body = client.get(f"{API}/me/orbi/reflection", headers=h).json()
    [link] = [l for l in body["links"] if l["domain"] == "sleep"]
    assert link["agreement"] == "consistent" and link["journalDays"] == [10, 11] and link["level"] == "alert"
    assert "4.3 h" in link["numbers"] and "the numbers agree" in link["text"]
    c = body["consultation"]
    assert c["suggested"] and c["priority"] == "routine" and c["requiresConfirmation"]
    assert "journal_and_numbers" in [r["code"] for r in c["reasons"]] and "flight surgeon" in c["message"]
    assert c["message"] in body["narrative"]
    badge = client.get(f"{API}/me/home", headers=h).json()["consultationSuggestion"]
    assert badge["suggested"] and "journal_and_numbers" in badge["reasons"]


def test_journal_only_and_numbers_only(client, h):
    add(journal(12, "The food tastes the same every day, no appetite."), sleep(11, 250), sleep(12, 260))
    links = {l["domain"]: l for l in client.get(f"{API}/me/orbi/reflection", headers=h).json()["links"]}
    assert links["nutrition"]["agreement"] == "journal_only" and "do not show it yet" in links["nutrition"]["text"]
    assert links["sleep"]["agreement"] == "numbers_only" and links["sleep"]["journalDays"] == []


def test_urgent_words_point_to_a_person_now(client, h):
    add(journal(12, "I had chest pain during the run and I can't breathe well."))
    c = client.get(f"{API}/me/orbi/reflection", headers=h).json()["consultation"]
    assert c["suggested"] and c["priority"] == "urgent" and c["reasons"][0]["code"] == "journal_urgent"
    assert "now" in c["message"] and "crewmate" in c["message"]


def test_requesting_a_consultation_reaches_the_ground_once(client, h):
    add(journal(10, "Could not sleep again."), journal(11, "Slept badly, so tired."), sleep(10, 260), sleep(11, 250))
    assert client.post(f"{API}/me/consultations", headers=h, json={"confirmed": False}).status_code == 422
    r = client.post(f"{API}/me/consultations", headers=h, json={"confirmed": True, "note": "I want to talk about sleep."})
    assert r.status_code == 201, r.text
    made = r.json()
    assert made["suggestedByOrbi"] and made["priority"] == "routine" and "Earth is about 20 minutes" in made["message"]
    again = client.post(f"{API}/me/consultations", headers=h, json={"confirmed": True}).json()
    assert again["requestId"] == made["requestId"]  # one open consultation at a time
    mine = client.get(f"{API}/me/consultations", headers=h).json()
    assert [x["requestId"] for x in mine] == [made["requestId"]]
    ground = login(client, "GROUND", "2026")
    listed = client.get(f"{API}/ground/consultations", headers=ground).json()
    row = next(x for x in listed if x["requestId"] == made["requestId"])
    assert row["name"] == "Test Pilot" and "I want to talk about sleep." in row["message"] and "Orbi's reasons" in row["message"]
    support = client.get("/api/v1/care/support").json()  # surgeon
    assert any(x["id"] == made["requestId"] for x in support)
    view = client.get(f"{API}/me/orbi/reflection", headers=h).json()["consultation"]
    assert view["existingRequest"]["id"] == made["requestId"]
    assert client.get(f"{API}/ground/consultations", headers=h).status_code == 403


def test_consultation_from_the_offline_queue_and_with_idempotency_key(client, h):
    op = {"opId": str(uuid.uuid4()), "type": "consultation.request", "recordedAt": "2026-11-14T20:00:00Z",
          "payload": {"confirmed": True, "note": "Asked with no link."}}
    first = client.post(f"{API}/me/sync", headers=h, json={"ops": [op]}).json()["results"][0]
    again = client.post(f"{API}/me/sync", headers=h, json={"ops": [op]}).json()["results"][0]
    assert first["status"] == "applied" and again["duplicate"] and again["entityId"] == first["entityId"]
    key = {"Idempotency-Key": str(uuid.uuid4())}
    a = client.post(f"{API}/me/consultations", headers=h | key, json={"confirmed": True}).json()
    b = client.post(f"{API}/me/consultations", headers=h | key, json={"confirmed": True}).json()
    assert a == b and str(a["requestId"]) == first["entityId"]
    bundle = client.get(f"{API}/me/offline-bundle", headers=h).json()
    assert "consultation" in bundle["reflection"] and bundle["sync"]["opTypes"][-1] == "consultation.request"


def test_ai_words_the_text_only_when_enabled(client, h, monkeypatch):
    from app.cosmo import reflection

    add(journal(11, "Could not sleep again."), sleep(11, 250))
    sent = {}

    def fake(payload):
        sent.update(payload)
        return "You wrote that sleep was hard, and your logs show short nights too."

    monkeypatch.setattr(reflection, "narrate", fake)
    r = client.get(f"{API}/me/orbi/reflection", headers=h)
    body = r.json()
    assert body["aiGenerated"] and body["messageSource"] == "ai_generated" and body["narrative"].startswith("You wrote")
    assert r.headers["X-Data-Provenance"].endswith("AI_GENERATED")
    assert sent["journal"][0]["summary"] == "Could not sleep again." and "Test" not in str(sent)  # no names sent
    off = client.get(f"{API}/me/orbi/reflection?ai=false", headers=h)
    assert off.json()["aiGenerated"] is False and "AI_GENERATED" not in off.headers["X-Data-Provenance"]


def test_model_answer_with_medical_advice_is_dropped(monkeypatch):
    from app.cosmo import reflection

    monkeypatch.setenv("ORBI_AI_ENABLED", "true")
    monkeypatch.setenv("GROQ_API_KEY", "test")
    assert reflection.ai_enabled()
    monkeypatch.setenv("ORBI_AI_ENABLED", "false")
    assert reflection.narrate({"journal": []}) is None


def test_ground_reads_the_reflection_of_a_crew_member(client):
    ground = login(client, "GROUND", "2026")
    r = client.get(f"{API}/ground/astronauts/ast-01/reflection", headers=ground)
    assert r.status_code == 200 and r.json()["astronautId"] == "ast-01" and r.json()["journal"]
    assert "AI_GENERATED" not in r.headers["X-Data-Provenance"]
    crew = client.get(f"{API}/ground/crew", headers=ground).json()["crew"]
    assert all("consultation" in row for row in crew)
    assert client.get(f"{API}/ground/astronauts/ast-01/reflection", headers=login(client, "ORB3-ANA", "2026")).status_code == 403
