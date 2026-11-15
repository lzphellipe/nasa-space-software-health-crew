import httpx
import pytest

from app.analog import agbresa
from app.config import SEED_DIR, get_settings
from app.services.analog_ingest import AnalogFetchError, download, fetch

API = "/api/v1/analog/agbresa"
S1 = (SEED_DIR / "agbresa" / "pone.0239228.s001.xlsx").read_bytes()
S2 = (SEED_DIR / "agbresa" / "pone.0239228.s002.xlsx").read_bytes()


@pytest.fixture(scope="module")
def parsed():
    return agbresa.parse_s1(S1), agbresa.parse_s2(S2)


def test_subjects_groups_and_sex(parsed):
    s1, s2 = parsed
    assert len(s1.subjects) == 24 and len(s2.subjects) == 16
    groups = [s.group for s in s1.subjects.values()]
    assert {g: groups.count(g) for g in set(groups)} == {"intermittent": 8, "continuous": 8, "control": 8}
    sexes = [s.sex for s in s1.subjects.values()]
    assert sexes.count("male") == 16 and sexes.count("female") == 8  # as reported in the paper
    for code, subject in s2.subjects.items():  # same volunteer, same coding in both files
        assert (subject.group, subject.sex) == (s1.subjects[code].group, s1.subjects[code].sex)


def test_group_decoding_reproduces_table_1_of_the_paper(parsed):
    _, s2 = parsed
    paper = {  # Frett et al. 2020, Table 1: heart rate during centrifugation
        ("intermittent", 1): 80.3, ("intermittent", 2): 86.3, ("intermittent", 3): 86.9,
        ("continuous", 1): 82.4, ("continuous", 2): 99.13, ("continuous", 3): 100.5,
    }
    for (group, idx), expected in paper.items():
        values = [
            o.value for o in s2.observations
            if o.metric == "heart_rate" and o.moment == "during" and o.session_index == idx
            and s2.subjects[o.subject_code].group == group
        ]
        assert len(values) == 8
        assert sum(values) / 8 == pytest.approx(expected, abs=0.15)


def test_impossible_values_are_withheld(parsed):
    s1, s2 = parsed
    invalid = {(o.subject_code, o.session, o.metric, o.moment, o.raw_value)
               for o in s1.observations + s2.observations if o.quality != "ok"}
    assert invalid == {
        ("Y", "HDT1", "systolic_bp", "during", 14.0),
        ("X", "HDT57", "ess", "pre", 25.0),
        ("Y", "HDT29", "msaq_peripheral", "post", 107.41),
        ("P", "HDT57", "panas_positive", "post", 9.0),
    }
    assert all(o.value is None for o in s1.observations + s2.observations if o.quality != "ok")


def test_control_group_has_one_weekly_value(parsed):
    s1, _ = parsed
    control = [o for o in s1.observations if s1.subjects[o.subject_code].group == "control"]
    assert control and {o.moment for o in control} == {"single"}


def test_status_rules():
    hr = agbresa.METRICS_BY_CODE["heart_rate"]
    assert agbresa.status_for(hr, "pre", 72, "ok") == "normal"
    assert agbresa.status_for(hr, "pre", 105, "ok") == "out_of_range"
    assert agbresa.status_for(hr, "during", 129, "ok") == "no_reference"  # centrifuge load, not resting
    ess = agbresa.METRICS_BY_CODE["ess"]
    assert agbresa.status_for(ess, "pre", 12, "ok") == "watch"
    assert agbresa.status_for(ess, "pre", None, "implausible") == "invalid"


def test_download_checks_for_xlsx():
    spec = agbresa.SOURCES[0]
    ok = httpx.MockTransport(lambda r: httpx.Response(200, content=S1))
    assert download(spec, 5, ok) == S1
    html = httpx.MockTransport(lambda r: httpx.Response(200, content=b"<html>error</html>"))
    with pytest.raises(AnalogFetchError):
        download(spec, 5, html)
    warnings = []
    content, origin, _ = fetch(spec, "auto", get_settings(), warnings, html)
    assert origin == "bundled" and content == S1 and warnings


def test_api_study_and_subjects(client):
    study = client.get(API).json()
    assert study["doi"] == "10.1371/journal.pone.0239228"
    assert {g["group"]: g["subjects"] for g in study["groups"]} == {"intermittent": 8, "continuous": 8, "control": 8}
    assert study["invalidValues"] == 4
    assert {f["datasetKey"] for f in study["files"]} == {"agbresa_s1", "agbresa_s2"}
    subjects = client.get(f"{API}/subjects", params={"group": "control"}).json()
    assert len(subjects) == 8


def test_api_subject_history(client):
    detail = client.get(f"{API}/subjects/Y").json()
    assert detail["subject"]["id"] == "AGB-Y"
    first = detail["sessions"][0]
    assert first["session"] == "HDT1"
    assert first["values"]["systolic_bp"]["during"] is None
    assert first["metricStatus"]["systolic_bp"]["during"] == "invalid"
    days = [s["bedRestDay"] for s in detail["sessions"]]
    assert days == sorted(days)
    assert client.get(f"{API}/subjects/NOPE").status_code == 404


def test_api_group_comparison_and_exports(client):
    cmp = client.get(f"{API}/groups/compare", params={"metric": "heart_rate", "moment": "during"}).json()
    continuous = next(g for g in cmp["groups"] if g["group"] == "continuous")
    assert [p["phase"] for p in continuous["points"]] == ["begin", "middle", "end"]
    assert continuous["points"][2]["mean"] == pytest.approx(100.5, abs=0.1)
    assert len(client.get(f"{API}/quality").json()) == 4
    assert len(client.get(f"{API}/export.csv").text.strip().splitlines()) == 862 + 432 + 1
    assert "AGBRESA" not in [s["id"] for s in client.get("/api/v1/dashboard/overview").json()["studies"]]
