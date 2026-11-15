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
    # "concentration": <analyte>_concentration_<unit>, <analyte>_percent[_normalized_value]
    layout: Literal["range", "concentration"]
    # Research panels: hundreds of proteins without clinical ranges. They are stored and queryable
    # (by panel or analyte, catalog, CSV), but kept out of crew summaries, alerts and overviews.
    research: bool = False
    # Prefix for analyte codes, when a panel measures proteins another panel also measures.
    code_prefix: str = ""
    default_category: str = "other"
    # Values on a log2 scale (Alamar NPQ): change is judged in absolute units, not percent.
    log_scale: bool = False


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
    DatasetSpec(
        key="immune",
        study=OSD_575,
        panel="immune",
        panel_name="Multiplex Serum Immune Cytokine Panel (Eve Technologies HD71)",
        specimen="serum",
        file_name="LSDS-8_Multiplex_serum_immune_EvePanel_TRANSFORMED.csv",
        layout="concentration",
        research=True,
        default_category="cytokines",
    ),
    DatasetSpec(
        key="alamar",
        study=OSD_575,
        panel="alamar",
        panel_name="Multiplex Serum Inflammation Panel (Alamar NULISAseq, NPQ)",
        specimen="serum",
        file_name="LSDS-8_Multiplex_serum.immune.AlamarPanel_TRANSFORMED.csv",
        layout="concentration",
        research=True,
        code_prefix="alamar_",
        default_category="cytokines",
        log_scale=True,
    ),
]

DATASETS_BY_KEY = {d.key: d for d in DATASETS}
RESEARCH_PANELS = frozenset(d.panel for d in DATASETS if d.research)
LOG_SCALE_PANELS = frozenset(d.panel for d in DATASETS if d.log_scale)
