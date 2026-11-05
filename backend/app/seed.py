"""Idempotent synthetic pilot seed data; existing business configuration is preserved."""

from datetime import time

from sqlalchemy.orm import Session

from app.config import Settings
from app.domain.types import Action, Intent
from app.persistence.database import make_engine, make_sessions
from app.persistence.models import Business, BusinessRule

BUSINESS_ID = "00000000-0000-0000-0000-000000000001"
RULE_ID = "00000000-0000-0000-0000-000000000002"


def seed(session: Session):
    """Idempotently insert the synthetic demo business and no-price-on-call owner rule when
    absent. Never overwrite existing owner edits; commit the caller's Session and return the
    demo business ID. Replace demo facts with owner-confirmed information before a real
    pilot.
    """
    if session.get(Business, BUSINESS_ID) is None:
        session.add(
            Business(
                id=BUSINESS_ID,
                name="Singh MRF Tyres",
                business_type="TYRE_RETAIL",
                address="Demo store address — replace before a pilot",
                opening_time=time(9, 30),
                closing_time=time(18, 30),
                timezone="Asia/Kolkata",
                default_language="hi-IN",
            )
        )
        session.flush()
    if session.get(BusinessRule, RULE_ID) is None:
        session.add(
            BusinessRule(
                id=RULE_ID,
                business_id=BUSINESS_ID,
                intent=Intent.PRICE_ENQUIRY,
                action=Action.DO_NOT_DISCLOSE_PRICE,
                instruction=(
                    "Never disclose prices over phone. Politely invite the caller to visit."
                ),
                enabled=True,
                priority=100,
            )
        )
    session.commit()
    return BUSINESS_ID


def main():
    """Load configured database settings, seed via a short Session, and dispose the CLI-owned
    engine even on failure. This command changes local database contents but never contacts
    speech/model providers.
    """
    engine = make_engine(Settings().database_url)
    try:
        with make_sessions(engine)() as session:
            print(f"Seeded business: {seed(session)}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
