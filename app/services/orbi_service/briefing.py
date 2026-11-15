
"""Create medical briefings and serialize them as GZIP and FHIR R4."""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Astronaut, DailyRecord, Mission, SupportRequest
from app.report.report_models import MedicalBriefingSnapshot
from app.services.orbi_service.guidance import (
    get_relevant_categories,
    select_supported_guidance,
)
from app.services.orbi_service.home_context import get_home_context
from app.services.orbi_service.insights import build_insight
from app.services.orbi_service.knowledge import retrieve_scientific_evidence
from app.services.orbi_service.narrative import generate_narrative
from app.synthetic.metrics import METRICS

logger = logging.getLogger(__name__)

SCHEMA = "cosmomedic.medical-briefing.v2"
PAGES = ("home", "sleep", "mood", "body", "meals")

MAX_PACKET_BYTES = 2_000_000
MAX_DECODED_BYTES = 10_000_000


def _page_assessment(
    session: Session,
    mission: Mission,
    astronaut: Astronaut,
    day: int,
    page: str,
) -> dict:
    """Generate an objective medical assessment for one health category.

    Parameters
    ----------
    session : Session
        Active database session.
    mission : Mission
        Mission containing the astronaut.
    astronaut : Astronaut
        Astronaut whose measurements are being analyzed.
    day : int
        Mission day used as the analysis endpoint.
    page : str
        Health category to analyze.

    Returns
    -------
    dict
        Statistical findings and third-person medical interpretation.
    """
    analysis = build_insight(
        session,
        mission,
        astronaut,
        page,
        day,
    )

    metrics = analysis.get("metrics", [])

    if analysis.get("status") != "available":
        return {
            "page": page,
            "status": analysis.get("status"),
            "metrics": metrics,
            "headline": None,
            "insight": None,
            "suggestedAction": None,
            "actionKind": None,
            "sources": [],
            "aiGenerated": False,
            "messageSource": "data_status",
            "narrativeAudience": "medical",
        }

    categories = None

    if page == "home":
        analysis["homePatterns"] = get_home_context(
            session,
            mission_id=mission.id,
            astronaut_id=astronaut.id,
            mission_day=day,
        )

        categories = get_relevant_categories(analysis)

    try:
        evidence = retrieve_scientific_evidence(
            session,
            analysis,
        )

        guidance = select_supported_guidance(
            page,
            evidence,
            relevant_categories=categories,
        )

        narrative = generate_narrative(
            analysis,
            scientific_evidence=evidence,
            supported_guidance=guidance,
            audience="medical",
        )

    except Exception:
        logger.exception(
            "Medical narrative generation failed for %s",
            page,
        )

        narrative = {
            "headline": (
                "Health measurements for clinical review"
                if page == "home"
                else None
            ),
            "insight": (
                "Measurements are available for clinical review. "
                "An interpreted narrative could not be generated."
            ),
            "suggestedAction": None,
            "actionKind": None,
            "sources": [],
            "aiGenerated": False,
            "messageSource": "statistical_fallback",
        }

    return {
        "page": page,
        "status": analysis["status"],
        "metrics": metrics,
        "headline": narrative.get("headline"),
        "insight": narrative.get("insight"),
        "suggestedAction": narrative.get("suggestedAction"),
        "actionKind": narrative.get("actionKind"),
        "sources": narrative.get("sources", []),
        "aiGenerated": narrative.get("aiGenerated", False),
        "messageSource": narrative.get("messageSource"),
        "narrativeAudience": "medical",
    }


def _report_metadata(
    report_id: str,
    generated_at: str,
    mission: Mission,
    astronaut: Astronaut,
    day: int,
    start_day: int,
    request: SupportRequest,
) -> dict:
    """Build identification and management metadata for a medical report.

    Parameters
    ----------
    report_id : str
        Unique report identifier.
    generated_at : str
        Report generation timestamp in ISO 8601 format.
    mission : Mission
        Mission associated with the consultation.
    astronaut : Astronaut
        Astronaut associated with the consultation.
    day : int
        Mission day of the consultation.
    start_day : int
        First mission day included in the analysis.
    request : SupportRequest
        Confirmed medical consultation request.

    Returns
    -------
    dict
        Report identification, generation, and analysis-period metadata.
    """
    return {
        "reportId": report_id,
        "reportVersion": "1.0",
        "generatedAt": generated_at,
        "generatedBy": "Orbi AI - CosmoMedic",
        "astronautId": astronaut.id,
        "missionId": mission.id,
        "missionDay": day,
        "analysisPeriod": {
            "startDay": start_day,
            "endDay": day,
        },
        "requestId": request.id,
        "reasonForConsultation": request.message,
    }


def make_briefing(
    session: Session,
    mission: Mission,
    astronaut: Astronaut,
    day: int,
    request: SupportRequest,
    report_id: str,
) -> dict:
    """Generate a third-person medical briefing for a consultation.

    Parameters
    ----------
    session : Session
        Active database session.
    mission : Mission
        Mission associated with the consultation.
    astronaut : Astronaut
        Astronaut requesting medical support.
    day : int
        Mission day of the consultation.
    request : SupportRequest
        Confirmed consultation request.
    report_id : str
        Unique identifier assigned to the report.

    Returns
    -------
    dict
        Complete medical briefing containing report metadata,
        measurements, interpretations, and supporting guidance.
    """
    start = max(1, day - 13)

    records = session.scalars(
        select(DailyRecord)
        .where(
            DailyRecord.mission_id == mission.id,
            DailyRecord.astronaut_id == astronaut.id,
            DailyRecord.mission_day.between(start, day),
        )
        .order_by(DailyRecord.mission_day)
    ).all()

    observations = [
        {
            "metric": metric.code,
            "label": metric.label,
            "value": float(getattr(record, metric.code)),
            "unit": metric.unit,
            "missionDay": record.mission_day,
            "date": record.date.isoformat(),
            "screeningStatus": (
                record.metric_status or {}
            ).get(metric.code),
        }
        for record in records
        for metric in METRICS
        if getattr(record, metric.code, None) is not None
    ]

    assessments = {
        page: _page_assessment(
            session,
            mission,
            astronaut,
            day,
            page,
        )
        for page in PAGES
    }

    findings = []

    for metric in assessments["home"]["metrics"]:
        trend = metric.get("trend") or {}

        findings.append(
            {
                "metric": metric.get("metric"),
                "label": metric.get("label"),
                "baselineMean": metric.get("baselineMean"),
                "recentMean": metric.get("recentMean"),
                "trend": (
                    trend.get("direction")
                    if isinstance(trend, dict)
                    else None
                ),
                "persistence": metric.get("persistence"),
                "priority": metric.get("priority", 0),
                "dataStatus": metric.get("status"),
            }
        )

    findings.sort(
        key=lambda item: -item["priority"]
    )

    sections = []

    for page in PAGES:
        assessment = assessments[page]
        insight = assessment.get("insight")

        if not insight:
            continue

        sections.append(
            f"{page.upper()}: {insight}"
        )

        action = assessment.get("suggestedAction")

        if action:
            sections.append(
                f"Suggested wellness guidance: {action}"
            )

    interpretation = (
        "\n\n".join(sections)
        or "Insufficient data for an interpreted medical summary."
    )

    generated_at = datetime.now(
        timezone.utc
    ).isoformat()

    metadata = _report_metadata(
        report_id=report_id,
        generated_at=generated_at,
        mission=mission,
        astronaut=astronaut,
        day=day,
        start_day=start,
        request=request,
    )

    return {
        "schema": SCHEMA,
        "reportId": report_id,
        "version": 1,
        "generatedAt": generated_at,
        "requestId": request.id,
        "missionId": mission.id,
        "astronautId": astronaut.id,
        "missionDay": day,
        "window": {
            "startDay": start,
            "endDay": day,
        },
        "reason": request.message,
        "reportType": "AI_ASSISTED_PRE_CONSULTATION",
        "reportMetadata": metadata,
        "narrativeAudience": "medical",
        "overallAssessment": assessments["home"],
        "categoryAssessments": {
            page: assessments[page]
            for page in PAGES
            if page != "home"
        },
        "interpretation": interpretation,
        "findings": findings,
        "observations": observations,
        "provenance": (
            "SIMULATED_TELEMETRY"
            if mission.synthetic
            else "RECORDED_TELEMETRY"
        ),
        "clinicalValidation": "not_validated",
        "limitations": [
            "AI-generated text is not a diagnosis or clinical decision.",
            "Screening flags are illustrative, not NASA medical thresholds.",
            "Missing data must not be interpreted as normal findings.",
            "A statistical fallback may replace AI text when the model is unavailable.",
        ],
    }


def encode_packet(briefing: dict) -> bytes:
    """Serialize a versioned briefing to deterministic GZIP bytes.

    Parameters
    ----------
    briefing : dict
        Structured medical briefing.

    Returns
    -------
    bytes
        Compressed binary packet.
    """
    raw = json.dumps(
        briefing,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")

    return gzip.compress(
        raw,
        mtime=0,
    )


def decode_packet(payload: bytes) -> dict:
    """Decode and validate a compressed medical briefing.

    Parameters
    ----------
    payload : bytes
        GZIP-compressed medical briefing.

    Returns
    -------
    dict
        Decoded medical briefing.

    Raises
    ------
    ValueError
        If the packet is oversized or has an unsupported schema.
    """
    if len(payload) > MAX_PACKET_BYTES:
        raise ValueError(
            "Packet exceeds size limit"
        )

    with gzip.GzipFile(
        fileobj=io.BytesIO(payload)
    ) as stream:
        decoded = stream.read(
            MAX_DECODED_BYTES + 1
        )

    if len(decoded) > MAX_DECODED_BYTES:
        raise ValueError(
            "Decompressed packet exceeds size limit"
        )

    result = json.loads(decoded)

    if (
        not isinstance(result, dict)
        or result.get("schema") != SCHEMA
    ):
        raise ValueError(
            "Unsupported briefing schema"
        )

    required = (
        "reportId",
        "version",
        "generatedAt",
        "requestId",
        "missionId",
        "astronautId",
        "missionDay",
        "overallAssessment",
        "categoryAssessments",
        "observations",
        "interpretation",
    )

    if any(
        key not in result
        for key in required
    ):
        raise ValueError(
            "Incomplete medical briefing"
        )

    if not isinstance(
        result["observations"],
        list,
    ):
        raise ValueError(
            "Invalid observations"
        )

    return result


def create_snapshot(
    session: Session,
    mission: Mission,
    astronaut: Astronaut,
    day: int,
    request: SupportRequest,
) -> MedicalBriefingSnapshot:
    """Create the report snapshot within the caller's transaction.

    Parameters
    ----------
    session : Session
        Active database session.
    mission : Mission
        Mission associated with the consultation.
    astronaut : Astronaut
        Astronaut requesting medical support.
    day : int
        Mission day of the consultation.
    request : SupportRequest
        Consultation request already inserted into the session.

    Returns
    -------
    MedicalBriefingSnapshot
        Stored medical report snapshot.
    """
    report_id = str(uuid4())

    briefing = make_briefing(
        session,
        mission,
        astronaut,
        day,
        request,
        report_id,
    )

    packet = encode_packet(briefing)

    if len(packet) > MAX_PACKET_BYTES:
        raise ValueError(
            "Medical briefing exceeds packet size limit"
        )

    snapshot = MedicalBriefingSnapshot(
        report_id=report_id,
        request_id=request.id,
        version=1,
        schema_version=SCHEMA,
        sha256=hashlib.sha256(
            packet
        ).hexdigest(),
        packet=packet,
    )

    session.add(snapshot)
    session.flush()

    return snapshot


def verified_packet(
    snapshot: MedicalBriefingSnapshot,
) -> bytes:
    """Verify the stored packet before returning its original bytes.

    Parameters
    ----------
    snapshot : MedicalBriefingSnapshot
        Persisted report snapshot.

    Returns
    -------
    bytes
        Original compressed packet.

    Raises
    ------
    ValueError
        If the stored packet fails its integrity check.
    """
    packet = bytes(snapshot.packet)

    calculated_hash = hashlib.sha256(
        packet
    ).hexdigest()

    if calculated_hash != snapshot.sha256:
        raise ValueError(
            "Stored medical briefing failed integrity verification"
        )

    return packet


def to_fhir_bundle(briefing: dict) -> dict:
    """Convert the briefing into a preliminary FHIR R4 collection Bundle.

    Parameters
    ----------
    briefing : dict
        Decoded medical briefing with measurements and interpretations.

    Returns
    -------
    dict
        Preliminary FHIR R4 Bundle containing Patient,
        Observation, and DiagnosticReport resources.

    Notes
    -----
    This demonstration mapping requires clinical terminology
    and formal FHIR validation before production interoperability.
    """
    patient_id = briefing["astronautId"]
    report_id = briefing["reportId"]

    entries = [
        {
            "resource": {
                "resourceType": "Patient",
                "id": patient_id,
            }
        }
    ]

    references = []

    for index, observation in enumerate(
        briefing["observations"],
        start=1,
    ):
        observation_id = (
            f"orbi-{report_id}-{index}"
        )

        references.append(
            {
                "reference": (
                    f"Observation/{observation_id}"
                )
            }
        )

        entries.append(
            {
                "resource": {
                    "resourceType": "Observation",
                    "id": observation_id,
                    "status": "preliminary",
                    "code": {
                        "text": observation["label"],
                        "coding": [
                            {
                                "system": (
                                    "https://cosmomedic.example/"
                                    "CodeSystem/mission-metrics"
                                ),
                                "code": observation["metric"],
                                "display": observation["label"],
                            }
                        ],
                    },
                    "subject": {
                        "reference": (
                            f"Patient/{patient_id}"
                        )
                    },
                    "effectiveDateTime": (
                        f"{observation['date']}T12:00:00Z"
                    ),
                    "valueQuantity": {
                        "value": observation["value"],
                        "unit": observation["unit"],
                    },
                    "note": [
                        {
                            "text": (
                                f"Mission day {observation['missionDay']}; "
                                "illustrative screening: "
                                f"{observation.get('screeningStatus')}"
                            )
                        }
                    ],
                }
            }
        )

    attachment = {
        "reportMetadata": briefing.get("reportMetadata", {}),
        "narrativeAudience": briefing.get(
            "narrativeAudience",
            "medical",
        ),
        "overallAssessment": briefing["overallAssessment"],
        "categoryAssessments": briefing["categoryAssessments"],
        "findings": briefing["findings"],
        "limitations": briefing["limitations"],
        "provenance": briefing["provenance"],
    }

    encoded = base64.b64encode(
        json.dumps(
            attachment,
            ensure_ascii=False,
        ).encode("utf-8")
    ).decode("ascii")

    entries.append(
        {
            "resource": {
                "resourceType": "DiagnosticReport",
                "id": f"orbi-report-{report_id}",
                "status": "preliminary",
                "code": {
                    "text": "AI-assisted pre-consultation briefing"
                },
                "subject": {
                    "reference": f"Patient/{patient_id}"
                },
                "issued": briefing["generatedAt"],
                "conclusion": briefing["interpretation"],
                "result": references,
                "presentedForm": [
                    {
                        "contentType": "application/json",
                        "data": encoded,
                        "title": "Orbi interpreted medical briefing",
                    }
                ],
            }
        }
    )

    return {
        "resourceType": "Bundle",
        "type": "collection",
        "timestamp": briefing["generatedAt"],
        "entry": entries,
    }
