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
    from app.models import AstronautPreference, SupportRequest

    with db.SessionLocal() as s:
        for model in (AppJournalEntry, AppChatMessage, AppLog, AppAlert, AppConfirmation, AppDeviceReading, AppSyncOp,
                      SupportRequest, AstronautPreference):
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


# --- Orbi sends the suggestion to the astronaut's chat ------------------------------------------------


def orbi_consult_messages(client, h):
    return [m for m in client.get(f"{API}/me/orbi/messages", headers=h).json() if m["author"] == "orbi"
            and ("flight surgeon" in m["text"] or "consultation" in m["text"])]


def short_night(client, h):
    r = client.post(f"{API}/me/logs", headers=h, json={"type": "sleep", "environmentId": "sleep", "data": {
        "bedtime": "2026-11-13T23:40:00Z", "wakeTime": "2026-11-14T04:00:00Z", "quality": 2, "wakeUps": "3+"}})
    assert r.status_code == 201, r.text


def test_orbi_writes_the_suggestion_in_the_chat_once(client, h):
    add(journal(10, "Could not sleep again."), journal(11, "Slept badly, so tired."), sleep(10, 260), sleep(11, 250))
    short_night(client, h)
    [msg] = orbi_consult_messages(client, h)
    assert msg["text"].startswith("Test, I noticed something: you wrote about sleep on 2 day(s), and the numbers agree")
    assert 'Reply "yes"' in msg["text"] and msg["mode"] == "journal"
    snap = client.get(f"{API}/snapshot", headers=h).json()
    assert any(m["id"] == msg["id"] for m in snap["ai_chat"]["messages"])  # the app shows it in the Orbi tab
    client.post(f"{API}/me/logs", headers=h, json={"type": "mood", "data": {"level": 2}})
    assert len(orbi_consult_messages(client, h)) == 1  # same reasons: not sent again


def test_yes_in_the_chat_requests_the_consultation(client, h):
    add(journal(10, "Could not sleep again."), journal(11, "Slept badly, so tired."), sleep(10, 260), sleep(11, 250))
    short_night(client, h)
    r = client.post(f"{API}/me/orbi/chat", headers=h, json={"text": "Yes please", "mode": "journal"}).json()
    done = r["messages"][-1]
    assert done["author"] == "orbi" and done["text"].startswith("Done, Test. I asked Mission Control")
    [req] = client.get(f"{API}/me/consultations", headers=h).json()
    ground = client.get(f"{API}/ground/consultations", headers=login(client, "GROUND", "2026")).json()
    row = next(x for x in ground if x["requestId"] == req["requestId"])
    assert "Asked in the Orbi chat: Yes please" in row["message"]
    client.post(f"{API}/me/orbi/chat", headers=h, json={"text": "yes", "mode": "journal"})
    assert len(client.get(f"{API}/me/consultations", headers=h).json()) == 1  # nothing new while it is open


def test_yes_from_the_offline_queue_requests_the_consultation(client, h):
    add(journal(10, "Could not sleep again."), journal(11, "Slept badly, so tired."), sleep(10, 260), sleep(11, 250))
    short_night(client, h)
    op = {"opId": str(uuid.uuid4()), "type": "chat.save", "payload": {"text": "ok, request a consultation",
                                                                       "reply": "Sure."}}
    res = client.post(f"{API}/me/sync", headers=h, json={"ops": [op]}).json()["results"][0]
    assert res["status"] == "applied" and res["result"]["consultationRequested"] is True
    assert len(client.get(f"{API}/me/consultations", headers=h).json()) == 1


def test_no_suggestion_no_request_from_a_plain_yes(client, h):
    client.post(f"{API}/me/orbi/chat", headers=h, json={"text": "yes", "mode": "journal"})
    assert client.get(f"{API}/me/consultations", headers=h).json() == []


def test_opt_out_stops_routine_messages_but_not_urgent_ones(client, h):
    assert client.put(f"{API}/me/preferences", headers=h, json={"proactiveMessages": False}).status_code == 200
    add(journal(10, "Could not sleep again."), journal(11, "Slept badly, so tired."), sleep(10, 260), sleep(11, 250))
    short_night(client, h)
    assert orbi_consult_messages(client, h) == []
    r = client.post(f"{API}/me/orbi/chat", headers=h, json={"text": "I have chest pain and I can't breathe", "mode": "journal"})
    texts = [m["text"] for m in r.json()["messages"] if m["author"] == "orbi"]
    assert any("talk to your flight surgeon or a crewmate now" in t for t in texts)


# --- Orbi chat: greetings, negation, safety first, model reply ------------------------------------------


def chat(client, h, text, mode="journal", key=None):
    r = client.post(f"{API}/me/orbi/chat", headers=h | ({"Idempotency-Key": key} if key else {}), json={"text": text, "mode": mode})
    assert r.status_code == 200, r.text
    return r


def test_rules_greet_back_and_read_negation(client, h):
    for hello in ("hi", "Good morning Orbi!", "oi, tudo bem?"):
        body = chat(client, h, hello).json()
        assert body["topic"] == "greeting" and body["reply"].split(",")[0] in ("Good morning", "Good afternoon", "Good evening")
        assert "Test!" in body["reply"] and body["aiGenerated"] is False
    assert chat(client, h, "I'm not feeling well today").json()["topic"] == "sad"
    assert chat(client, h, "I feel great after the sweat session").json()["topic"] == "good"


def test_model_answers_with_context_and_without_the_name(client, h, monkeypatch):
    from app.cosmo import llm

    seen = {}

    def fake(system, payload, max_words, cache=True, reasoning="low"):
        seen.update(payload)
        return "Good evening, {name}! Your sleep looked short this week. How did today go?"

    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "complete", fake)
    add(sleep(11, 250))
    r = chat(client, h, "hey Orbi, long day")
    body = r.json()
    assert body["reply"] == "Good evening, Test! Your sleep looked short this week. How did today go?"
    assert body["aiGenerated"] and body["messages"][1]["provenance"] == "AI_GENERATED"
    assert r.headers["X-Data-Provenance"].endswith("AI_GENERATED")
    assert seen["message"] == "hey Orbi, long day" and "sleep" in seen["today"]
    assert seen["conversation"][-1]["text"] == "hey Orbi, long day" and "Test Pilot" not in str(seen)


def test_danger_never_goes_to_the_model(client, h, monkeypatch):
    from app.cosmo import llm

    calls = []
    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "complete", lambda *a, **k: calls.append(a) or "model text")
    for text, topic in [("I don't want to live anymore", "crisis"), ("I have chest pain and I can't breathe", "urgent")]:
        body = chat(client, h, text).json()
        assert body["topic"] == topic and body["aiGenerated"] is False and "crewmate" in body["reply"]
    assert calls == []
    private = chat(client, h, "just between us, rough day", mode="private").json()
    assert private["saved"] is False and private["messages"] == [] and calls == []


def test_retry_and_offline_copy_save_the_exchange_once(client, h):
    key = str(uuid.uuid4())
    a = chat(client, h, "Hello again", key=key).json()
    b = chat(client, h, "Hello again", key=key).json()
    assert a == b
    res = client.post(f"{API}/me/sync", headers=h, json={"ops": [{"opId": key, "type": "chat.save",
                                                                  "payload": {"text": "Hello again", "reply": "Hi!"}}]}).json()
    assert res["results"][0]["duplicate"] is True
    mine = [m for m in client.get(f"{API}/me/orbi/messages", headers=h).json() if m["text"] == "Hello again"]
    assert len(mine) == 1


def test_off_limits_model_answers_are_dropped():
    from app.cosmo import llm

    for text in ("You should take 200 mg of ibuprofen.", "This looks like a diagnosis of insomnia.", "You have a sleep disorder."):
        assert llm.OFF_LIMITS.search(text)
    assert not llm.OFF_LIMITS.search("Talking to your flight surgeon could help.")


def test_model_reply_that_takes_credit_for_actions_falls_back_to_the_rules(client, h, monkeypatch):
    from app.cosmo import llm

    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "complete", lambda *a, **k: "Good evening, {name}. I've logged the request for a consultation.")
    body = chat(client, h, "hi").json()
    assert body["aiGenerated"] is False and "logged the request" not in body["reply"]
