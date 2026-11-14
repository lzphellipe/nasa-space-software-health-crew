"""Authentication, check-ins, 7-day patterns, proactive messages, human support and PDF search."""

import hashlib
from collections import defaultdict
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import db
from app.models import ProactiveMessage, UserRefreshToken, utcnow
from app.services.patterns import detect_window
from app.services.proactive import maybe_send

M = "/api/v1/missions/SYN-LDM-01"
NO_AUTH = {"Authorization": ""}


# --- authentication and access ------------------------------------------------------


def test_login_and_me(client, auth):
    bad = client.post("/api/v1/auth/login", json={"username": "astronaut1", "password": "wrong"}, headers=NO_AUTH)
    assert bad.status_code == 401
    logged_in = client.post(
        "/api/v1/auth/login",
        json={"username": "astronaut1", "password": "test-password"},
        headers=NO_AUTH,
    )
    assert logged_in.status_code == 200
    login_body = logged_in.json()
    assert set(login_body) == {
        "accessToken",
        "tokenType",
        "expiresIn",
        "refreshToken",
        "refreshExpiresIn",
        "user",
    }
    assert login_body["expiresIn"] == 1800
    assert login_body["refreshExpiresIn"] == 30 * 24 * 60 * 60
    assert set(login_body["user"]) == {"username", "displayName", "role", "astronautId"}
    me = client.get("/api/v1/me", headers=auth("astronaut1")).json()
    assert me["astronautId"] == "A001" and me["missionDay"] == 180
    assert client.get("/api/v1/me").status_code == 403  # surgeon has no personal endpoints
    form = client.post("/api/v1/auth/token", data={"username": "surgeon", "password": "test-password"}, headers=NO_AUTH)
    assert set(form.json()) == {
        "access_token",
        "token_type",
        "expires_in",
        "refresh_token",
        "refresh_expires_in",
    }
    assert form.json()["token_type"] == "bearer"
    assert form.json()["expires_in"] == 1800
    assert form.json()["refresh_token"]


def test_refresh_rotates_tokens_and_replay_revokes_the_family(client):
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "astronaut2", "password": "test-password"},
        headers=NO_AUTH,
    )
    original = login.json()["refreshToken"]
    original_hash = hashlib.sha256(original.encode("utf-8")).hexdigest()
    with db.SessionLocal() as session:
        stored = session.scalar(select(UserRefreshToken).where(UserRefreshToken.token_hash == original_hash))
        assert stored is not None
        assert stored.token_hash != original

    rotated = client.post("/api/v1/auth/refresh", json={"refreshToken": original}, headers=NO_AUTH)
    assert rotated.status_code == 200
    assert set(rotated.json()) == {
        "accessToken",
        "tokenType",
        "expiresIn",
        "refreshToken",
        "refreshExpiresIn",
        "user",
    }
    assert rotated.json()["refreshToken"] != original
    assert 0 < rotated.json()["refreshExpiresIn"] <= login.json()["refreshExpiresIn"]
    me = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {rotated.json()['accessToken']}"},
    )
    assert me.status_code == 200

    replay = client.post("/api/v1/auth/refresh", json={"refreshToken": original}, headers=NO_AUTH)
    assert replay.status_code == 401
    revoked_family = client.post(
        "/api/v1/auth/refresh",
        json={"refreshToken": rotated.json()["refreshToken"]},
        headers=NO_AUTH,
    )
    assert revoked_family.status_code == 401


def test_logout_revokes_refresh_session(client):
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "astronaut3", "password": "test-password"},
        headers=NO_AUTH,
    )
    refresh_token = login.json()["refreshToken"]
    access_token = login.json()["accessToken"]
    response = client.post("/api/v1/auth/logout", json={"refreshToken": refresh_token}, headers=NO_AUTH)
    assert response.status_code == 204
    assert response.content == b""
    assert client.post("/api/v1/auth/refresh", json={"refreshToken": refresh_token}, headers=NO_AUTH).status_code == 401
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {access_token}"}).status_code == 200


def test_expired_refresh_token_is_rejected(client):
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "astronaut4", "password": "test-password"},
        headers=NO_AUTH,
    )
    refresh_token = login.json()["refreshToken"]
    token_hash = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
    with db.SessionLocal() as session:
        stored = session.scalar(select(UserRefreshToken).where(UserRefreshToken.token_hash == token_hash))
        assert stored is not None
        stored.expires_at = utcnow() - timedelta(seconds=1)
        session.commit()

    response = client.post("/api/v1/auth/refresh", json={"refreshToken": refresh_token}, headers=NO_AUTH)
    assert response.status_code == 401


def test_astronaut_sees_only_own_data(client, auth):
    a1 = auth("astronaut1")
    assert client.get(f"{M}/astronauts/A001/today", headers=a1).status_code == 200
    for path in (f"{M}/astronauts/A002/today", f"{M}/astronauts/A002/daily", f"{M}/astronauts/A002/agent-context",
                 f"{M}/dashboard", f"{M}/events", "/api/v1/care/support", "/api/v1/care/checkins"):
        assert client.get(path, headers=a1).status_code == 403, path
    assert client.get(f"{M}/astronauts/A002/today").status_code == 200  # surgeon
    assert client.get(f"{M}/dashboard", headers=NO_AUTH).status_code == 401
    assert client.get("/api/v1/crew", headers=NO_AUTH).status_code == 200  # public OSDR data stays open


# --- 7-day pattern rules ------------------------------------------------------------


def days(n, **values):
    return [{"mission_day": d, **values} for d in range(n)]


def test_rules_on_a_window():
    records = days(7, sleep_hours=5.2, mood=2, body_temperature=38.3, systolic_bp=142, diastolic_bp=91,
                   blood_glucose=95, heart_rate=60, calorie_intake=2500)
    checkins = [{"mission_day": d, "stress": 4, "fatigue": 4, "symptoms": ["headache"] + (["chest_pain"] if d == 6 else [])}
                for d in range(7)]
    found = {(f.pattern, f.detail): f.severity for f in detect_window(6, records, checkins, {})}
    assert found[("sleep_debt", "")] == "high"
    assert found[("elevated_stress", "")] == "medium"
    assert found[("low_mood", "")] == "medium"
    assert found[("wellbeing_cluster", "")] == "high"
    assert found[("fever", "")] == "high"
    assert found[("rising_blood_pressure", "")] == "high"
    assert found[("recurring_symptom", "headache")] == "medium"
    assert found[("urgent_symptom", "chest_pain")] == "high"


def test_quiet_window_has_no_findings():
    records = days(7, sleep_hours=7, mood=4, body_temperature=36.7, systolic_bp=118, diastolic_bp=76,
                   blood_glucose=88, heart_rate=62, calorie_intake=2600)
    checkins = [{"mission_day": d, "stress": 2, "fatigue": 2, "symptoms": []} for d in range(7)]
    assert detect_window(6, records, checkins, {"calorie_intake": {"mean": 2600}}) == []


def test_history_finds_the_labeled_events(client):
    def detected(astronaut, pattern, start, end):
        rows = client.get("/api/v1/care/patterns", params={
            "astronautId": astronaut, "fromDay": start, "toDay": end, "minSeverity": "medium"}).json()
        return [r for r in rows if r["pattern"] == pattern]

    assert detected("A002", "fever", 47, 55)
    assert detected("A003", "rising_blood_pressure", 140, 180)
    assert detected("A004", "wellbeing_cluster", 100, 130)
    assert detected("A001", "recurring_symptom", 120, 150)
    assert not detected("A002", "fever", 0, 40)


# --- proactive messages --------------------------------------------------------------


def test_at_most_one_message_every_7_days(client):
    messages = client.get("/api/v1/care/messages").json()
    assert messages
    by_astronaut = defaultdict(list)
    for m in messages:
        by_astronaut[m["astronautId"]].append(m["missionDay"])
    for days_sent in by_astronaut.values():
        days_sent.sort()
        assert all(b - a >= 7 for a, b in zip(days_sent, days_sent[1:]))
    assert all("turn these messages off" in m["body"] for m in messages)


def test_opt_out_stops_messages(client, auth):
    a3 = auth("astronaut3")
    assert client.put("/api/v1/me/preferences", json={"proactiveMessages": False}, headers=a3).json() == {
        "proactiveMessages": False, "minDaysBetweenMessages": 7}
    with db.SessionLocal() as session:
        session.query(ProactiveMessage).filter(ProactiveMessage.astronaut_id == "A003").delete()
        session.commit()
        assert maybe_send(session, "SYN-LDM-01", "A003", 160, 7) is None  # A003 has findings on day 160
        client.put("/api/v1/me/preferences", json={"proactiveMessages": True}, headers=a3)
        assert maybe_send(session, "SYN-LDM-01", "A003", 160, 7) is not None
        assert maybe_send(session, "SYN-LDM-01", "A003", 165, 7) is None  # 5 days later: too soon
        session.rollback()


def test_messages_are_private_and_can_be_marked_read(client, auth):
    mine = client.get("/api/v1/me/messages", headers=auth("astronaut4")).json()
    assert mine
    other = client.post(f"/api/v1/me/messages/{mine[0]['id']}/read", headers=auth("astronaut1"))
    assert other.status_code == 404
    read = client.post(f"/api/v1/me/messages/{mine[0]['id']}/read", headers=auth("astronaut4"))
    assert read.json()["status"] == "read"


# --- check-ins and human support ----------------------------------------------------


def test_checkin_upsert_validation_and_urgent_routing(client, auth):
    a2 = auth("astronaut2")
    options = client.get("/api/v1/me/checkins/options", headers=a2).json()
    assert any(s["code"] == "chest_pain" and s["urgent"] for s in options["symptoms"])
    assert client.post("/api/v1/me/checkins", json={"stress": 6, "fatigue": 2}, headers=a2).status_code == 422
    assert client.post("/api/v1/me/checkins", json={"stress": 2, "fatigue": 2, "symptoms": ["x"]}, headers=a2).status_code == 422

    first = client.post("/api/v1/me/checkins", json={"stress": 2, "fatigue": 2, "symptoms": ["headache"]}, headers=a2).json()
    assert first["supportRequestId"] is None
    second = client.post("/api/v1/me/checkins", json={"stress": 3, "fatigue": 4, "symptoms": ["shortness_of_breath"]},
                         headers=a2).json()
    assert second["checkin"]["id"] == first["checkin"]["id"]  # same day: replaced
    assert second["urgentSymptoms"] == ["shortness_of_breath"]
    queue = client.get("/api/v1/care/support", params={"status": "open"}).json()
    assert queue[0]["id"] == second["supportRequestId"] and queue[0]["priority"] == "urgent"


def test_support_request_flow(client, auth):
    a1, a4 = auth("astronaut1"), auth("astronaut4")
    created = client.post("/api/v1/me/support", json={"topic": "family", "message": "Can I get an extra call home?"},
                          headers=a1)
    assert created.status_code == 201
    rid = created.json()["id"]
    assert rid not in [r["id"] for r in client.get("/api/v1/me/support", headers=a4).json()]
    patched = client.patch(f"/api/v1/care/support/{rid}", json={
        "status": "acknowledged", "response": "Scheduled for tomorrow's comm pass.", "careNotes": "internal"}).json()
    assert patched["assignedTo"] == "surgeon"
    seen = next(r for r in client.get("/api/v1/me/support", headers=a1).json() if r["id"] == rid)
    assert seen["response"] == "Scheduled for tomorrow's comm pass." and "careNotes" not in seen
    assert client.patch(f"/api/v1/care/support/{rid}", json={"status": "resolved"}, headers=a1).status_code == 403


# --- PDF knowledge base -----------------------------------------------------------------


def tiny_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def test_bundled_standard_is_searchable(client, auth):
    docs = client.get("/api/v1/knowledge/documents", headers=auth("astronaut1")).json()
    assert any("NASA-STD-3001" in d["title"] and d["pages"] == 87 for d in docs)
    result = client.get("/api/v1/knowledge/search", params={"q": "radiation exposure limits"}, headers=auth("astronaut1")).json()
    assert result["total"] > 0 and result["hits"][0]["page"] > 6  # not the table of contents
    assert client.get("/api/v1/knowledge/search", params={"q": "radiation"}, headers=NO_AUTH).status_code == 401


def test_upload_pdf(client, auth):
    pdf = tiny_pdf("Hydration checklist for crew quarters zeta")
    files = {"file": ("hydration.pdf", pdf, "application/pdf")}
    assert client.post("/api/v1/knowledge/documents", files=files, headers=auth("astronaut1")).status_code == 403
    created = client.post("/api/v1/knowledge/documents", files=files, data={"title": "Hydration checklist"})
    assert created.status_code == 201, created.text
    hit = client.get("/api/v1/knowledge/search", params={"q": "hydration zeta"}).json()["hits"][0]
    assert hit["documentTitle"] == "Hydration checklist" and hit["page"] == 1
    assert client.post("/api/v1/knowledge/documents", files=files).status_code == 409
    not_pdf = {"file": ("notes.txt", b"plain text", "text/plain")}
    assert client.post("/api/v1/knowledge/documents", files=not_pdf).status_code == 422
    page = client.get(f"/api/v1/knowledge/documents/{created.json()['id']}/pages/1").json()
    assert "Hydration checklist" in page["text"]


@pytest.mark.parametrize("path", ["/api/v1/care/patterns", "/api/v1/care/messages", "/api/v1/care/checkins"])
def test_care_endpoints_answer(client, path):
    assert client.get(path).status_code == 200
