"""AGBRESA (DLR/ESA/NASA 60-day head-down-tilt bed rest): files, metrics and parser.

Source: Frett T. et al. (2020) "Tolerability of daily intermittent or continuous
short-arm centrifugation during 60-day 6° head down bed rest (AGBRESA study)",
PLoS ONE 15(9): e0239228, supplementary files S1 and S2 (CC BY 4.0).

Bed rest is an Earth analog of some effects of microgravity. These are healthy
volunteers, not astronauts.

Codes in the spreadsheets are not documented in the paper. They were decoded
from the data:
* Intervention / Group: 1 = intermittent, 2 = continuous, 3 = control.
  Per-group means of S2 reproduce Table 1 of the paper exactly with this mapping.
* Gender: 1 = male, 2 = female (10 men and 6 women in the centrifuge groups,
  16 men and 8 women overall, as reported in the paper).
* Age_group: 1 or 2; the cut-off is not published.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

import openpyxl

STUDY_ID = "AGBRESA"
STUDY_TITLE = (
    "Tolerability of daily intermittent or continuous short-arm centrifugation during "
    "60-day 6° head down bed rest (AGBRESA study)"
)
STUDY_DOI = "10.1371/journal.pone.0239228"
STUDY_URL = "https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0239228"
STUDY_CONTEXT = "AGBRESA 60-day bed rest (Earth analog, DLR/ESA/NASA, 2019)"
DESIGN = (
    "24 healthy volunteers (16 men, 8 women) spent 60 days in strict 6° head-down bed rest at "
    "DLR :envihab, Cologne, in 2019. Two groups had daily artificial gravity on a short-arm "
    "centrifuge (+1 Gz at the center of mass): intermittent (6 × 5 min) or continuous (30 min); "
    "a control group had none. Blood pressure and heart rate were measured before, during and "
    "after centrifugation on bed rest days 1, 30 and 60; sleepiness (ESS), motion sickness (MSAQ) "
    "and mood (PANAS) questionnaires were taken weekly before and after centrifugation."
)

GROUPS = {1: "intermittent", 2: "continuous", 3: "control"}
SEXES = {1: "male", 2: "female"}
SESSION_PHASES = {1: "begin", 2: "middle", 3: "end"}


@dataclass(frozen=True)
class SourceSpec:
    key: str
    file_name: str
    title: str

    @property
    def url(self) -> str:
        suffix = self.file_name.rsplit(".", 2)[-2]  # "s001"
        return f"https://journals.plos.org/plosone/article/file?type=supplementary&id={STUDY_DOI}.{suffix}"


SOURCES = [
    SourceSpec("agbresa_s1", "pone.0239228.s001.xlsx", "S1: questionnaires ESS, MSAQ and PANAS"),
    SourceSpec("agbresa_s2", "pone.0239228.s002.xlsx", "S2: heart rate and blood pressure pre, during and post centrifugation"),
]


@dataclass(frozen=True)
class AnalogMetric:
    code: str
    label: str
    unit: str
    source: str  # agbresa_s1 | agbresa_s2
    plausible_min: float
    plausible_max: float
    description: str
    out_high: float | None = None
    out_low: float | None = None
    watch_high: float | None = None
    # Moments for which the thresholds apply (resting values only for BP/HR).
    threshold_moments: tuple[str, ...] = ()

    def thresholds(self) -> dict:
        return {
            k: v for k, v in (("outHigh", self.out_high), ("outLow", self.out_low), ("watchHigh", self.watch_high))
            if v is not None
        }


METRICS: list[AnalogMetric] = [
    AnalogMetric(
        "systolic_bp", "Systolic blood pressure", "mmHg", "agbresa_s2", 60, 250,
        "Measured before the centrifuge run, 2 minutes into it, and after it.",
        out_high=140, out_low=90, watch_high=130, threshold_moments=("pre", "post"),
    ),
    AnalogMetric(
        "diastolic_bp", "Diastolic blood pressure", "mmHg", "agbresa_s2", 30, 150,
        "Measured before the centrifuge run, 2 minutes into it, and after it.",
        out_high=90, out_low=60, watch_high=85, threshold_moments=("pre", "post"),
    ),
    AnalogMetric(
        "heart_rate", "Heart rate", "bpm", "agbresa_s2", 30, 220,
        "Measured before the centrifuge run, 2 minutes into it, and after it.",
        out_high=101, out_low=45, watch_high=90, threshold_moments=("pre", "post"),
    ),
    AnalogMetric(
        "ess", "Daytime sleepiness (Epworth Sleepiness Scale)", "0–24", "agbresa_s1", 0, 24,
        "Sum of 8 items scored 0–3; higher means sleepier. 0–10 normal, 11–15 mild to moderate, "
        "16–24 severe excessive daytime sleepiness.",
        out_high=16, watch_high=11, threshold_moments=("pre", "post"),
    ),
    AnalogMetric(
        "msaq_overall", "Motion sickness, overall (MSAQ)", "%", "agbresa_s1", 0, 100,
        "Motion Sickness Assessment Questionnaire, percent of the maximum score; higher means more "
        "symptoms. The lowest value in the file (no symptoms) is about 11 %.",
    ),
    AnalogMetric("msaq_gastrointestinal", "Motion sickness, gastrointestinal (MSAQ)", "%", "agbresa_s1", 0, 100, "MSAQ sub-score; floor ≈ 11 % in this file."),
    AnalogMetric("msaq_central", "Motion sickness, central (MSAQ)", "%", "agbresa_s1", 0, 100, "MSAQ sub-score; floor ≈ 8.9 % in this file."),
    AnalogMetric("msaq_peripheral", "Motion sickness, peripheral (MSAQ)", "%", "agbresa_s1", 0, 100, "MSAQ sub-score; floor ≈ 14.8 % in this file."),
    AnalogMetric("msaq_sopite", "Motion sickness, sopite-related (MSAQ)", "%", "agbresa_s1", 0, 100, "MSAQ sub-score (drowsiness, fatigue); floor ≈ 11 % in this file."),
    AnalogMetric(
        "panas_positive", "Positive affect (PANAS)", "10–50", "agbresa_s1", 10, 50,
        "Positive and Negative Affect Schedule; higher means more positive mood.",
    ),
    AnalogMetric(
        "panas_negative", "Negative affect (PANAS)", "10–50", "agbresa_s1", 10, 50,
        "Positive and Negative Affect Schedule; higher means more negative mood.",
    ),
]
METRICS_BY_CODE = {m.code: m for m in METRICS}

MOMENTS = {
    "pre": "Before the centrifuge run",
    "during": "2 minutes into the centrifuge run (plateau)",
    "post": "After the centrifuge run",
    "single": "Weekly assessment (control group, no centrifugation)",
}

S1_COLUMNS = {
    "ESS": "ess",
    "MSAQ_{m}_O": "msaq_overall",
    "MSAQ_{m}_G": "msaq_gastrointestinal",
    "MSAQ_{m}_C": "msaq_central",
    "MSAQ_{m}_P": "msaq_peripheral",
    "MSAQ_{m}_S": "msaq_sopite",
    "PANAS_{m}_P": "panas_positive",
    "PANAS_{m}_N": "panas_negative",
}
S2_COLUMNS = {
    ("SBP_SAHC_pre_run", "pre"): "systolic_bp",
    ("DBP_SAHC_pre_run", "pre"): "diastolic_bp",
    ("HR_SAHC_pre_run", "pre"): "heart_rate",
    ("SBP_peri_2min", "during"): "systolic_bp",
    ("DBP_peri_2min", "during"): "diastolic_bp",
    ("HR_peri_2min", "during"): "heart_rate",
    ("SBP_SAHC_post_run", "post"): "systolic_bp",
    ("DBP_SAHC_post_run", "post"): "diastolic_bp",
    ("HR_SAHC_post_run", "post"): "heart_rate",
}


@dataclass
class ParsedSubject:
    code: str
    group: str
    sex: str
    age_group: int | None
    campaign: str | None = None

    @property
    def id(self) -> str:
        return f"AGB-{self.code}"


@dataclass
class ParsedObservation:
    subject_code: str
    session: str
    bed_rest_day: int
    session_index: int
    month: str | None
    metric: str
    moment: str
    raw_value: float | None
    value: float | None
    quality: str  # ok | implausible
    quality_note: str | None


@dataclass
class ParsedFile:
    subjects: dict[str, ParsedSubject] = field(default_factory=dict)
    observations: list[ParsedObservation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rows: int = 0


def _rows(content: bytes) -> tuple[dict[str, int], list[tuple]]:
    ws = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True).worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    header = {str(h).strip(): i for i, h in enumerate(rows[0]) if h is not None}
    data = [r for r in rows[1:] if r and r[0] not in (None, "")]
    return header, data


def _num(v) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return None


def _int(v) -> int | None:
    n = _num(v)
    return int(n) if n is not None else None


def subject_code(raw: str) -> str:
    """S1 writes '0A', S2 writes 'A': same volunteer."""
    text = str(raw).strip()
    return text[1:] if len(text) > 1 and text[0] == "0" else text


def _day(session: str) -> int:
    match = re.search(r"(\d+)", session)
    if not match:
        raise ValueError(f"Unrecognized session {session!r}")
    return int(match.group(1))


def _check(metric: AnalogMetric, raw: float | None) -> tuple[float | None, str, str | None]:
    if raw is None:
        return None, "ok", None
    if not metric.plausible_min <= raw <= metric.plausible_max:
        return None, "implausible", (
            f"{raw:g} is outside the possible range of {metric.label} "
            f"({metric.plausible_min:g}–{metric.plausible_max:g}); value withheld"
        )
    return raw, "ok", None


def parse_s1(content: bytes) -> ParsedFile:
    h, data = _rows(content)
    out = ParsedFile(rows=len(data))
    for r in data:
        code = subject_code(r[h["Subject"]])
        group = GROUPS.get(_int(r[h["Group"]]))
        if group is None:
            out.warnings.append(f"S1: unknown group for subject {code}")
            continue
        out.subjects.setdefault(
            code,
            ParsedSubject(code, group, SEXES.get(_int(r[h["Gender"]]), "unknown"), _int(r[h["Age_group"]])),
        )
        session = str(r[h["HDT"]]).strip()
        date = str(r[h["Date"]] or "").strip().rstrip("-")
        for template, metric_code in S1_COLUMNS.items():
            metric = METRICS_BY_CODE[metric_code]
            if group == "control":
                # Controls had no centrifugation: one weekly value, copied into the
                # PRE and POST columns of the spreadsheet. Keep it once.
                column = template.format(m="POST") if "{m}" in template else f"{template}_POST"
                moments = [("single", column)]
            else:
                moments = [
                    (moment, template.format(m=moment.upper()) if "{m}" in template else f"{template}_{moment.upper()}")
                    for moment in ("pre", "post")
                ]
            for moment, column in moments:
                if column not in h:
                    continue
                raw = _num(r[h[column]])
                if raw is None:
                    continue
                value, quality, note = _check(metric, raw)
                out.observations.append(
                    ParsedObservation(
                        code, session, _day(session), _int(r[h["HDT_Number"]]) or 0, date or None,
                        metric_code, moment, raw, value, quality, note,
                    )
                )
    return out


def parse_s2(content: bytes) -> ParsedFile:
    h, data = _rows(content)
    out = ParsedFile(rows=len(data))
    session_index = {"HDT1": 1, "HDT30": 2, "HDT60": 3}
    for r in data:
        code = subject_code(r[h["Subject"]])
        group = GROUPS.get(_int(r[h["Intervention"]]))
        out.subjects.setdefault(
            code,
            ParsedSubject(
                code, group or "unknown", SEXES.get(_int(r[h["Gender"]]), "unknown"),
                _int(r[h["Age_Group"]]), str(r[h["Study_campaign"]] or "") or None,
            ),
        )
        session = str(r[h["Examination"]]).strip()
        day = _int(r[h["Day"]]) or _day(session)
        values: dict[tuple[str, str], tuple[float | None, str, str | None, float | None]] = {}
        for (column, moment), metric_code in S2_COLUMNS.items():
            raw = _num(r[h[column]])
            values[(metric_code, moment)] = (*_check(METRICS_BY_CODE[metric_code], raw), raw)
        # Systolic must exceed diastolic at the same moment.
        for moment in ("pre", "during", "post"):
            sbp, dbp = values[("systolic_bp", moment)], values[("diastolic_bp", moment)]
            if sbp[0] is not None and dbp[0] is not None and sbp[0] <= dbp[0]:
                values[("systolic_bp", moment)] = (None, "implausible", f"systolic {sbp[3]:g} ≤ diastolic {dbp[3]:g}; value withheld", sbp[3])
        for (metric_code, moment), (value, quality, note, raw) in values.items():
            if raw is None:
                continue
            out.observations.append(
                ParsedObservation(
                    code, session, day, session_index.get(session, 0), None,
                    metric_code, moment, raw, value, quality, note,
                )
            )
    return out


PARSERS = {"agbresa_s1": parse_s1, "agbresa_s2": parse_s2}


def status_for(metric: AnalogMetric, moment: str, value: float | None, quality: str) -> str:
    if quality == "implausible":
        return "invalid"
    if value is None:
        return "no_data"
    if moment not in metric.threshold_moments or not metric.thresholds():
        return "no_reference"
    if metric.out_high is not None and value >= metric.out_high:
        return "out_of_range"
    if metric.out_low is not None and value < metric.out_low:
        return "out_of_range"
    if metric.watch_high is not None and value >= metric.watch_high:
        return "watch"
    return "normal"
