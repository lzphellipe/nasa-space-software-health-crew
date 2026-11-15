
"""API endpoint for questions about internal documents."""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.agent_service import ask_health_agent


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/agent", tags=["agent"])


class AgentQuestion(BaseModel):
    question: str = Field(min_length=3, max_length=1500)


class AgentAnswer(BaseModel):
    answer: str


@router.post("/ask", response_model=AgentAnswer)
def ask_agent(payload: AgentQuestion) -> AgentAnswer:
    """
    Process a question using the internal PDF and CrewAI agent.

    Parameters
    ----------
    payload : AgentQuestion
        Request containing the user's question.

    Returns
    -------
    AgentAnswer
        Response containing the agent's answer.
    """
    try:
        return AgentAnswer(answer=ask_health_agent(payload.question))

    except FileNotFoundError:
        raise HTTPException(
            status_code=503,
            detail="Internal PDF document is not configured."
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc)
        )

    except Exception:
        logger.exception("Failed to execute the health agent")
        raise HTTPException(
            status_code=502,
            detail="AI service execution failed."
        )
