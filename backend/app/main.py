from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.security import require_api_key
from app.config import Settings
from app.observability.telemetry import Telemetry, configure_logging
from app.persistence.database import make_engine, make_sessions
from app.persistence.repository import CallRepository
from app.providers.mock import build_providers
from app.services.errors import BusinessNotFound, CallExecutionError
from app.services.orchestrator import ConversationOrchestrator


class SimulateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    business_id: UUID
    utterance: str = Field(min_length=1, max_length=4000)
    caller_phone: str | None = Field(default=None, pattern=r"^\+?[0-9]{7,15}$")


def create_app(
    settings=None,
    *,
    engine=None,
    providers=None,
    telemetry=None,
    voice_settings=None,
    voice_factory=None,
):
    settings = settings or Settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app):
        from app.realtime.config import VoiceSettings
        from app.realtime.factory import build_voice_providers, validate_live_settings
        from app.realtime.router import SessionRegistry

        app.state.settings = settings
        app.state.voice_settings = voice_settings or VoiceSettings()
        if (
            app.state.voice_settings.enabled
            and app.state.voice_settings.mode == "live"
            and voice_factory is None
        ):
            validate_live_settings(app.state.voice_settings)
        app.state.voice_factory = voice_factory or (
            lambda: build_voice_providers(app.state.voice_settings)
        )
        app.state.voice_registry = SessionRegistry(app.state.voice_settings.max_sessions)
        app.state.engine = engine if engine is not None else make_engine(settings.database_url)
        app.state.sessions = make_sessions(app.state.engine)
        app.state.providers = (
            providers if providers is not None else build_providers(settings.provider_mode)
        )
        app.state.telemetry = (
            telemetry if telemetry is not None else Telemetry(settings.otlp_endpoint)
        )
        try:
            yield
        finally:
            if telemetry is None:
                app.state.telemetry.shutdown()
            if engine is None:
                app.state.engine.dispose()

    api = FastAPI(title="Pratham AI", version="0.2.0", lifespan=lifespan)
    from app.realtime.router import router as voice_router

    api.include_router(voice_router)

    def session():
        with api.state.sessions() as db:
            yield db

    @api.get("/health")
    def health(db: Annotated[Session, Depends(session)]):
        try:
            db.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            raise HTTPException(503, "Database unavailable") from exc
        return {"status": "ok", "provider_mode": settings.provider_mode}

    @api.post("/calls/simulate", status_code=201, dependencies=[Depends(require_api_key)])
    def simulate(request: SimulateRequest, db: Annotated[Session, Depends(session)]):
        try:
            result = ConversationOrchestrator(
                CallRepository(db),
                api.state.providers,
                api.state.telemetry,
            ).run(str(request.business_id), request.utterance, request.caller_phone)
        except BusinessNotFound as exc:
            raise HTTPException(404, "Business not found") from exc
        except CallExecutionError as exc:
            raise HTTPException(
                503,
                {
                    "call_id": exc.call_id,
                    "trace_id": exc.trace_id,
                    "category": exc.category,
                    "stage": exc.stage,
                },
            ) from exc
        return asdict(result)

    @api.get("/calls/{call_id}", dependencies=[Depends(require_api_key)])
    def detail(call_id: UUID, db: Annotated[Session, Depends(session)]):
        call = CallRepository(db).detail(str(call_id))
        if call is None:
            raise HTTPException(404, "Call not found")
        return call

    @api.get("/metrics", dependencies=[Depends(require_api_key)])
    def metrics():
        return api.state.telemetry.metrics_snapshot()

    return api


app = create_app()
