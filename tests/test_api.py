from app.osdr.analytes import CATEGORIES

API = "/api/v1"


def test_health(client):
    assert client.get("/health").json() == {"status": "ok", "dataReady": True}


def test_overview(client):
    data = client.get(f"{API}/dashboard/overview").json()
    assert data["crewCount"] == 4
    assert data["sampleCount"] == 84
    assert data["measurementCount"] == 1344
    assert data["defaultTimepoint"] == "R+1"
    assert [t["timepoint"]["code"] for t in data["timepoints"]] == [
        "L-92", "L-44", "L-3", "R+1", "R+45", "R+82", "R+194"
    ]
    assert data["lastIngest"]["status"] == "success"


def test_crew_list_uses_camel_case(client):
    crew = client.get(f"{API}/crew").json()
    assert [c["id"] for c in crew] == ["C001", "C002", "C003", "C004"]
    assert {"displayName", "overallStatus", "counts"} <= crew[0].keys()
    assert crew[0]["counts"]["total"] == 48


def test_crew_summary_groups_by_category(client):
    data = client.get(f"{API}/crew/C004/summary", params={"timepoint": "R+1"}).json()
    assert data["timepoint"]["code"] == "R+1"
    assert {c["category"] for c in data["categories"]} <= set(CATEGORIES)
    assert sum(c["counts"]["total"] for c in data["categories"]) == 48
    severities = [a["severity"] for a in data["alerts"]]
    assert severities == sorted(severities, key=lambda s: s != "high")


def test_out_of_range_matches_reference(client):
    page = client.get(f"{API}/measurements", params={"status": "out_of_range", "limit": 2000}).json()
    assert page["total"] > 0
    for m in page["items"]:
        assert (m["refMin"] is not None and m["value"] < m["refMin"]) or (
            m["refMax"] is not None and m["value"] > m["refMax"]
        )


def test_preflight_is_never_watch(client):
    page = client.get(f"{API}/measurements", params={"phase": "preflight", "status": "watch"}).json()
    assert page["total"] == 0


def test_series_has_baseline(client):
    data = client.get(f"{API}/crew/C001/analytes/white_blood_cell_count/series").json()
    assert data["baseline"]["n"] == 3
    assert len(data["points"]) == 7
    days = [p["missionDay"] for p in data["points"]]
    assert days == sorted(days)


def test_heatmap_and_trend(client):
    heat = client.get(f"{API}/dashboard/heatmap", params={"panel": "cbc", "metric": "z_score"}).json()
    assert len(heat["cells"]) == 4 * 20
    trend = client.get(f"{API}/dashboard/analytes/crp/trend").json()
    assert len(trend["series"]) == 4
    assert trend["analyte"]["hasReferenceRange"] is False


def test_not_found(client):
    assert client.get(f"{API}/crew/C999/summary").status_code == 404
    assert client.get(f"{API}/crew/C001/analytes/unknown/series").status_code == 404
    assert client.get(f"{API}/crew", params={"timepoint": "Z+9"}).status_code == 404


def test_reingest_is_idempotent(client):
    run = client.post(f"{API}/ingest", params={"source": "bundled"}).json()
    assert run["status"] == "success"
    assert all(f["skipped"] for f in run["files"])
    forced = client.post(f"{API}/ingest", params={"source": "bundled", "force": True}).json()
    assert not any(f["skipped"] for f in forced["files"])
    assert client.get(f"{API}/dashboard/overview").json()["measurementCount"] == 1344


def test_export_csv(client):
    response = client.get(f"{API}/export/measurements.csv")
    assert response.headers["content-type"].startswith("text/csv")
    # 1,344 clinical + 1,988 Eve immune (28 x 71) + 5,481 Alamar (27 x 203) measurements, plus the header
    assert len(response.text.strip().splitlines()) == 8814
