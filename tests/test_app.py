"""CosmoMedic app layer (/api/v1/app): the frontend's data model, computed by the backend."""

import pytest

PIN = "2026"


@pytest.fixture(scope="module")
def crew(client):
    cache = {}

    def headers(crew_id: str) -> dict:
        if crew_id not in cache:
            r = client.post("/api/v1/app/auth/login", json={"crewId": crew_id, "pin": PIN})
            assert r.status_code == 200, r.text
            cache[crew_id] = {"Authorization": f"Bearer {r.json()['accessToken']}"}
        return cache[crew_id]

    return headers


def test_crew_login(client):
    r = client.post("/api/v1/app/auth/login", json={"crewId": "orb3-mika", "pin": PIN})
    assert r.status_code == 200
    body = r.json()
    assert body["astronaut"] == {"id": "ast-03", "name": "Mika Tanaka", "crewId": "ORB3-MIKA", "onboardingDone": True}
    assert body["next"] == "/home"
    assert client.post("/api/v1/app/auth/login", json={"crewId": "ORB3-MIKA", "pin": "0000"}).status_code == 401


def test_mission_is_day_12_of_orbit_3(client):
    m = client.get("/api/v1/app/mission").json()
    assert (m["id"], m["today"], m["lengthDays"], m["commDelaySec"]) == ("orbit-3", 12, 30, 1200)
    assert {e["id"] for e in m["environments"]} >= {"lab", "sleep"}


def test_daily_scores_reproduce_the_mock_stories(client, crew):
    # Status tag of the app: >= 70 ok, 50-69 warn, < 50 alert.
    expected = {"ORB3-ANA": (74, "ok", "stable"), "ORB3-RAJ": (97, "ok", "stable"),
                "ORB3-MIKA": (67, "warn", "attention"), "ORB3-LEO": (40, "alert", "alert")}
    for crew_id, (score, level, label) in expected.items():
        s = client.get("/api/v1/app/me/status", headers=crew(crew_id)).json()
        assert (s["overallScore"], s["statusLevel"], s["statusLabel"]) == (score, level, label), crew_id
        assert s["logsTotal"] == 6 and s["days"][-1]["statusLevel"] == level
        assert set(s["areas"]) == {"head", "torso", "armL", "armR", "legL", "legR"}
        assert len(s["days"]) == 7 and s["days"][-1]["missionDay"] == 12


def test_rule_alerts_and_orbi_notes(client, crew):
    raj = client.get("/api/v1/app/me/alerts", headers=crew("ORB3-RAJ")).json()
    assert raj == []
    leo = {a["type"]: a for a in client.get("/api/v1/app/me/alerts", headers=crew("ORB3-LEO")).json()}
    assert leo["low_mood"]["level"] == "high" and leo["low_mood"]["provenance"] == "RULE_DERIVED"
    assert leo["low_mood"]["sentToGround"] is True
    assert leo["poor_sleep"]["provenance"] == "SIMULATED_TELEMETRY"  # from the mock database
    notes = client.get("/api/v1/app/me/orbi-notes", headers=crew("ORB3-MIKA")).json()
    assert len(notes) == 1 and notes[0]["types"][0] == "low_mood"
    assert "turn these messages off" in notes[0]["body"]


def test_home(client, crew):
    h = client.get("/api/v1/app/me/home", headers=crew("ORB3-ANA")).json()
    assert [i["module"] for i in h["logs"]] == ["mood", "sleep", "radiation", "meals", "exercise", "body", "relax"]
    assert h["logsTotal"] == 6 and h["mission"]["missionDay"] == 12
    assert h["statusLevel"] == "ok"
    by_module = {i["module"]: i for i in h["logs"]}
    assert by_module["body"]["logTypes"] == ["measurement", "injury"] and by_module["meals"]["logTypes"] == ["meal"]
    assert not by_module["relax"]["countsForTotal"]
    assert h["orbi"]["types"][0] == "poor_sleep"
    assert h["avatarState"] == "sleepy"


@pytest.mark.parametrize("module", ["mood", "sleep", "radiation", "body", "meals", "exercise", "relax"])
def test_overviews(client, crew, module):
    r = client.get(f"/api/v1/app/me/logs/{module}/overview", headers=crew("ORB3-RAJ"))
    assert r.status_code == 200
    assert r.json()["module"] == module and "stats" in r.json()


def test_save_log_gives_10_coins_and_marks_today(client, crew):
    h = crew("ORB3-RAJ")
    before = client.get("/api/v1/app/me/home", headers=h).json()
    r = client.post("/api/v1/app/me/logs", headers=h, json={
        "type": "exercise", "environmentId": "lab",
        "data": {"modality": "aerobic", "equipment": "treadmill", "durationMin": 45, "effort": 3}})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["coinsEarned"] == 10 and body["coins"] == before["coins"] + 10
    assert body["log"]["provenance"] == "USER_REPORTED" and body["log"]["missionDay"] == 12
    assert body["log"]["timestamp"].endswith("Z")
    ex = client.get("/api/v1/app/me/logs/exercise/overview", headers=h).json()
    assert ex["loggedToday"] and ex["buttonLabel"].startswith("✓ Logged today")


def test_log_validation_is_scoped_to_its_type(client, crew):
    h = crew("ORB3-RAJ")
    r = client.post("/api/v1/app/me/logs", headers=h, json={"type": "mood", "data": {"level": 9}})
    assert r.status_code == 422
    assert all("mood" in str(e["loc"]) for e in r.json()["detail"])
    r = client.post("/api/v1/app/me/logs", headers=h, json={"type": "sleep", "data": {
        "bedtime": "2026-11-14T08:00:00Z", "wakeTime": "2026-11-14T07:00:00Z", "quality": 3, "wakeUps": "none"}})
    assert r.status_code == 422
    r = client.post("/api/v1/app/me/logs", headers=h, json={"type": "measurement", "data": {
        "metric": "blood_pressure", "values": {"systolic": 70, "diastolic": 90}}})
    assert r.status_code == 422


def test_sleep_log_computes_duration(client, crew):
    r = client.post("/api/v1/app/me/logs", headers=crew("ORB3-RAJ"), json={"type": "sleep", "environmentId": "sleep", "data": {
        "bedtime": "2026-11-14T00:40:00Z", "wakeTime": "2026-11-14T06:50:00Z", "quality": 4, "wakeUps": "none", "source": "manual"}})
    assert r.status_code == 201, r.text
    data = r.json()["log"]["data"]
    assert data["durationMin"] == 370 and data["bedtime"] == "2026-11-14T00:40:00Z"
    overview = client.get("/api/v1/app/me/logs/sleep/overview", headers=crew("ORB3-RAJ")).json()
    assert overview["loggedToday"] and overview["stats"]["lastNight"]["durationMin"] == 370


def test_measurement_delta_vs_baseline(client, crew):
    r = client.post("/api/v1/app/me/logs", headers=crew("ORB3-RAJ"), json={
        "type": "measurement", "data": {"metric": "strength", "values": {"value": 50}}})
    assert r.status_code == 201
    assert isinstance(r.json()["log"]["data"]["deltaVsBaselinePct"], float)


def test_severe_injury_goes_to_the_care_queue(client, crew):
    h = crew("ORB3-RAJ")
    r = client.post("/api/v1/app/me/logs", headers=h, json={"type": "injury", "data": {
        "bodyPart": "knee", "side": "left", "kind": "strain", "severity": "severe", "pain": 4,
        "description": "Twisted the knee on the treadmill harness"}})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["log"]["data"]["flaggedToGround"] is True
    assert body["supportRequestId"] is not None
    assert any(a["type"] == "injury" and a["level"] == "high" for a in body["alerts"])
    queue = client.get("/api/v1/care/support").json()
    assert any(q["id"] == body["supportRequestId"] and q["priority"] == "urgent" for q in queue)
    s = client.get("/api/v1/app/me/status", headers=h).json()
    assert s["areas"]["legL"] == "alert"


def test_edit_log_keeps_coins_and_ownership(client, crew):
    h = crew("ORB3-RAJ")
    log = client.get("/api/v1/app/me/logs?type=mood&limit=1", headers=h).json()[0]
    coins = client.get("/api/v1/app/me/home", headers=h).json()["coins"]
    r = client.put(f"/api/v1/app/me/logs/{log['id']}", headers=h, json={"type": "mood", "data": {"level": 2}})
    assert r.status_code == 200 and r.json()["coinsEarned"] == 0 and r.json()["coins"] == coins
    assert r.json()["log"]["data"]["label"] == "tired"
    assert client.put(f"/api/v1/app/me/logs/{log['id']}", headers=crew("ORB3-ANA"),
                      json={"type": "mood", "data": {"level": 2}}).status_code == 404
    assert client.put(f"/api/v1/app/me/logs/{log['id']}", headers=h,
                      json={"type": "meal", "data": {}}).status_code == 422


def test_ground_messages_have_communication_delay(client, crew):
    h = crew("ORB3-ANA")
    thread = client.get("/api/v1/app/me/ground-messages", headers=h).json()
    assert thread["commDelaySec"] == 1200 and thread["messages"]
    assert {"from", "sentAt", "estimatedArrivalAt", "status"} <= set(thread["messages"][0])
    sent = client.post("/api/v1/app/me/ground-messages", headers=h, json={"text": "Fan still loud", "urgent": True})
    assert sent.status_code == 201
    assert sent.json()["status"] == "in_transit" and sent.json()["from"] == "astronaut"
    reply = client.post("/api/v1/app/ground/messages", json={"astronautId": "ast-01", "text": "On it"})
    assert reply.status_code == 201 and reply.json()["from"] == "ground"
    # The ground reply is still travelling: it does not show up in the astronaut's thread yet.
    ids = {m["id"] for m in client.get("/api/v1/app/me/ground-messages", headers=h).json()["messages"]}
    assert sent.json()["id"] in ids and reply.json()["id"] not in ids


def test_ground_dashboard_is_care_team_only(client, crew):
    assert client.get("/api/v1/app/ground/crew", headers=crew("ORB3-ANA")).status_code == 403
    board = client.get("/api/v1/app/ground/crew").json()
    assert [r["astronaut"]["id"] for r in board["crew"]] == ["ast-01", "ast-02", "ast-03", "ast-04"]
    assert board["crew"][3]["latestAlert"]["level"] == "high"
    env = client.get("/api/v1/app/ground/environment").json()
    assert env["latest"] and env["readings"][0]["timestamp"].endswith("Z")
    assert client.get("/api/v1/app/ground/alerts?minLevel=high").json()
    assert client.get("/api/v1/app/ground/astronauts/ast-04/summaries").status_code == 200
    assert client.get("/api/v1/app/ground/astronauts/ast-99/status").status_code == 404


def test_astronauts_only_see_their_own_data(client, crew):
    logs = client.get("/api/v1/app/me/logs", headers=crew("ORB3-MIKA")).json()
    assert logs and {l["astronautId"] for l in logs} == {"ast-03"}
    assert client.get("/api/v1/app/me/home").status_code == 403  # the surgeon has no crew profile


def test_shop_purchase_and_equip(client, crew):
    h = crew("ORB3-MIKA")
    view = client.get("/api/v1/app/me/avatar", headers=h).json()
    coins = view["avatar"]["coins"]
    items = [i for group in view["shop"].values() for i in group]
    too_expensive = [i for i in items if not i["owned"] and i["price"] > coins]
    if too_expensive:
        assert client.post("/api/v1/app/me/avatar/purchase", headers=h,
                           json={"itemId": too_expensive[0]["id"]}).status_code == 409
    item = min((i for i in items if not i["owned"] and i["price"] <= coins), key=lambda i: i["price"])
    assert client.post("/api/v1/app/me/avatar/equip", headers=h, json={"itemId": item["id"]}).status_code == 409
    bought = client.post("/api/v1/app/me/avatar/purchase", headers=h, json={"itemId": item["id"]}).json()
    assert bought["avatar"]["coins"] == coins - item["price"]
    equipped = client.post("/api/v1/app/me/avatar/equip", headers=h, json={"itemId": item["id"]}).json()
    group = next(g for g in equipped["shop"].values() if any(i["id"] == item["id"] for i in g))
    assert next(i for i in group if i["id"] == item["id"])["equipped"]
    renamed = client.put("/api/v1/app/me/avatar", headers=h, json={"name": "Nova"}).json()
    assert renamed["avatar"]["name"] == "Nova"


def test_leaderboard(client, crew):
    board = client.get("/api/v1/app/leaderboard?by=logs", headers=crew("ORB3-LEO")).json()
    assert [r["rank"] for r in board["ranking"]] == [1, 2, 3, 4]
    assert sum(r["isMe"] for r in board["ranking"]) == 1


def test_orbi_opt_out(client, crew):
    h = crew("ORB3-LEO")
    prefs = client.put("/api/v1/app/me/preferences", headers=h, json={"proactiveMessages": False}).json()
    assert prefs["proactiveMessages"] is False and prefs["minDaysBetweenNotes"] == 7
    assert client.put("/api/v1/app/me/preferences", headers=h, json={"proactiveMessages": True}).json()["proactiveMessages"]


def test_profile_and_onboarding(client, crew):
    h = crew("ORB3-LEO")
    p = client.get("/api/v1/app/me/profile", headers=h).json()
    assert p["baseline"] and p["mission"]["id"] == "orbit-3"
    done = client.post("/api/v1/app/me/onboarding", headers=h, json={"avatarName": "Rover", "dailyReminders": False}).json()
    assert done["avatar"]["name"] == "Rover" and done["dailyReminders"] is False and done["onboardingDone"]


def test_app_provenance_header(client, crew):
    r = client.get("/api/v1/app/mission")
    assert r.headers["X-Data-Provenance"] == "SIMULATED_TELEMETRY"
    sources = {s["id"] for s in client.get("/api/v1/provenance").json()["sources"]}
    assert "cosmomedic-demo" in sources
