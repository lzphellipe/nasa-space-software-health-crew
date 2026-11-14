"""The OSDR studies and files this API ingests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MISSION = "SpaceX Inspiration4"


@dataclass(frozen=True)
class StudySpec:
    id: str
    number: int
    title: str

    @property
    def url(self) -> str:
        return f"https://osdr.nasa.gov/bio/repo/data/studies/{self.id}"


@dataclass(frozen=True)
class DatasetSpec:
    key: str
    study: StudySpec
    panel: str
    panel_name: str
    specimen: str
    file_name: str
    # "range": <analyte>_value_<unit>, <analyte>_range_min_<unit>, <analyte>_range_max_<unit>
    # "concentration": <analyte>_concentration_<unit>, <analyte>_percent
    layout: Literal["range", "concentration"]


OSD_569 = StudySpec(
    id="OSD-569",
    number=569,
    title=(
        "Whole Blood Measurements from the SpaceX Inspiration4 Mission "
        "(RNA-seq, epitranscriptome, WGS, clonal hematopoiesis, CBC)"
    ),
)
OSD_575 = StudySpec(
    id="OSD-575",
    number=575,
    title=(
        "SpaceX Inspiration4 Blood Serum Metabolic Panel and Immune/Cardiac Cytokine Arrays "
        "(Comprehensive Metabolic Panel and Multiplex)"
    ),
)

STUDIES = [OSD_569, OSD_575]

DATASETS = [
    DatasetSpec(
        key="cbc",
        study=OSD_569,
        panel="cbc",
        panel_name="Complete Blood Count",
        specimen="whole-blood",
        file_name="LSDS-7_Complete_Blood_Count_CBC_TRANSFORMED.csv",
        layout="range",
    ),
    DatasetSpec(
        key="cmp",
        study=OSD_575,
        panel="cmp",
        panel_name="Comprehensive Metabolic Panel",
        specimen="serum",
        file_name="LSDS-8_Comprehensive_Metabolic_Panel_CMP_TRANSFORMED.csv",
        layout="range",
    ),
    DatasetSpec(
        key="cardio",
        study=OSD_575,
        panel="cardio",
        panel_name="Multiplex Serum Cardiovascular Panel (Eve Technologies)",
        specimen="serum",
        file_name="LSDS-8_Multiplex_serum_cardiovascular_EvePanel_TRANSFORMED.csv",
        layout="concentration",
    ),
]

DATASETS_BY_KEY = {d.key: d for d in DATASETS}
