import base64
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from app.config import Settings
from app.main import create_app
from app.persistence.models import Call
from app.realtime.config import VoiceSettings
from app.realtime.harness import start_event


def test_data_endpoints_require_configured_bearer_key(engine, session, telemetry):
    with TestClient(
        create_app(
            Settings(api_key=SecretStr("private-test-key")), engine=engine, telemetry=telemetry
        )
    ) as client:
        assert client.get("/health").status_code == 200
        assert client.get(f"/calls/{uuid4()}").status_code == 401
        assert client.get("/metrics").status_code == 401
        assert client.post("/calls/simulate", json={}).status_code == 401
        assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    for key in (None, SecretStr("")):
        with TestClient(
            create_app(Settings(api_key=key), engine=engine, telemetry=telemetry)
        ) as client:
            assert client.get("/metrics", headers={"Authorization": "Bearer "}).status_code == 503


def test_websocket_authentication_and_full_handshake(engine, session, telemetry):
    settings = VoiceSettings(
        enabled=True,
        exotel_ws_password=SecretStr("test-ws-password"),
        exotel_account_sid="AC-demo",
        pace_audio=False,
    )
    app = create_app(engine=engine, telemetry=telemetry, voice_settings=settings)
    auth = "Basic " + base64.b64encode(b"pratham:test-ws-password").decode()
    with TestClient(app) as client:
        for headers in ({}, {"Authorization": "Basic invalid"}, {"Authorization": "Bearer wrong"}):
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect("/voice/exotel", headers=headers):
                    pass
        with client.websocket_connect("/voice/exotel", headers={"Authorization": auth}) as socket:
            socket.send_json({"event": "connected"})
            start = start_event()
            socket.send_json(start)
            while True:
                event = socket.receive_json()
                if event["event"] == "mark":
                    socket.send_json({**event, "sequence_number": "2"})
                    break
            socket.send_json(
                {
                    "event": "stop",
                    "stream_sid": "MZ-demo",
                    "sequence_number": "3",
                    "stop": {"call_sid": "CA-demo", "reason": "callended"},
                }
            )
            with pytest.raises(WebSocketDisconnect):
                socket.receive_json()
    session.expire_all()
    call = session.scalar(select(Call))
    assert call is not None and call.status == "COMPLETED"


def test_invalid_account_and_disabled_ingress_are_rejected(engine, session, telemetry):
    settings = VoiceSettings(
        enabled=True, exotel_ws_password=SecretStr("test-ws-password"), exotel_account_sid="AC-real"
    )
    auth = "Basic " + base64.b64encode(b"pratham:test-ws-password").decode()
    with TestClient(
        create_app(engine=engine, telemetry=telemetry, voice_settings=settings)
    ) as client:
        with client.websocket_connect("/voice/exotel", headers={"Authorization": auth}) as socket:
            socket.send_json(start_event())
            with pytest.raises(WebSocketDisconnect):
                socket.receive_json()
    assert not session.scalars(select(Call)).all()
    with TestClient(create_app(engine=engine, telemetry=telemetry)) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/voice/exotel", headers={"Authorization": auth}):
                pass
