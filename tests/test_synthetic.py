import pytest

from app.synthetic.assess import assess_record, compute_baselines, find_episodes
from app.synthetic.generator import BONE_SCAN_DAYS, EVENTS, WBC_DAYS, generate
from app.synthetic.metrics import REQUIRED_METRICS

API = "/api/v1/missions/SYN-LDM-01"


@pytest.fixture(scope="module")
def dataset():
    return generate(42)


def test_shape(dataset):
    assert len(dataset.records) == 4 * 211
    days = sorted({r["mission_day"] for r in dataset.records})
    assert days[0] == -30 and days[-1] == 180
    assert {r["mission_id"] for r in dataset.records} == {"SYN-LDM-01"}


def test_required_metrics_are_never_missing(dataset):
    for r in dataset.records:
        for code in REQUIRED_METRICS:
            assert r[code] is not None, (r["astronaut_id"], r["mission_day"], code)
        assert 1 <= r["mood"] <= 5


def test_sparse_metrics_follow_their_cadence(dataset):
    for r in dataset.records:
        assert (r["bone_density"] is not None) == (r["mission_day"] in BONE_SCAN_DAYS)
        assert (r["white_blood_cells"] is not None) == (r["mission_day"] in WBC_DAYS)


def test_generation_is_deterministic():
    assert generate(42).records == generate(42).records
    assert generate(42).records != generate(7).records


def test_bone_density_declines_in_flight(dataset):
    for a in dataset.astronauts:
        bone = [r["bone_density"] for r in dataset.records if r["astronaut_id"] == a.id and r["bone_density"]]
        assert bone[-1] < bone[0] * 0.95


@pytest.mark.parametrize("event", EVENTS, ids=lambda e: e.kind)
def test_every_labeled_event_produces_an_episode(dataset, event):
    records = [r for r in dataset.records if r["astronaut_id"] == event.astronaut_id]
    baselines = compute_baselines(records)
    episodes = find_episodes(records, [assess_record(r, baselines) for r in records])
    hits = [
        e for e in episodes
        if e["metric"] in event.effects
        and e["start_day"] <= event.end_day
        and e["end_day"] >= event.start_day
        and (e["status"] == "out_of_range" or e["measurements"] >= 5)
    ]
    assert hits, f"{event.kind} left no clear signal"


def test_api_mission_and_dashboard(client):
    missions = client.get("/api/v1/missions").json()
    assert missions[0]["id"] == "SYN-LDM-01" and missions[0]["synthetic"] is True
    dash = client.get(f"{API}/dashboard").json()
    assert dash["missionDay"] == 180
    assert dash["counts"]["total"] == 4
    assert len(dash["statusByDay"]) == 211


def test_api_agent_context_default_window(client):
    ctx = client.get(f"{API}/astronauts/A004/agent-context").json()
    assert ctx["window"] == {"fromDay": 91, "toDay": 180, "days": 90}
    assert len(ctx["daily"]) == 90
    assert ctx["dataNotice"].startswith("SYNTHETIC")
    assert any("Sleep: out of range" in h for h in ctx["highlights"])
    lean = client.get(f"{API}/astronauts/A004/agent-context", params={"includeDaily": False}).json()
    assert lean["daily"] is None


def test_api_today_reports_latest_sparse_values(client):
    day = client.get(f"{API}/astronauts/A002/today", params={"day": 49}).json()
    wbc = next(m for m in day["metrics"] if m["metric"] == "white_blood_cells")
    temp = next(m for m in day["metrics"] if m["metric"] == "body_temperature")
    assert wbc["lastMeasuredDay"] == 49 and wbc["status"] == "out_of_range"
    assert temp["status"] == "out_of_range"


def test_api_today_also_keyed_by_metric(client):
    """The CosmoMedic app reads `values.<metric>.value/unit/status`."""
    day = client.get(f"{API}/astronauts/A002/today", params={"day": 49}).json()
    assert set(day["values"]) == {m["metric"] for m in day["metrics"]}
    temp = day["values"]["body_temperature"]
    assert temp["unit"] == "°C" and temp["status"] == "out_of_range" and temp["value"] >= 38
    assert day["summary"].startswith("BP ") and "°C" in day["summary"]


def test_api_daily_records_lead_with_day_status_and_summary(client):
    records = client.get(f"{API}/astronauts/A004/daily", params={"fromDay": 174, "toDay": 180}).json()
    assert len(records) == 7
    assert list(records[0])[:3] == ["missionDay", "status", "summary"]
    assert "sleep" in records[0]["summary"] and records[0]["values"]["sleep_hours"] is not None


def test_api_baseline_is_an_object(client):
    b = client.get(f"{API}/astronauts/A001/baseline").json()
    assert (b["astronautId"], b["missionId"], b["fromDay"], b["toDay"]) == ("A001", "SYN-LDM-01", -30, -1)
    assert b["provenance"] == "SIMULATED_TELEMETRY"
    sbp = next(m for m in b["metrics"] if m["metric"] == "systolic_bp")
    assert b["values"]["systolic_bp"] == sbp["mean"] and sbp["n"] == 30
    assert b["summary"].startswith("BP ")


def test_api_series_events_and_export(client):
    series = client.get(f"{API}/metrics/systolic_bp/series", params={"fromDay": 0, "toDay": 29}).json()
    assert len(series["series"]) == 4 and len(series["crewMean"]) == 30
    events = client.get(f"{API}/events").json()
    assert {e["astronautId"] for e in events} == {"A001", "A002", "A003", "A004"}
    csv = client.get(f"{API}/export.csv").text.strip().splitlines()
    assert len(csv) == 845
    assert csv[0].startswith("astronaut_id,mission_id,mission_day")


def test_api_errors(client):
    assert client.get(f"{API}/astronauts/A009/daily").status_code == 404
    assert client.get(f"{API}/dashboard", params={"day": 999}).status_code == 422
    assert client.get(f"{API}/metrics/nope/series").status_code == 404
    assert client.get("/api/v1/missions/NOPE").status_code == 404


def test_api_regenerate(client, auth):
    before = client.get(f"{API}/astronauts/A001/daily", params={"fromDay": 0, "toDay": 0}).json()
    assert client.post("/api/v1/missions/regenerate", params={"seed": 42}).status_code == 403  # surgeon
    regenerated = client.post("/api/v1/missions/regenerate", params={"seed": 42}, headers=auth("admin"))
    assert regenerated.json()["seed"] == 42
    after = client.get(f"{API}/astronauts/A001/daily", params={"fromDay": 0, "toDay": 0}).json()
    assert before == after
