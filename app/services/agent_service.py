
"""Question-answering service for internal health protocols."""

from app.agents.health_agent import answer_with_agent
from app.agents.pdf_tools import search_pdf


def ask_health_agent(question: str) -> str:
    """
    Retrieve document context and generate an agent response.

    Parameters
    ----------
    question : str
        Question received from the API.

    Returns
    -------
    str
        Agent response or a message indicating insufficient evidence.
    """
    context = search_pdf(question)

    if not context:
        return "I could not find this information in the document."

    return answer_with_agent(question, context)
