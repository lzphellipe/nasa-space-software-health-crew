"""Device telemetry (wearable, cuff, dosimeter, EVA suit) and Orbi's confirmation queue."""

from datetime import datetime, timedelta, timezone

import pytest

API = "/api/v1/app"


def login(client, crew_id, pin):
    body = client.post(f"{API}/auth/login", json={"crewId": crew_id, "pin": pin}).json()
    return {"Authorization": f"Bearer {body['accessToken']}"}


@pytest.fixture()
def fresh_test(client):
    """The TEST astronaut with no readings, confirmations or logs for today."""
    from sqlalchemy import delete

    from app import db
    from app.cosmo.models import AppConfirmation, AppDeviceReading, AppLog

    with db.SessionLocal() as s:
        for model in (AppConfirmation, AppDeviceReading):
            s.execute(delete(model).where(model.astronaut_id == "ast-test"))
        s.execute(delete(AppLog).where(AppLog.astronaut_id == "ast-test", AppLog.mission_day == 12))
        s.commit()
    return login(client, "TEST", "0000")


def ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat().replace("+00:00", "Z")


def test_seeded_sync_feeds_the_health_overview(client):
    home = client.get(f"{API}/me/home", headers=login(client, "ORB3-LEO", "2026")).json()
    assert home["sync"]["lastSyncAt"] and home["sync"]["simulated"] is True
    assert {d["device"] for d in home["sync"]["devices"]} == {"wrist_band", "bp_cuff", "dosimeter"}
    assert {"heart_rate", "spo2", "body_temperature"} <= set(home["vitals"])
    assert home["vitals"]["spo2"]["provenance"] == "SIMULATED_TELEMETRY"
    assert home["pendingConfirmations"] == home["sync"]["pendingConfirmations"]


def test_sync_from_a_real_device_and_confirm_a_sleep_session(client, fresh_test):
    h = fresh_test
    r = client.post(f"{API}/me/telemetry", headers=h, json={"source": "health_connect", "device": "wrist_band", "readings": [
        {"metric": "heart_rate", "measuredAt": ago(minutes=30), "value": 64},
        {"metric": "spo2", "measuredAt": ago(minutes=30), "value": 97},
        {"metric": "sleep_session", "measuredAt": ago(hours=6), "values": {
            "bedtime": ago(hours=12), "wakeTime": ago(hours=6, minutes=30), "avgHr": 55, "awakenings": 2}},
    ]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["accepted"] == 3 and body["provenance"] == "DEVICE_MEASURED"
    [conf] = body["confirmations"]
    assert conf["kind"] == "log" and conf["logType"] == "sleep" and conf["actions"] == ["confirm", "correct", "dismiss"]
    assert conf["proposed"]["wakeUps"] == "1-2" and conf["proposed"]["source"] == "archer_band"
    assert "5 h 30 min" in conf["question"]
    # resending the same batch is safe
    again = client.post(f"{API}/me/telemetry", headers=h, json={"source": "health_connect", "device": "wrist_band",
                                                                 "readings": [{"metric": "heart_rate", "measuredAt": r.json()["byMetric"] and ago(minutes=30), "value": 64}]})
    assert again.status_code == 201

    coins = client.get(f"{API}/me/home", headers=h).json()["coins"]
    done = client.post(f"{API}/me/confirmations/{conf['id']}/confirm", headers=h, json={"correction": {"quality": 2}})
    assert done.status_code == 200, done.text
    out = done.json()
    assert out["confirmation"]["status"] == "corrected" and out["confirmation"]["logId"]
    log = out["saved"]["log"]
    assert log["type"] == "sleep" and log["data"]["quality"] == 2 and log["data"]["durationMin"] == 330
    assert log["provenance"] == "DEVICE_MEASURED" and out["saved"]["coinsEarned"] == 10
    assert client.get(f"{API}/me/home", headers=h).json()["coins"] == coins + 10
    assert client.get(f"{API}/me/logs/sleep/overview", headers=h).json()["loggedToday"]
    assert client.post(f"{API}/me/confirmations/{conf['id']}/confirm", headers=h).status_code == 409


def test_readings_are_validated(client, fresh_test):
    bad = client.post(f"{API}/me/telemetry", headers=fresh_test, json={"source": "ble", "device": "watch", "readings": [
        {"metric": "spo2", "measuredAt": ago(minutes=1), "value": 140},
        {"metric": "blood_pressure", "measuredAt": ago(minutes=1), "values": {"systolic": 70, "diastolic": 90}},
        {"metric": "teleport", "measuredAt": ago(minutes=1), "value": 1},
    ]})
    assert bad.status_code == 422
    assert [e["loc"][-1] for e in bad.json()["detail"]] == [0, 1, 2]
    assert client.post(f"{API}/me/telemetry", headers=fresh_test,
                       json={"source": "fax", "device": "watch", "readings": []}).status_code == 422


def test_low_spo2_check_answered_not_ok_alerts_the_ground(client, fresh_test):
    h = fresh_test
    r = client.post(f"{API}/me/telemetry/simulate", headers=h, json={"hours": 3, "anomaly": "low_spo2"}).json()
    assert r["provenance"] == "SIMULATED_TELEMETRY"
    check = next(c for c in r["confirmations"] if c["kind"] == "check")
    assert check["metric"] == "spo2" and check["urgent"] and check["actions"] == ["ok", "not_ok", "dismiss"]
    assert "shortness_of_breath" in check["symptomOptions"] and "89" in check["question"]
    assert client.post(f"{API}/me/confirmations/{check['id']}/confirm", headers=h).status_code == 422

    ans = client.post(f"{API}/me/confirmations/{check['id']}/answer", headers=h,
                      json={"answer": "not_ok", "symptoms": ["shortness_of_breath", "dizziness"]}).json()
    assert ans["confirmation"]["status"] == "answered" and ans["alert"]["level"] == "high"
    assert ans["alert"]["type"] == "device_check" and ans["alert"]["sentToGround"] and "Mission Control" in ans["reply"]
    ground = login(client, "GROUND", "2026")
    alerts = client.get(f"{API}/ground/alerts", headers=ground, params={"astronautId": "ast-test"}).json()
    assert any(a["id"] == ans["alert"]["id"] for a in alerts)
    queue = client.get("/api/v1/care/support").json()
    assert any(q["detail"] == "device_check:spo2" and q["priority"] == "urgent" for q in queue)
    # still active after the rules run again (a new log triggers them)
    client.post(f"{API}/me/logs", headers=h, json={"type": "mood", "data": {"level": 3}})
    assert any(a["id"] == ans["alert"]["id"] for a in client.get(f"{API}/me/alerts", headers=h).json())


def test_check_answered_ok_and_dismissed_readings(client, fresh_test):
    h = fresh_test
    r = client.post(f"{API}/me/telemetry/simulate", headers=h, json={"hours": 24, "anomaly": "fever"}).json()
    kinds = {c["metric"]: c for c in r["confirmations"]}
    assert set(kinds) == {"sleep_session", "blood_pressure", "radiation_dose", "body_temperature"}
    ok = client.post(f"{API}/me/confirmations/{kinds['body_temperature']['id']}/answer", headers=h, json={"answer": "ok"}).json()
    assert ok["alert"] is None and ok["confirmation"]["answer"]["answer"] == "ok"
    gone = client.post(f"{API}/me/confirmations/{kinds['radiation_dose']['id']}/dismiss", headers=h).json()
    assert gone["status"] == "dismissed"
    bp = client.post(f"{API}/me/confirmations/{kinds['blood_pressure']['id']}/confirm", headers=h).json()
    assert bp["saved"]["log"]["data"]["metric"] == "blood_pressure" and bp["saved"]["log"]["provenance"] == "SIMULATED_TELEMETRY"
    pending = client.get(f"{API}/me/confirmations", headers=h).json()
    assert [c["metric"] for c in pending] == ["sleep_session"]
    assert len(client.get(f"{API}/me/confirmations", headers=h, params={"status": "all"}).json()) == 4
    # a second fever reading the same day does not ask again
    again = client.post(f"{API}/me/telemetry", headers=h, json={"source": "ble", "device": "thermometer", "readings": [
        {"metric": "body_temperature", "measuredAt": ago(minutes=5), "value": 38.6}]}).json()
    assert again["confirmations"] == []


def test_logged_today_is_not_asked_again(client, fresh_test):
    h = fresh_test
    client.post(f"{API}/me/logs", headers=h, json={"type": "measurement", "data": {
        "metric": "blood_pressure", "values": {"systolic": 118, "diastolic": 76}}})
    r = client.post(f"{API}/me/telemetry/simulate", headers=h, json={"hours": 24}).json()
    assert "blood_pressure" not in {c["metric"] for c in r["confirmations"]}


def test_eva_suit_telemetry(client, fresh_test):
    h = fresh_test
    r = client.post(f"{API}/me/telemetry/simulate", headers=h, json={"hours": 8, "eva": True, "anomaly": "eva_co2"}).json()
    assert {"eva_heart_rate", "eva_co2", "eva_suit_pressure", "eva_metabolic_rate"} <= set(r["byMetric"])
    check = next(c for c in r["confirmations"] if c["metric"] == "eva_co2")
    assert check["urgent"] and "headache" in check["question"]
    view = client.get(f"{API}/me/telemetry", headers=h, params={"metric": "eva_suit_pressure", "hours": 8}).json()
    s = view["summary"]["eva_suit_pressure"]
    assert 29 < s["avg"] < 30.2 and s["unit"] == "kPa" and view["readings"][0]["source"] == "simulator"
    status = client.get(f"{API}/me/devices/status", headers=h).json()
    assert "eva_suit" in {d["device"] for d in status["devices"]}


def test_ground_sees_sync_and_confirmations(client):
    ground = login(client, "GROUND", "2026")
    crew = client.get(f"{API}/ground/crew", headers=ground).json()["crew"]
    assert all(r["lastSyncAt"] and "spo2" in r["vitals"] for r in crew)
    tel = client.get(f"{API}/ground/astronauts/ast-02/telemetry", headers=ground, params={"metric": "heart_rate"}).json()
    assert tel["summary"]["heart_rate"]["count"] > 50 and tel["sync"]["devices"]
    confs = client.get(f"{API}/ground/confirmations", headers=ground, params={"status": "pending"}).json()
    assert all(c["status"] == "pending" for c in confs)
    assert client.get(f"{API}/ground/confirmations", headers=login(client, "ORB3-ANA", "2026")).status_code == 403


def test_metric_catalog_and_provenance(client):
    metrics = {m["metric"]: m for m in client.get(f"{API}/telemetry/metrics").json()}
    assert metrics["sleep_session"]["becomesLog"] == "sleep" and metrics["eva_co2"]["eva"]
    types = [t["code"] for t in client.get("/api/v1/provenance").json()["types"]]
    assert types[-1] == "DEVICE_MEASURED"
    r = client.get(f"{API}/me/devices/status", headers=login(client, "ORB3-RAJ", "2026"))
    assert set(r.headers["X-Data-Provenance"].split(",")) == {"DEVICE_MEASURED", "SIMULATED_TELEMETRY"}
