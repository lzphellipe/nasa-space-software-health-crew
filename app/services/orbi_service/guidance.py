
"""Select health guidance based on relevant metrics and scientific passages."""

from __future__ import annotations

from app.services.knowledge import tokenize
from app.services.orbi_service.insights import PAGE_METRICS


GUIDANCE_RULES = {
    "sleep": [
        {
            "actionId": "sleep_environment",
            "action": (
                "Consider whether lighting or changes in your sleep "
                "environment may be affecting your rest, and discuss "
                "persistent difficulties with your crew health team."
            ),
            "requiredTerms": [
                ["sleep", "circadian"],
                ["light", "lighting"],
            ],
        },
        {
            "actionId": "sleep_schedule",
            "action": (
                "Review whether changes in your sleep schedule may be "
                "affecting your rest, and share any persistent "
                "difficulties with your crew health team."
            ),
            "requiredTerms": [
                ["sleep", "circadian"],
                ["schedule", "shifting"],
            ],
        },
    ],
    "mood": [
        {
            "actionId": "behavioral_support",
            "action": (
                "Consider discussing persistent changes in your "
                "emotional well-being with your crew behavioral "
                "health support team."
            ),
            "requiredTerms": [
                ["behavioral", "psychological", "mental"],
                ["support", "health", "care"],
            ],
        },
    ],
    "body": [
        {
            "actionId": "medical_review",
            "action": (
                "Share persistent changes in your physical "
                "measurements with your crew medical team "
                "for an appropriate review."
            ),
            "requiredTerms": [
                ["medical", "health"],
                ["monitoring", "assessment", "evaluation"],
            ],
        },
    ],
    "meals": [
        {
            "actionId": "nutrition_review",
            "action": (
                "Review your recent food intake with your crew "
                "health team if you have concerns about meeting "
                "your nutritional needs."
            ),
            "requiredTerms": [
                ["nutrition", "nutritional"],
                ["intake", "requirements", "needs"],
            ],
        },
    ],
    "exercise": [
        {
            "actionId": "exercise_review",
            "action": (
                "Review your exercise routine with the crew team "
                "responsible for physical countermeasures before "
                "making changes to your activity plan."
            ),
            "requiredTerms": [
                ["exercise"],
                ["countermeasure", "countermeasures"],
            ],
        },
    ],
    "radiation": [
        {
            "actionId": "radiation_review",
            "action": (
                "Review any concerns about recorded radiation "
                "exposure with the crew radiation safety team."
            ),
            "requiredTerms": [
                ["radiation"],
                ["monitoring", "protection", "exposure"],
            ],
        },
    ],
}


def get_relevant_categories(insight: dict) -> set[str]:
    """Identify health categories with prioritized measurements.

    Parameters
    ----------
    insight : dict
        Statistical health analysis containing metric priorities.

    Returns
    -------
    set[str]
        Categories containing at least one prioritized metric.
    """
    relevant_metrics = {
        metric["metric"]
        for metric in insight.get("metrics", [])
        if metric.get("priority", 0) > 0
        and metric.get("metric")
    }

    return {
        category
        for category, metric_codes in PAGE_METRICS.items()
        if category != "home"
        and any(
            code in relevant_metrics
            for code in metric_codes
        )
    }


def select_supported_guidance(
    page: str,
    scientific_evidence: list[dict],
    relevant_categories: set[str] | None = None,
) -> list[dict]:
    """Select guidance candidates for relevant health categories.

    Parameters
    ----------
    page : str
        Requested Orbi page.
    scientific_evidence : list[dict]
        Retrieved scientific passages and source references.
    relevant_categories : set[str] or None
        Prioritized categories for the home overview.

    Returns
    -------
    list[dict]
        Guidance candidates with associated scientific references.

    Notes
    -----
    Keyword matching is a preliminary relevance check and does not
    independently establish clinical validity.
    """
    if page == "home":
        categories = [
            category
            for category in PAGE_METRICS
            if category != "home"
            and relevant_categories is not None
            and category in relevant_categories
        ]
    else:
        categories = [page]

    supported = []

    for category in categories:
        for rule in GUIDANCE_RULES.get(category, []):
            for source in scientific_evidence:
                excerpt = source.get("excerpt", "")
                source_id = source.get("sourceId")

                if not excerpt or not source_id:
                    continue

                passage_terms = set(tokenize(excerpt))

                matches = all(
                    any(
                        term in passage_terms
                        for term in group
                    )
                    for group in rule["requiredTerms"]
                )

                if not matches:
                    continue

                supported.append(
                    {
                        "actionId": rule["actionId"],
                        "action": rule["action"],
                        "category": category,
                        "sourceId": source_id,
                    }
                )

                break

    return supported
