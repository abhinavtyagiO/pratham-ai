from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app
from app.seed import BUSINESS_ID


def test_simulate_and_retrieve_through_http(engine, session, providers, telemetry):
    api = create_app(
        Settings(api_key=SecretStr("test-key")),
        engine=engine,
        providers=providers,
        telemetry=telemetry,
    )
    with TestClient(api, headers={"Authorization": "Bearer test-key"}) as client:
        assert client.get("/health").status_code == 200
        response = client.post(
            "/calls/simulate",
            json={
                "business_id": BUSINESS_ID,
                "utterance": "MRF ka rate kya hai?",
                "caller_phone": "+919999999999",
            },
        )
        assert response.status_code == 201
        result = response.json()
        assert result["outcome"] == "RESOLVED"
        detail = client.get(f"/calls/{result['call_id']}")
        assert detail.status_code == 200
        assert len(detail.json()["messages"]) == 3
        assert detail.json()["trace_id"] == result["trace_id"]
        assert detail.json()["outcome"]["summary"]
        assert client.get("/metrics").json()["resource_metrics"]
        assert client.get(f"/calls/{uuid4()}").status_code == 404
        assert client.get("/calls/not-a-uuid").status_code == 422
        assert (
            client.post(
                "/calls/simulate",
                json={
                    "business_id": str(uuid4()),
                    "utterance": "rate?",
                },
            ).status_code
            == 404
        )


def test_invalid_requests_are_rejected(engine, session, telemetry):
    with TestClient(
        create_app(Settings(api_key=SecretStr("test-key")), engine=engine, telemetry=telemetry),
        headers={"Authorization": "Bearer test-key"},
    ) as client:
        for fields in (
            {"utterance": " "},
            {"utterance": "x" * 4001},
            {"caller_phone": "not-a-phone"},
            {"business_id": "bad"},
            {"extra": "ignored?"},
        ):
            response = client.post(
                "/calls/simulate",
                json={
                    "business_id": BUSINESS_ID,
                    "utterance": "rate?",
                    **fields,
                },
            )
            assert response.status_code == 422


def test_provider_failure_returns_correlated_error_and_retrievable_failure(
    engine,
    session,
    telemetry,
    providers,
    monkeypatch,
):
    def fail(*a, **k):
        raise TimeoutError("PRIVATE API KEY")

    monkeypatch.setattr(providers.model, "generate", fail)
    with TestClient(
        create_app(
            Settings(api_key=SecretStr("test-key")),
            engine=engine,
            providers=providers,
            telemetry=telemetry,
        ),
        headers={"Authorization": "Bearer test-key"},
    ) as client:
        response = client.post(
            "/calls/simulate",
            json={
                "business_id": BUSINESS_ID,
                "utterance": "rate?",
            },
        )
        assert response.status_code == 503
        error = response.json()["detail"]
        assert error["category"] == "MODEL_ERROR" and error["trace_id"]
        assert "PRIVATE" not in response.text
        call = client.get(f"/calls/{error['call_id']}").json()
        assert call["status"] == "FAILED" and call["outcome"]["needs_attention"]
