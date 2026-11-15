"""Orbi insights and consultations (routes of the agent) with the AI switched off: statistical fallback."""

import pytest

URL = "/api/v1/orbi"


@pytest.fixture(autouse=True)
def ai_off(monkeypatch):
    monkeypatch.setenv("ORBI_AI_ENABLED", "false")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)


def test_insight_uses_the_statistical_text_without_a_model(client, auth):
    crew = auth("astronaut4")
    for page in ("home", "sleep"):
        r = client.get(f"{URL}/insights", headers=crew, params={"page": page, "missionDay": 120})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["aiGenerated"] is False and body["messageSource"] in ("statistical_fallback", "data_status")
        assert body.get("message") or body.get("insight")
    assert client.get(f"{URL}/insights", headers=crew, params={"page": "nope"}).status_code == 422


def test_insight_access(client, auth):
    assert client.get(f"{URL}/insights", headers={"Authorization": ""}, params={"page": "sleep"}).status_code == 401
    assert client.get(f"{URL}/insights", headers=auth("astronaut4"), params={"page": "sleep", "astronautId": "A001"}).status_code == 403
    assert client.get(f"{URL}/insights", params={"page": "sleep", "astronautId": "A001"}).status_code == 200  # surgeon


def test_consultation_saves_a_briefing_the_care_team_can_read(client, auth):
    crew = auth("astronaut4")
    assert client.post(f"{URL}/consultations", headers=crew, json={"confirmed": False}).status_code == 422
    r = client.post(f"{URL}/consultations", headers=crew, json={"confirmed": True, "message": "I would like to talk about my sleep."})
    assert r.status_code == 201, r.text
    made = r.json()
    assert len(made["sha256"]) == 64 and made["status"] == "open"
    assert client.get(made["packetUrl"], headers=crew).status_code == 200
    assert client.get(made["packetUrl"], headers=auth("astronaut1")).status_code in (403, 404)
    assert client.get(f"{URL}/ground/consultations/{made['requestId']}/fhir").status_code == 200  # surgeon
    assert client.get(f"{URL}/ground/consultations/{made['requestId']}/fhir", headers=crew).status_code == 403
