"""One place to call the language model (Groq through litellm) for the CosmoMedic app.

Off unless ORBI_AI_ENABLED is on and GROQ_API_KEY is set. Every call has a timeout, never
raises (None means "use the rules"), and removes litellm's log filters from the server's
loggers afterwards (they once took the whole API down). Answers are cached by input.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections import OrderedDict
from threading import Lock

logger = logging.getLogger(__name__)

_CACHE: OrderedDict[str, str] = OrderedDict()
_CACHE_LOCK = Lock()
# Answers that read like medical advice are dropped; the rules answer instead.
OFF_LIMITS = re.compile(r"\b(diagnos\w*|prescri\w*|dosage|dose of|\d+\s?mg\b|milligram|"
                        r"you (have|might have|may have|probably have) (a |an )?([a-z]+ ){0,2}(disorder|disease|syndrome|condition))",
                        re.I)


def enabled() -> bool:
    return os.getenv("ORBI_AI_ENABLED", "true").lower() in ("1", "true", "yes") and bool(os.getenv("GROQ_API_KEY"))


def model() -> str:
    return os.getenv("AGENT_MODEL", "groq/llama-3.3-70b-versatile")


def complete(system: str, payload: dict, max_words: int, cache: bool = True, reasoning: str | None = "low") -> str | None:
    """The model's text for `payload` (sent as JSON), or None. `reasoning` applies to reasoning models (gpt-oss)."""
    if not enabled():
        return None
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    key = hashlib.sha256((model() + system + raw).encode()).hexdigest()
    if cache:
        with _CACHE_LOCK:
            if key in _CACHE:
                return _CACHE[key]
    from app.services.orbi_service import narrative as agent  # loads CrewAI/litellm once, safely

    try:
        if agent.load_crewai() is None:
            return None
        import litellm

        extra = {"reasoning_effort": reasoning} if reasoning and "gpt-oss" in model() else {}
        resp = litellm.completion(model=model(), temperature=0.3, timeout=agent.LLM_TIMEOUT_SECONDS, max_tokens=1500,
                                  messages=[{"role": "system", "content": system}, {"role": "user", "content": raw}],
                                  **extra)
        text = (resp.choices[0].message.content or "").strip()
    except Exception:
        logger.exception("Language model call failed; using the rules")
        return None
    finally:
        agent.detach_litellm_log_filters()
    text = re.sub(r"\s+", " ", text).strip().strip('"')
    if not text or len(text.split()) > max_words * 1.5 or OFF_LIMITS.search(text):
        return None
    if cache:
        with _CACHE_LOCK:
            _CACHE[key] = text
            if len(_CACHE) > 512:
                _CACHE.popitem(last=False)
    return text
