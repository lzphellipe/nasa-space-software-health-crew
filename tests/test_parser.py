import pytest

from app.config import SEED_DIR
from app.osdr.analytes import describe
from app.osdr.datasets import DATASETS_BY_KEY
from app.osdr.parser import parse_csv, parse_sample_name, parse_timepoint, to_float


def parse(key):
    spec = DATASETS_BY_KEY[key]
    return parse_csv((SEED_DIR / spec.file_name).read_text(encoding="utf-8-sig"), spec.layout)


@pytest.mark.parametrize("key, analytes", [("cbc", 20), ("cmp", 19), ("cardio", 9)])
def test_every_column_is_mapped(key, analytes):
    result = parse(key)
    assert len(result.samples) == 28
    assert len(result.analytes) == analytes
    assert len(result.measurements) == 28 * analytes
    assert result.warnings == []


def test_irregular_range_column_is_picked_up():
    alt = next(m for m in parse("cmp").measurements if m.analyte_code == "alt")
    assert (alt.ref_min, alt.ref_max) == (9.0, 46.0)


def test_na_upper_bound_becomes_none():
    egfr = next(m for m in parse("cmp").measurements if m.analyte_code == "egfr_non_african_american")
    assert egfr.ref_min == 60.0 and egfr.ref_max is None


def test_prefix_analytes_do_not_steal_columns():
    m = next(m for m in parse("cbc").measurements if m.analyte_code == "basophils")
    assert (m.ref_min, m.ref_max) == (0.0, 2.0)  # percent range, not the absolute 0–200


def test_multiplex_keeps_relative_percent():
    crp = parse("cardio").measurements[0]
    assert crp.relative_percent is not None and crp.ref_min is None


def test_hemoglobin_unit_is_corrected():
    _, unit = describe("hemoglobin", "percent")
    assert unit == "g/dL"


@pytest.mark.parametrize("raw, expected", [("", None), ("NA", None), ("N/A", None), ("1,200", 1200.0), ("4.2", 4.2)])
def test_to_float(raw, expected):
    assert to_float(raw) == expected


def test_sample_and_timepoint_names():
    key = parse_sample_name("C002_whole-blood_R+194_cbc")
    assert (key.crew_id, key.specimen, key.timepoint) == ("C002", "whole-blood", "R+194")
    assert parse_sample_name("not-a-sample") is None
    assert parse_timepoint("L-92").phase == "preflight" and parse_timepoint("L-92").offset_days == -92
    assert parse_timepoint("R+1").phase == "postflight"
