"""Evidence-linked wellness reminders for Orbi."""
from __future__ import annotations
from app.services.knowledge import tokenize
from app.services.orbi_service.insights import PAGE_METRICS

RULES = {
    "sleep": [
        ("sleep_light", "Remember that light exposure influences your sleep-wake cycle. Keep your rest environment dim during scheduled sleep, following mission guidance.", (("sleep", "circadian"), ("light", "lighting"))),
        ("sleep_rhythm", "Your sleep-wake rhythm responds to light timing. Follow the lighting and rest schedule approved for your mission.", (("sleep", "circadian"), ("schedule", "rhythm"))),
    ],
    "mood": [
        ("mood_contact", "Planned social contact can support well-being during long missions. Consider using your next available private communication window.", (("behavioral", "psychological", "mental"), ("social", "communication", "support"))),
    ],
    "body": [
        ("body_consistency", "To compare measurements reliably, follow the same conditions specified in your mission's measurement protocol.", (("medical", "health"), ("monitoring", "measurement", "assessment"))),
    ],
    "meals": [
        ("meals_recording", "Check that your food log reflects what you consumed, rather than what was planned. This helps make intake trends more meaningful.", (("nutrition", "nutritional", "food"), ("intake", "consumption"))),
    ],
}


def get_relevant_categories(insight: dict) -> set[str]:
    """Identify categories with prioritized, available measurements."""
    relevant = {m["metric"] for m in insight.get("metrics", []) if m.get("priority", 0) > 0 and m.get("status") == "available"}
    return {page for page, codes in PAGE_METRICS.items() if page != "home" and any(code in relevant for code in codes)}


def select_supported_guidance(page: str, scientific_evidence: list[dict], relevant_categories: set[str] | None = None) -> list[dict]:
    """Match candidate reminders to passages; matching is not clinical validation."""
    categories = relevant_categories or set() if page == "home" else {page}
    candidates = []
    for category in categories:
        for action_id, action, groups in RULES.get(category, []):
            for source in scientific_evidence:
                terms = set(tokenize(source.get("excerpt", "")))
                if source.get("sourceId") and all(any(term in terms for term in group) for group in groups):
                    candidates.append({"actionId": action_id, "action": action, "category": category, "kind": "good_practice", "sourceId": source["sourceId"]})
                    break
    return candidates
