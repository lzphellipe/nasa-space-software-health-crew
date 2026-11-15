"""Snapshot in the frontend's mock-db.json shape, and saving a chat with Orbi that happened on the device."""

import json
import uuid
from pathlib import Path

API = "/api/v1/app"
MOCK = json.loads((Path(__file__).parents[1] / "app" / "seed" / "cosmomedic" / "mock-db.json").read_text(encoding="utf-8"))


def login(client, crew_id, pin):
    body = client.post(f"{API}/auth/login", json={"crewId": crew_id, "pin": pin}).json()
    return {"Authorization": f"Bearer {body['accessToken']}"}


def test_snapshot_has_the_shape_of_the_frontend_mock(client):
    snap = client.get(f"{API}/snapshot", headers=login(client, "ORB3-ANA", "2026")).json()
    assert snap["currentAstronautId"] == "ast-01" and snap["role"] == "astronaut"
    assert {k for k in MOCK if not k.startswith("$")} <= set(snap)
    pairs = [(snap["mission"], MOCK["mission"]), (snap["astronauts"][0], MOCK["astronauts"][0]),
             (snap["logs"][0], MOCK["logs"][0]), (snap["daily_summaries"][0], MOCK["daily_summaries"][0]),
             (snap["environment_readings"][0], MOCK["environment_readings"][0]), (snap["shop"][0], MOCK["shop"][0]),
             (snap["ai_chat"]["messages"][0], MOCK["ai_chat"]["messages"][0]),
             (snap["ai_chat"]["journal"][0], MOCK["ai_chat"]["journal"][0]),
             (snap["ai_chat"]["alerts"][0], MOCK["ai_chat"]["alerts"][0]),
             (snap["ground_messages"][0], MOCK["ground_messages"][0]), (snap["calendar"][0], MOCK["calendar"][0])]
    for mine, theirs in pairs:
        assert {k for k in theirs if not k.startswith("$")} <= set(mine)
    assert snap["mission"]["today"] == 12 and len(snap["daily_summaries"]) == 12
    assert {a["id"] for a in snap["astronauts"]} == {"ast-01", "ast-02", "ast-03", "ast-04"}


def test_astronaut_gets_only_own_health_data(client):
    snap = client.get(f"{API}/snapshot", headers=login(client, "ORB3-ANA", "2026")).json()
    assert {l["astronautId"] for l in snap["logs"]} == {"ast-01"}
    assert {s["astronautId"] for s in snap["daily_summaries"]} == {"ast-01"}
    assert {a["astronautId"] for a in snap["ai_chat"]["alerts"]} <= {"ast-01"}
    mate = next(a for a in snap["astronauts"] if a["id"] == "ast-02")
    assert mate["name"] and mate["avatar"]["equipped"] and mate["baseline"]["restingHr"] == 0 and mate["profile"]["age"] == 0
    assert [r["astronautId"] for r in snap["leaderboard"]] == ["ast-01", "ast-02", "ast-03", "ast-04"]
    assert all(r["logs"] > 0 for r in snap["leaderboard"])


def test_ground_gets_the_crew_but_not_the_chats(client):
    snap = client.get(f"{API}/snapshot", headers=login(client, "GROUND", "2026")).json()
    assert snap["role"] == "ground" and {l["astronautId"] for l in snap["logs"]} == {"ast-01", "ast-02", "ast-03", "ast-04"}
    assert len(snap["daily_summaries"]) == 48 and snap["ai_chat"]["messages"] == []
    assert all(a["baseline"]["restingHr"] > 0 for a in snap["astronauts"])
    assert client.get(f"{API}/snapshot", headers={"Authorization": ""}).status_code == 401


def test_chat_with_orbi_on_the_device_is_saved_once(client):
    h = login(client, "TEST", "0000")
    op = {"opId": str(uuid.uuid4()), "type": "chat.save", "recordedAt": "2026-11-14T20:00:00Z",
          "payload": {"text": "I could not sleep well.", "reply": "Sorry to hear that. Want a wind-down plan?", "moodLevel": 2}}
    for _ in range(2):
        r = client.post(f"{API}/me/sync", headers=h, json={"ops": [op]})
        assert r.status_code == 200 and r.json()["results"][0]["status"] == "applied", r.text
    snap = client.get(f"{API}/snapshot", headers=h).json()
    msgs = [m for m in snap["ai_chat"]["messages"] if m["text"] in (op["payload"]["text"], op["payload"]["reply"])]
    assert [m["author"] for m in msgs] == ["astronaut", "orbi"]
    entry = next(j for j in snap["ai_chat"]["journal"] if j["summary"] == "I could not sleep well.")
    assert entry["moodLevel"] == 2 and entry["sourceMessageIds"] == [m["id"] for m in msgs]
    bad = {"opId": str(uuid.uuid4()), "type": "chat.save", "payload": {"text": "hi"}}
    assert client.post(f"{API}/me/sync", headers=h, json={"ops": [bad]}).json()["results"][0]["status"] == "rejected"
