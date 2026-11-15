"""Offline support: local-copy bundle, queue upload applied at most once, changes since a cursor."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

API = "/api/v1/app"


def login(client, crew_id, pin):
    body = client.post(f"{API}/auth/login", json={"crewId": crew_id, "pin": pin}).json()
    return {"Authorization": f"Bearer {body['accessToken']}"}


@pytest.fixture()
def h(client):
    """The TEST astronaut with no offline operations, readings or confirmations, and no logs for days 11-12."""
    from sqlalchemy import delete

    from app import db
    from app.cosmo.models import AppConfirmation, AppDeviceReading, AppLog, AppSyncOp, AppSyncState

    with db.SessionLocal() as s:
        for model in (AppConfirmation, AppDeviceReading, AppSyncOp, AppSyncState):
            s.execute(delete(model).where(model.astronaut_id == "ast-test"))
        s.execute(delete(AppLog).where(AppLog.astronaut_id == "ast-test", AppLog.mission_day >= 11))
        s.commit()
    return login(client, "TEST", "0000")


def ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat().replace("+00:00", "Z")


def op(type_, payload=None, **kw):
    return {"opId": str(uuid.uuid4()), "type": type_, "payload": payload or {}, **kw}


def sync(client, h, ops, **kw):
    r = client.post(f"{API}/me/sync", headers=h, json={"ops": ops, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def test_bundle_has_what_the_app_needs_with_no_link(client, h):
    b = client.get(f"{API}/me/offline-bundle", headers=h).json()
    for key in ("cursor", "profile", "home", "status", "summaries", "logs", "alerts", "confirmations",
                "groundMessages", "mealPlan", "foods", "rules", "orbi", "sync", "preferences"):
        assert key in b, key
    assert b["missionDay"] == 12 and len(b["mealPlan"]) == 3 and len(b["foods"]) == 24
    assert b["sync"]["opTypes"][0] == "log.create"
    # the device checks in data form agree with the checks the server runs
    from app.cosmo import telemetry

    checks = {c["metric"]: c for c in b["rules"]["deviceChecks"]}
    assert set(checks) == set(telemetry.CHECKS)
    assert checks["spo2"] == checks["spo2"] | {"op": "<", "threshold": 92, "urgent": True, "unit": "%"}
    assert telemetry.CHECKS["spo2"][0](91.9) and not telemetry.CHECKS["spo2"][0](92)
    assert telemetry.CHECKS["body_temperature"][0](37.8) and not telemetry.CHECKS["body_temperature"][0](37.7)
    assert b["rules"]["score"]["countedLogTypes"] == ["mood", "sleep", "radiation", "measurement", "meal", "exercise"]
    # Orbi's replies travel with the bundle; crisis and urgent ones say there is no link and to tell a crewmate
    topics = {t["topic"]: t for t in b["orbi"]["topics"]}
    assert topics["crisis"]["needsHuman"] and "crewmate" in topics["crisis"]["offlineNote"]
    assert topics["sleep"]["offlineNote"] is None and topics["sleep"]["reply"]
    assert b["orbi"]["fallback"]


def test_queue_is_applied_once_and_resending_is_safe(client, h):
    coins = client.get(f"{API}/me/home", headers=h).json()["coins"]
    mood = op("log.create", {"type": "mood", "data": {"level": 2, "tags": ["tired"]}}, recordedAt=ago(hours=20), missionDay=11)
    exercise = op("log.create", {"type": "exercise", "environmentId": "lab", "data": {
        "modality": "aerobic", "equipment": "treadmill", "durationMin": 40, "effort": 3}}, recordedAt=ago(hours=3))
    edit = op("log.update", {"type": "mood", "data": {"level": 3}}, targetId=f"op:{mood['opId']}", recordedAt=ago(hours=19))
    message = op("message.send", {"text": "Felt off yesterday, better now.", "attachments": [f"op:{mood['opId']}"]},
                 recordedAt=ago(hours=2))
    batch = [mood, exercise, edit, message]
    first = sync(client, h, batch, deviceId="phone-1", queuedRemaining=0, clientTime=ago(seconds=0))
    assert first["summary"] == {"received": 4, "applied": 4, "duplicates": 0, "rejected": 0, "conflicts": 0}
    r_mood, r_ex, r_edit, r_msg = first["results"]
    assert r_mood["result"]["log"]["missionDay"] == 11 and r_mood["result"]["loggedToday"] is False
    assert r_mood["lagSec"] >= 19 * 3600 and r_mood["result"]["log"]["timestamp"].startswith(mood["recordedAt"][:16])
    assert r_ex["result"]["log"]["missionDay"] == 12 and r_ex["result"]["loggedToday"] is True
    assert r_edit["entityId"] == r_mood["entityId"] and r_edit["result"]["log"]["data"]["level"] == 3
    assert r_msg["result"]["attachments"] == [r_mood["entityId"]] and r_msg["result"]["status"] == "in_transit"
    assert abs(first["clockSkewSec"]) < 5
    home = first["changes"]["home"]
    assert home["coins"] == coins + 20

    # the answer was lost: the app sends the same batch again
    again = sync(client, h, batch)
    assert again["summary"] == {"received": 4, "applied": 0, "duplicates": 4, "rejected": 0, "conflicts": 0}
    assert [r["entityId"] for r in again["results"]] == [r["entityId"] for r in first["results"]]
    assert again["results"][0]["result"] == r_mood["result"]
    assert again["changes"]["home"]["coins"] == coins + 20
    logs = client.get(f"{API}/me/logs?fromDay=11", headers=h).json()
    assert sorted((l["type"], l["missionDay"]) for l in logs) == [("exercise", 12), ("mood", 11)]
    assert logs[0]["receivedAt"] > logs[0]["timestamp"]
    thread = client.get(f"{API}/me/ground-messages", headers=h).json()["messages"]
    assert sum(m["text"].startswith("Felt off") for m in thread) == 1


def test_refused_operations_get_the_same_answer_again(client, h):
    bad = op("log.create", {"type": "mood", "data": {"level": 9}})
    future = op("log.create", {"type": "mood", "data": {"level": 3}}, missionDay=13)
    orphan = op("log.update", {"type": "mood", "data": {"level": 3}}, targetId="op:missing-operation")
    good = op("log.create", {"type": "mood", "data": {"level": 4}}, recordedAt=ago(days=-1))  # device clock ahead
    out = sync(client, h, [bad, future, orphan, good])
    assert [r["status"] for r in out["results"]] == ["rejected", "rejected", "rejected", "applied"]
    assert out["results"][0]["httpStatus"] == 422 and "level" in str(out["results"][0]["error"])
    assert "after today" in out["results"][1]["error"]["detail"]
    assert out["clockAdjustedOps"] == 1 and out["results"][3]["lagSec"] == 0
    again = sync(client, h, [bad])
    assert again["results"][0] == out["results"][0] | {"duplicate": True}
    # an op id cannot be reused for something else
    reused = sync(client, h, [{**good, "type": "message.send", "payload": {"text": "hi"}}])
    assert reused["results"][0]["status"] == "rejected" and "already used" in reused["results"][0]["error"]["detail"]


def test_editing_a_log_that_changed_on_the_server_is_a_conflict(client, h):
    saved = client.post(f"{API}/me/logs", headers=h, json={"type": "mood", "data": {"level": 3}}).json()["log"]
    assert saved["updatedAt"]
    client.put(f"{API}/me/logs/{saved['id']}", headers=h, json={"type": "mood", "data": {"level": 4}})
    stale = op("log.update", {"type": "mood", "data": {"level": 1}}, targetId=saved["id"], baseUpdatedAt=saved["updatedAt"])
    out = sync(client, h, [stale])
    [res] = out["results"]
    assert res["status"] == "conflict" and res["httpStatus"] == 409 and res["result"]["data"]["level"] == 4
    assert out["summary"]["conflicts"] == 1
    current = client.get(f"{API}/me/logs?fromDay=12&type=mood", headers=h).json()[0]
    fresh = op("log.update", {"type": "mood", "data": {"level": 1}}, targetId=saved["id"], baseUpdatedAt=current["updatedAt"])
    assert sync(client, h, [fresh])["results"][0]["result"]["log"]["data"]["level"] == 1


def test_device_check_asked_and_answered_offline_reaches_the_ground(client, h):
    reading_at = ago(hours=5)
    readings = op("telemetry", {"source": "ble", "device": "pulse_oximeter", "readings": [
        {"metric": "spo2", "measuredAt": reading_at, "value": 89}]}, recordedAt=reading_at)
    answer = op("confirmation.answer", {"metric": "spo2", "answer": "not_ok", "symptoms": ["shortness_of_breath"]},
                targetId=f"op:{readings['opId']}", recordedAt=ago(hours=5))
    out = sync(client, h, [readings, answer])
    assert [r["status"] for r in out["results"]] == ["applied", "applied"], out["results"]
    conf = out["results"][0]["result"]["confirmations"][0]
    assert out["results"][1]["entityId"] == conf["id"]
    alert = out["results"][1]["result"]["alert"]
    assert alert["level"] == "high" and alert["sentToGround"] is True
    assert alert["id"] in out["changes"]["activeAlertIds"] and conf["id"] not in out["changes"]["pendingConfirmationIds"]
    again = sync(client, h, [readings, answer])
    assert again["summary"]["duplicates"] == 2
    ground = login(client, "GROUND", "2026")
    alerts = client.get(f"{API}/ground/alerts?astronautId=ast-test", headers=ground).json()
    assert sum(a["type"] == "device_check" for a in alerts) == 1


def test_idempotency_key_on_single_requests(client, h):
    key = {"Idempotency-Key": str(uuid.uuid4())}
    body = {"type": "mood", "data": {"level": 4}}
    first = client.post(f"{API}/me/logs", headers=h | key, json=body)
    second = client.post(f"{API}/me/logs", headers=h | key, json=body)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json() and first.json()["coinsEarned"] == 10
    assert len(client.get(f"{API}/me/logs?fromDay=12&type=mood", headers=h).json()) == 1
    r = client.post(f"{API}/me/ground-messages", headers=h | key, json={"text": "hello"})
    assert r.status_code == 422 and "already used" in r.json()["detail"]
    key = {"Idempotency-Key": str(uuid.uuid4())}
    sent = [client.post(f"{API}/me/ground-messages", headers=h | key, json={"text": "Queued once"}) for _ in range(2)]
    assert sent[0].status_code == 201 and sent[0].json()["id"] == sent[1].json()["id"]
    bad = {"Idempotency-Key": str(uuid.uuid4())}
    refused = [client.post(f"{API}/me/logs", headers=h | bad, json={"type": "meal", "data": {
        "planMealId": "not-in-plan", "adherence": "followed"}}) for _ in range(2)]
    assert refused[0].status_code == refused[1].status_code == 422 and refused[0].json() == refused[1].json()


def test_changes_since_a_cursor(client, h):
    start = client.get(f"{API}/me/changes", headers=h).json()
    assert start["since"] is None and start["home"]["mission"]["missionDay"] == 12
    quiet = client.get(f"{API}/me/changes", headers=h, params={"since": start["cursor"]}).json()
    assert quiet["logs"] == [] and quiet["alerts"] == [] and quiet["groundMessages"] == [] and quiet["confirmations"] == []
    client.post(f"{API}/me/logs", headers=h, json={"type": "mood", "data": {"level": 4}})
    client.post(f"{API}/me/ground-messages", headers=h, json={"text": "Checking in"})
    after = client.get(f"{API}/me/changes", headers=h, params={"since": start["cursor"]}).json()
    assert [l["type"] for l in after["logs"]] == ["mood"] and [m["text"] for m in after["groundMessages"]] == ["Checking in"]
    assert client.get(f"{API}/me/changes", headers=h, params={"since": "yesterday"}).status_code == 422
    assert client.get(f"{API}/me/changes", headers=h, params={"since": after["cursor"]}).json()["logs"] == []


def test_ground_sees_how_old_the_data_is(client, h):
    ground = login(client, "GROUND", "2026")
    before = client.get(f"{API}/ground/astronauts/ast-test/sync", headers=ground).json()
    assert before["link"]["status"] == "unknown" and before["operations"] == []
    sync(client, h, [op("log.create", {"type": "mood", "data": {"level": 3}}, recordedAt=ago(hours=9))], queuedRemaining=2)
    after = client.get(f"{API}/ground/astronauts/ast-test/sync", headers=ground).json()
    link = after["link"]
    assert link["status"] == "online" and link["minutesSinceContact"] == 0 and link["queuedOnDevice"] == 2
    assert link["lastBatch"]["applied"] == 1 and link["lastBatch"]["maxLagSec"] >= 9 * 3600
    assert after["operations"][0]["type"] == "log.create" and after["operations"][0]["lagSec"] >= 9 * 3600
    crew = client.get(f"{API}/ground/crew", headers=ground).json()["crew"]
    assert all(c["link"]["status"] in ("online", "delayed", "offline", "unknown") and "commDelaySec" in c["link"] for c in crew)
    assert client.get(f"{API}/me/sync/ops", headers=h).json()[0]["status"] == "applied"
    # only crew accounts have a queue
    assert client.post(f"{API}/me/sync", headers=ground, json={"ops": []}).status_code == 403
