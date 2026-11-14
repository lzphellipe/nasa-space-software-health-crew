"""Parse OSDR LSDS "TRANSFORMED" CSVs into long-form records.

Handles the quirks seen in the published files:
* UTF-8 BOM on the first header;
* missing values written as "", "NA" or "N/A" (e.g. eGFR has no upper limit);
* inconsistent range columns (``alt_max_units_per_liter`` has no ``range_``);
* unit-less ratio analytes (``albumin_to_globulin_ratio_value``).
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

MISSING_TOKENS = {"", "na", "n/a", "nan", "null", "none", "-", "nd"}

SAMPLE_RE = re.compile(
    r"^(?P<crew>C\d{3})_(?P<specimen>[A-Za-z\-]+)_(?P<timepoint>[A-Z]{1,2}[+-]?\d+)(?:_(?P<suffix>\w+))?$"
)
TIMEPOINT_RE = re.compile(r"^(?P<prefix>[A-Z]{1,2})(?P<sign>[+-]?)(?P<days>\d+)$")


@dataclass(frozen=True)
class SampleKey:
    sample_name: str
    crew_id: str
    specimen: str
    timepoint: str


@dataclass
class ParsedMeasurement:
    sample: SampleKey
    analyte_code: str
    unit_slug: str | None
    value: float | None
    ref_min: float | None = None
    ref_max: float | None = None
    relative_percent: float | None = None


@dataclass
class ParseResult:
    measurements: list[ParsedMeasurement] = field(default_factory=list)
    samples: list[SampleKey] = field(default_factory=list)
    analytes: dict[str, str | None] = field(default_factory=dict)  # code -> unit slug
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ParsedTimepoint:
    code: str
    phase: str
    offset_days: int


def to_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    text = raw.strip()
    if text.lower() in MISSING_TOKENS:
        return None
    text = text.replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def parse_sample_name(name: str) -> SampleKey | None:
    match = SAMPLE_RE.match(name.strip())
    if not match:
        return None
    return SampleKey(
        sample_name=name.strip(),
        crew_id=match["crew"],
        specimen=match["specimen"].lower(),
        timepoint=match["timepoint"],
    )


def parse_timepoint(code: str) -> ParsedTimepoint:
    """``L-92`` = 92 days before launch, ``R+1`` = 1 day after return, ``FD2`` = flight day 2."""
    match = TIMEPOINT_RE.match(code)
    if not match:
        raise ValueError(f"Unrecognized timepoint code: {code!r}")
    prefix, sign, days = match["prefix"], match["sign"], int(match["days"])
    offset = -days if sign == "-" else days
    if prefix == "L":
        return ParsedTimepoint(code, "preflight", offset)
    if prefix == "R":
        return ParsedTimepoint(code, "postflight", offset)
    return ParsedTimepoint(code, "inflight", offset)


def _read_rows(text: str) -> tuple[list[str], list[dict[str, str]]]:
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    headers = [h.strip().lstrip("﻿") for h in (reader.fieldnames or [])]
    reader.fieldnames = headers
    rows = [row for row in reader if any((v or "").strip() for v in row.values())]
    return headers, rows


def _range_layout(headers: list[str]) -> tuple[dict[str, tuple[str, str | None]], dict[str, dict[str, str]], set[str]]:
    """Map value columns and their min/max columns; return the columns left unmatched."""
    value_cols: dict[str, tuple[str, str | None]] = {}
    for col in headers:
        if "_value" in col:
            code, _, unit = col.partition("_value")
            value_cols[col] = (code, unit.lstrip("_") or None)

    used = set(value_cols)
    ranges: dict[str, dict[str, str]] = {}
    # Longest code first so e.g. "absolute_basophils" never steals "basophils" columns.
    codes = sorted({code for code, _ in value_cols.values()}, key=len, reverse=True)
    for code in codes:
        ranges[code] = {}
        for col in headers:
            if col in used:
                continue
            for bound in ("min", "max"):
                if col.startswith(f"{code}_range_{bound}") or col.startswith(f"{code}_{bound}"):
                    ranges[code][bound] = col
                    used.add(col)
    return value_cols, ranges, used


def parse_csv(text: str, layout: str) -> ParseResult:
    headers, rows = _read_rows(text)
    if not headers or headers[0].lower() != "sample name":
        raise ValueError("Expected the first column to be 'Sample Name'")
    data_cols = headers[1:]
    result = ParseResult()

    if layout == "range":
        value_cols, ranges, used = _range_layout(data_cols)
        for col, (code, unit) in value_cols.items():
            result.analytes[code] = unit
    elif layout == "concentration":
        conc_cols: dict[str, tuple[str, str | None]] = {}
        pct_cols: dict[str, str] = {}
        for col in data_cols:
            if "_concentration" in col:
                code, _, unit = col.partition("_concentration")
                conc_cols[col] = (code, unit.lstrip("_") or None)
            elif col.endswith("_percent"):
                pct_cols[col[: -len("_percent")]] = col
        used = set(conc_cols) | set(pct_cols.values())
        for col, (code, unit) in conc_cols.items():
            result.analytes[code] = unit
    else:
        raise ValueError(f"Unknown layout {layout!r}")

    unused = [c for c in data_cols if c not in used]
    if unused:
        result.warnings.append(f"Ignored columns with an unrecognized pattern: {', '.join(unused)}")

    for row in rows:
        name = (row.get("Sample Name") or "").strip()
        key = parse_sample_name(name)
        if key is None:
            result.warnings.append(f"Skipped row with unrecognized sample name: {name!r}")
            continue
        result.samples.append(key)

        if layout == "range":
            for col, (code, unit) in value_cols.items():
                bounds = ranges.get(code, {})
                result.measurements.append(
                    ParsedMeasurement(
                        sample=key,
                        analyte_code=code,
                        unit_slug=unit,
                        value=to_float(row.get(col)),
                        ref_min=to_float(row.get(bounds["min"])) if "min" in bounds else None,
                        ref_max=to_float(row.get(bounds["max"])) if "max" in bounds else None,
                    )
                )
        else:
            for col, (code, unit) in conc_cols.items():
                pct_col = pct_cols.get(code)
                result.measurements.append(
                    ParsedMeasurement(
                        sample=key,
                        analyte_code=code,
                        unit_slug=unit,
                        value=to_float(row.get(col)),
                        relative_percent=to_float(row.get(pct_col)) if pct_col else None,
                    )
                )

    return result
