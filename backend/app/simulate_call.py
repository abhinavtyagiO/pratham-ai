import argparse
import json
import sys
from dataclasses import asdict

from app.config import Settings
from app.observability.telemetry import Telemetry, configure_logging
from app.persistence.database import make_engine, make_sessions
from app.persistence.repository import CallRepository
from app.providers.mock import build_providers
from app.seed import BUSINESS_ID
from app.services.errors import BusinessNotFound, CallExecutionError
from app.services.orchestrator import ConversationOrchestrator


def main():
    parser = argparse.ArgumentParser(description="Run one deterministic simulated customer turn")
    parser.add_argument("utterance", nargs="?")
    parser.add_argument("--business-id", default=BUSINESS_ID)
    parser.add_argument("--caller-phone")
    parser.add_argument(
        "--replay", metavar="CALL_ID", help="Replay with saved business/rule snapshot"
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if not args.utterance and not args.replay:
        parser.error("Provide an utterance or --replay CALL_ID")
    settings = Settings()
    configure_logging(settings.log_level)
    telemetry = Telemetry(settings.otlp_endpoint)
    engine = make_engine(settings.database_url)
    try:
        with make_sessions(engine)() as session:
            repo = CallRepository(session)
            snapshot = None
            if args.replay:
                previous = repo.detail(args.replay)
                if previous is None:
                    parser.error("Replay call not found")
                customer = [m["text"] for m in previous["messages"] if m["speaker"] == "CUSTOMER"]
                if not customer or not previous["configuration_snapshot"]:
                    parser.error("Replay needs a persisted customer utterance and configuration")
                args.utterance = customer[0]
                args.business_id = previous["business_id"]
                args.caller_phone = previous["caller_phone"]
                snapshot = previous["configuration_snapshot"]
            result = ConversationOrchestrator(
                repo,
                build_providers(settings.provider_mode),
                telemetry,
            ).run(
                args.business_id,
                args.utterance,
                args.caller_phone,
                replay_of=args.replay,
                snapshot=snapshot,
            )
            if args.json:
                print(json.dumps(asdict(result), ensure_ascii=False))
            else:
                for label, value in (
                    ("Intent", result.intent),
                    ("Rule", result.decision),
                    ("Response", result.response),
                    ("Outcome", result.outcome),
                    ("Call ID", result.call_id),
                    ("Trace ID", result.trace_id),
                ):
                    print(f"{label}:\n{value}\n")
    except CallExecutionError as exc:
        print(
            json.dumps({"error": str(exc), "call_id": exc.call_id, "trace_id": exc.trace_id}),
            file=sys.stderr,
        )
        return 1
    except BusinessNotFound:
        print("Business not found. Run python -m app.seed first.", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
        telemetry.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
