"""Loading the Orbi agent (CrewAI + litellm) must not leave litellm filters on the server's loggers.

On Azure, litellm's filters on the asyncio and uvicorn loggers failed inside every log call
after the agent was first used, and the whole API stopped answering.
"""

import logging

import pytest

from app.services.orbi_service import narrative

SERVER_LOGGERS = ("asyncio", "uvicorn.error", "uvicorn.access", "httpx")


def _litellm_filters(name):
    log = logging.getLogger(name)
    return [f for owner in [log, *log.handlers] for f in owner.filters if narrative._from_litellm(f)]


class _Boom(logging.Filter):
    """Stands for a litellm filter that raises, as on Azure."""

    def filter(self, record):
        raise KeyError("litellm")


def test_broken_litellm_filter_is_removed_from_server_loggers(monkeypatch):
    _Boom.__module__ = "litellm._logging"
    boom = _Boom()
    log = logging.getLogger("uvicorn.access")
    log.addFilter(boom)
    try:
        with pytest.raises(KeyError):
            log.info('%s - "%s %s HTTP/%s" %d', "1.2.3.4", "GET", "/health", "1.1", 200)
        assert narrative.detach_litellm_log_filters() >= 1
        log.info('%s - "%s %s HTTP/%s" %d', "1.2.3.4", "GET", "/health", "1.1", 200)
        assert boom not in log.filters
    finally:
        log.removeFilter(boom)


def test_loading_crewai_leaves_server_loggers_clean():
    pytest.importorskip("crewai")
    assert narrative.load_crewai() is not None
    crewai = narrative.load_crewai()
    crewai.LLM(model="groq/openai/gpt-oss-120b", timeout=narrative.LLM_TIMEOUT_SECONDS)  # loads litellm if not yet
    import sys

    assert "litellm" in sys.modules
    for name in SERVER_LOGGERS:
        assert _litellm_filters(name) == [], name
    # litellm keeps redaction off the server loggers even if it is imported again elsewhere
    import os

    assert os.environ.get("LITELLM_DISABLE_REDACT_SECRETS") == "true"


def test_api_keeps_answering_after_loading_the_agent(client):
    pytest.importorskip("crewai")
    narrative.load_crewai()
    for _ in range(3):
        assert client.get("/health").status_code == 200
