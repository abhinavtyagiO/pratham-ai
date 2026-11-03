import pytest

from app.persistence.models import Business
from app.persistence.repository import CallRepository
from app.seed import BUSINESS_ID
from app.services.errors import CallExecutionError


def test_context_failure_still_has_persisted_call_metadata(runner, session):
    session.get(Business, BUSINESS_ID).timezone = "Invalid/Timezone"
    session.commit()
    with pytest.raises(CallExecutionError) as raised:
        runner.run(BUSINESS_ID, "MRF ka rate kya hai?")
    assert raised.value.category == "CONTEXT_ERROR"
    detail = CallRepository(session).detail(raised.value.call_id)
    assert detail["status"] == "FAILED" and detail["outcome"]["needs_attention"]
    assert detail["failures"][0]["stage"] == "load_business_context"


def test_policy_failure_never_reaches_model(runner, session, monkeypatch):
    invoked = []

    def fail(*args, **kwargs):
        raise ValueError("Policy implementation failed")

    monkeypatch.setattr(runner.policy, "evaluate", fail)
    monkeypatch.setattr(runner.providers.model, "generate", lambda *a, **k: invoked.append(True))
    with pytest.raises(CallExecutionError) as raised:
        runner.run(BUSINESS_ID, "rate kya hai?")
    assert raised.value.category == "POLICY_ERROR"
    assert not invoked
    detail = CallRepository(session).detail(raised.value.call_id)
    assert detail["failures"][0]["stage"] == "evaluate_policy"
    assert len(detail["messages"]) == 2
