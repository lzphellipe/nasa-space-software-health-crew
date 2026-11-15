"""Every response says where its data comes from."""

import pytest

from app.provenance import ROUTES, Provenance

M = "/api/v1/missions/SYN-LDM-01"


def test_catalog_lists_types_sources_and_routes(client):
    catalog = client.get("/api/v1/provenance", headers={"Authorization": ""}).json()
    assert [t["code"] for t in catalog["types"]] == [p.value for p in Provenance]
    assert {s["id"] for s in catalog["sources"]} >= {"osdr-osd-569", "osdr-osd-575", "agbresa", "synthetic-mission"}
    agbresa = next(s for s in catalog["sources"] if s["id"] == "agbresa")
    assert "not astronauts" in agbresa["population"]
    assert len(catalog["routes"]) == len(ROUTES)


@pytest.mark.parametrize("path, expected", [
    ("/api/v1/crew", "REAL_RESEARCH_DATA,RULE_DERIVED"),
    ("/api/v1/analog/agbresa", "REAL_RESEARCH_DATA,RULE_DERIVED"),
    (f"{M}/dashboard", "SIMULATED_TELEMETRY,RULE_DERIVED"),
    ("/api/v1/care/patterns", "RULE_DERIVED"),
    ("/api/v1/knowledge/documents", "REFERENCE_DOCUMENT"),
])
def test_header(client, path, expected):
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["X-Data-Provenance"] == expected


def test_only_the_orbi_insight_routes_claim_ai_generated():
    claimed = [prefix for prefix, values in ROUTES if Provenance.AI_GENERATED.value in values]
    assert claimed == ["/api/v1/orbi"]


def test_item_level_values(client, auth):
    assert client.get("/api/v1/crew/C001/summary").json()["provenance"] == "REAL_RESEARCH_DATA"
    assert client.get("/api/v1/measurements", params={"limit": 1}).json()["items"][0]["provenance"] == "REAL_RESEARCH_DATA"
    obs = client.get("/api/v1/analog/agbresa/subjects/Y/observations").json()
    assert {o["provenance"] for o in obs} == {"REAL_RESEARCH_DATA"}

    day = client.get(f"{M}/astronauts/A002/daily", params={"fromDay": 49, "toDay": 49}).json()[0]
    assert day["provenance"] == "SIMULATED_TELEMETRY"
    assert client.get(f"{M}/astronauts/A002/episodes").json()[0]["provenance"] == "RULE_DERIVED"

    a3 = auth("astronaut3")
    history = client.get("/api/v1/me/checkins", params={"fromDay": 10, "toDay": 12}, headers=a3).json()
    assert {c["provenance"] for c in history} == {"SIMULATED_TELEMETRY"}
    sent = client.post("/api/v1/me/checkins", json={"stress": 2, "fatigue": 2}, headers=a3).json()
    assert sent["checkin"]["provenance"] == "USER_REPORTED"
    assert client.get("/api/v1/me/messages", headers=auth("astronaut4")).json()[0]["provenance"] == "RULE_DERIVED"

    hits = client.get("/api/v1/knowledge/search", params={"q": "radiation"}, headers=a3).json()
    assert hits["provenance"] == "REFERENCE_DOCUMENT" and hits["hits"][0]["provenance"] == "REFERENCE_DOCUMENT"


def test_support_provenance_follows_the_reason(client, auth):
    mine = client.post("/api/v1/me/support", json={"topic": "other", "message": "Question"}, headers=auth("astronaut1")).json()
    assert mine["provenance"] == "USER_REPORTED"
    queue = client.get("/api/v1/care/support").json()
    assert {r["provenance"] for r in queue if r["reason"] == "pattern"} == {"RULE_DERIVED"}
    assert {r["provenance"] for r in queue if r["reason"] != "pattern"} == {"USER_REPORTED"}
