"""Demo accounts: one per synthetic astronaut, plus the care team."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.security import hash_password
from app.models import User

log = logging.getLogger(__name__)

DEMO_USERS = [
    ("astronaut1", "Astronaut 1 (Commander)", "astronaut", "A001"),
    ("astronaut2", "Astronaut 2 (Pilot)", "astronaut", "A002"),
    ("astronaut3", "Astronaut 3 (Mission Specialist 1)", "astronaut", "A003"),
    ("astronaut4", "Astronaut 4 (Mission Specialist 2)", "astronaut", "A004"),
    ("surgeon", "Flight Surgeon", "medical", None),
    ("admin", "Administrator", "admin", None),
]


def seed_demo_users(session: Session, password: str) -> int:
    created = 0
    for username, name, role, astronaut_id in DEMO_USERS:
        if session.scalar(select(User).where(User.username == username)) is None:
            session.add(
                User(
                    username=username, display_name=name, role=role,
                    astronaut_id=astronaut_id, password_hash=hash_password(password),
                )
            )
            created += 1
    session.commit()
    if created:
        log.warning("Created %d demo accounts with the DEMO_PASSWORD; change it outside a demo", created)
    return created
