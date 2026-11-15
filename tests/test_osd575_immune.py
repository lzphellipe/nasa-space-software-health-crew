"""OSD-575 immune cytokine arrays (Eve HD71 and Alamar NULISAseq): ingested as research panels."""

from app.config import SEED_DIR
from app.osdr.analytes import protein_name
from app.osdr.datasets import DATASETS_BY_KEY, RESEARCH_PANELS
from app.osdr.parser import parse_csv

API = "/api/v1"


def parse(key):
    spec = DATASETS_BY_KEY[key]
    return parse_csv((SEED_DIR / spec.file_name).read_text(encoding="utf-8-sig"), spec.layout)


def test_parse_eve_immune_panel():
    result = parse("immune")
    assert len(result.samples) == 28 and len(result.analytes) == 71 and len(result.measurements) == 28 * 71
    assert result.analytes["il_6"] == "picogram_per_milliliter"
    assert not result.warnings
    m = next(x for x in result.measurements if x.analyte_code == "6ckine" and x.sample.sample_name == "C001_serum_L-3")
    assert m.value == 909.52 and round(m.relative_percent, 2) == 87.23


def test_parse_alamar_panel_sample_id_and_normalized_percent():
    result = parse("alamar")
    # The Alamar file names its id column "Sample ID" and has no C003 L-44 sample.
    assert len(result.samples) == 27 and len(result.analytes) == 203 and not result.warnings
    assert "C003_serum_L-44" not in {s.sample_name for s in result.samples}
    assert result.analytes["activin_a"] == "npq"
    assert all(m.relative_percent is not None for m in result.measurements[:50])


def test_protein_names():
    assert protein_name("il_1ra") == "IL-1RA"
    assert protein_name("tnfα") == "TNFα"
    assert protein_name("ifn_α2") == "IFN-α2"
    assert protein_name("ifna1;ifna13") == "IFNA1/IFNA13"


def test_research_panels_are_queryable(client):
    assert RESEARCH_PANELS == {"immune", "alamar"}
    eve = client.get(f"{API}/analytes", params={"panel": "immune"}).json()
    alamar = client.get(f"{API}/analytes", params={"panel": "alamar"}).json()
    assert len(eve) == 71 and len(alamar) == 203
    il6 = next(a for a in eve if a["code"] == "il_6")
    assert (il6["displayName"], il6["unit"], il6["category"], il6["hasReferenceRange"]) == ("IL-6", "pg/mL", "cytokines", False)
    # Alamar codes are prefixed: both panels measure EGF, LIF and TSLP.
    assert {"egf", "alamar_egf"} <= {a["code"] for a in eve + alamar}
    assert all(a["code"].startswith("alamar_") and a["unit"] == "NPQ (log2)" for a in alamar)
    assert "log2" in alamar[0]["unitNote"]
    page = client.get(f"{API}/measurements", params={"panel": "alamar", "limit": 1}).json()
    assert page["total"] == 27 * 203 and page["items"][0]["provenance"] == "REAL_RESEARCH_DATA"


def test_research_panels_stay_out_of_crew_summaries(client):
    assert all(c["counts"]["total"] == 48 for c in client.get(f"{API}/crew").json())
    assert client.get(f"{API}/crew/C001/summary").json()["counts"]["total"] == 48
    alerts = client.get(f"{API}/dashboard/alerts").json()
    assert not any(a["analyteCode"].startswith("alamar_") or a["analyteCode"] == "il_6" for a in alerts)
    overview = client.get(f"{API}/dashboard/overview").json()
    assert (overview["sampleCount"], overview["measurementCount"], overview["analyteCount"]) == (84, 1344, 48)
    panels = {p["panel"]: p for p in overview["researchPanels"]}
    assert panels["immune"]["analyteCount"] == 71 and panels["alamar"]["sampleCount"] == 27 and panels["alamar"]["logScale"]
    assert len(client.get(f"{API}/dashboard/heatmap").json()["analytes"]) == 48
    # ...but they are there when asked for by panel or analyte
    assert len(client.get(f"{API}/dashboard/heatmap", params={"panel": "immune"}).json()["analytes"]) == 71
    assert client.get(f"{API}/crew/C001/measurements", params={"panel": "immune", "timepoint": "R+1"}).json()
    series = client.get(f"{API}/crew/C002/analytes/alamar_il6/series").json()
    assert len(series["points"]) == 7 and series["baseline"]["n"] == 3


def test_log_scale_panel_uses_fold_change(client):
    rows = client.get(f"{API}/measurements", params={"panel": "alamar", "phase": "postflight", "limit": 2000}).json()["items"]
    for m in rows:
        if m["deltaAbs"] is None:
            continue
        assert abs(m["deltaPct"] - (2 ** m["deltaAbs"] - 1) * 100) < 1e-6
        if m["status"] == "watch":
            assert abs(m["deltaAbs"]) >= 1 and abs(m["zScore"]) >= 2


def test_existing_database_gets_new_datasets(client):
    from app import db
    from app.services.ingest import missing_datasets

    with db.SessionLocal() as session:
        assert missing_datasets(session) == []
