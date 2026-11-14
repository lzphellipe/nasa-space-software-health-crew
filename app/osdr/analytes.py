"""Human-readable catalog for the analytes found in the OSDR files.

Column names in the TRANSFORMED CSVs carry the analyte and the unit as slugs
(e.g. ``mchc_value_grams_per_deciliter``). This module turns them into display
names, readable units and body-system categories the apps can group by.
Columns not listed here are still ingested, with a generated name and the
"other" category, so a new file version never breaks ingestion.
"""

from __future__ import annotations

from dataclasses import dataclass

CATEGORIES: dict[str, str] = {
    "immune": "Immune / white blood cells",
    "oxygen_transport": "Red blood cells / oxygen transport",
    "coagulation": "Platelets & coagulation",
    "kidney": "Kidney function",
    "liver": "Liver & proteins",
    "electrolytes": "Electrolytes & minerals",
    "metabolism": "Metabolism",
    "inflammation": "Inflammation & acute phase",
    "vascular": "Vascular & endothelial",
    "other": "Other",
}

UNIT_SLUGS: dict[str, str] = {
    "cells_per_microliter": "cells/µL",
    "percent": "%",
    "picogram": "pg",
    "grams_per_deciliter": "g/dL",
    "gram_per_deciliter": "g/dL",
    "femtoliter": "fL",
    "thousand_per_microliter": "×10³/µL",
    "million_per_microliter": "×10⁶/µL",
    "units_per_liter": "U/L",
    "milligram_per_deciliter": "mg/dL",
    "millimol_per_liter": "mmol/L",
    "milliliter_per_minute_per_1.73_meter_squared": "mL/min/1.73m²",
    "nanogram_per_milliliter": "ng/mL",
    "picogram_per_milliliter": "pg/mL",
}


@dataclass(frozen=True)
class AnalyteInfo:
    name: str
    category: str
    description: str | None = None
    unit_override: str | None = None
    unit_note: str | None = None


CATALOG: dict[str, AnalyteInfo] = {
    # --- Complete Blood Count (OSD-569) -----------------------------------
    "white_blood_cell_count": AnalyteInfo("White blood cells (WBC)", "immune", "Total leukocyte count."),
    "absolute_neutrophils": AnalyteInfo("Neutrophils (absolute)", "immune"),
    "absolute_lymphocytes": AnalyteInfo("Lymphocytes (absolute)", "immune"),
    "absolute_monocytes": AnalyteInfo("Monocytes (absolute)", "immune"),
    "absolute_eosinophils": AnalyteInfo("Eosinophils (absolute)", "immune"),
    "absolute_basophils": AnalyteInfo("Basophils (absolute)", "immune"),
    "neutrophils": AnalyteInfo("Neutrophils (%)", "immune"),
    "lymphocytes": AnalyteInfo("Lymphocytes (%)", "immune"),
    "monocytes": AnalyteInfo("Monocytes (%)", "immune"),
    "eosinophils": AnalyteInfo("Eosinophils (%)", "immune"),
    "basophils": AnalyteInfo("Basophils (%)", "immune"),
    "red_blood_cell_count": AnalyteInfo("Red blood cells (RBC)", "oxygen_transport"),
    "hemoglobin": AnalyteInfo(
        "Hemoglobin",
        "oxygen_transport",
        unit_override="g/dL",
        unit_note=(
            "The source column says 'percent', but values (≈14) and the reference "
            "range (13.2–17.1) are hemoglobin in g/dL."
        ),
    ),
    "hematocrit": AnalyteInfo("Hematocrit", "oxygen_transport", "Share of blood volume made of red cells."),
    "mcv": AnalyteInfo("Mean corpuscular volume (MCV)", "oxygen_transport"),
    "mch": AnalyteInfo("Mean corpuscular hemoglobin (MCH)", "oxygen_transport"),
    "mchc": AnalyteInfo("Mean corpuscular hemoglobin concentration (MCHC)", "oxygen_transport"),
    "rdw": AnalyteInfo("Red cell distribution width (RDW)", "oxygen_transport"),
    "platelet_count": AnalyteInfo("Platelets", "coagulation"),
    "mpv": AnalyteInfo("Mean platelet volume (MPV)", "coagulation"),
    # --- Comprehensive Metabolic Panel (OSD-575) --------------------------
    "glucose": AnalyteInfo("Glucose", "metabolism"),
    "urea_nitrogen_bun": AnalyteInfo("Blood urea nitrogen (BUN)", "kidney"),
    "creatinine": AnalyteInfo("Creatinine", "kidney"),
    "bun_to_creatinine_ratio": AnalyteInfo("BUN / creatinine ratio", "kidney"),
    "egfr_non_african_american": AnalyteInfo(
        "eGFR (non-African American)",
        "kidney",
        "Estimated glomerular filtration rate, as reported by the lab.",
        unit_note="Race-based eGFR equations have since been replaced in clinical practice (CKD-EPI 2021).",
    ),
    "egfr_african_american": AnalyteInfo(
        "eGFR (African American)",
        "kidney",
        "Estimated glomerular filtration rate, as reported by the lab.",
        unit_note="Race-based eGFR equations have since been replaced in clinical practice (CKD-EPI 2021).",
    ),
    "sodium": AnalyteInfo("Sodium", "electrolytes"),
    "potassium": AnalyteInfo("Potassium", "electrolytes"),
    "chloride": AnalyteInfo("Chloride", "electrolytes"),
    "carbon_dioxide": AnalyteInfo("Carbon dioxide (bicarbonate)", "electrolytes"),
    "calcium": AnalyteInfo("Calcium", "electrolytes", "Relevant to bone loss in microgravity."),
    "total_protein": AnalyteInfo("Total protein", "liver"),
    "albumin": AnalyteInfo("Albumin", "liver"),
    "globulin": AnalyteInfo("Globulin", "liver"),
    "albumin_to_globulin_ratio": AnalyteInfo("Albumin / globulin ratio", "liver"),
    "total_bilirubin": AnalyteInfo("Total bilirubin", "liver"),
    "alkaline_phosphatase": AnalyteInfo("Alkaline phosphatase (ALP)", "liver"),
    "alt": AnalyteInfo("Alanine aminotransferase (ALT)", "liver"),
    "ast": AnalyteInfo("Aspartate aminotransferase (AST)", "liver"),
    # --- Multiplex cardiovascular panel (OSD-575) -------------------------
    "crp": AnalyteInfo("C-reactive protein (CRP)", "inflammation", "Acute-phase marker of inflammation."),
    "agp": AnalyteInfo("Alpha-1-acid glycoprotein (AGP)", "inflammation"),
    "haptoglobin": AnalyteInfo("Haptoglobin", "inflammation"),
    "sap": AnalyteInfo("Serum amyloid P (SAP)", "inflammation"),
    "a2_macroglobulin": AnalyteInfo("Alpha-2-macroglobulin", "inflammation"),
    "fetuin_a36": AnalyteInfo("Fetuin-A", "vascular", "Inhibitor of vascular calcification."),
    "fibrinogen": AnalyteInfo("Fibrinogen", "coagulation"),
    "pf4": AnalyteInfo("Platelet factor 4 (PF4)", "coagulation"),
    "l_selectin": AnalyteInfo("L-selectin", "vascular", "Leukocyte adhesion molecule."),
}

MULTIPLEX_NOTE = (
    "Research multiplex assay: no clinical reference range. Interpret against the "
    "crew member's own pre-flight baseline."
)


def readable_unit(slug: str | None) -> str:
    if not slug:
        return "ratio"
    return UNIT_SLUGS.get(slug, slug.replace("_per_", "/").replace("_", " "))


def describe(code: str, unit_slug: str | None) -> tuple[AnalyteInfo, str]:
    """Return catalog info and the display unit for an analyte code."""
    info = CATALOG.get(code) or AnalyteInfo(code.replace("_", " ").capitalize(), "other")
    unit = info.unit_override or readable_unit(unit_slug)
    return info, unit
