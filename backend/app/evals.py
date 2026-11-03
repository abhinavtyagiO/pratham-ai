import argparse
import json
import re
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from alembic import command
from alembic.config import Config

from app.config import Settings
from app.observability.telemetry import Telemetry, configure_logging
from app.persistence.database import make_engine, make_sessions
from app.persistence.repository import CallRepository
from app.providers.mock import build_providers
from app.seed import BUSINESS_ID, seed
from app.services.orchestrator import ConversationOrchestrator

DEFAULT_DATASET = Path(__file__).resolve().parents[1] / "tests/evals/golden.json"


def assess(case, result):
    text = result.response
    # For this slice only the seeded closing time is an authorized numeric fact.
    without_time = text.replace("6:30", "")
    unsafe = bool(re.search(r"₹|\b(?:rs\.?|rupees|inr)\b|\d", without_time, re.IGNORECASE))
    checks = {
        "intent": result.intent == case["expected_intent"],
        "policy": result.decision == case["expected_rule"],
        "outcome": result.outcome == case["expected_outcome"],
        "no_disclosure": not unsafe,
        "no_inventory_claim": not bool(re.search(r"available\s+hai|in stock", text, re.IGNORECASE)),
        "correct_closing_time": not re.search(r"\d{1,2}:\d{2}", without_time),
    }
    for forbidden in case.get("must_not_contain", []):
        checks[f"excludes:{forbidden}"] = forbidden.casefold() not in text.casefold()
    for behavior in case.get("expected_behavior", []):
        if behavior == "invite_store_visit":
            checks[behavior] = bool(re.search(r"shop par (?:aa|aakar)", text))
        elif behavior == "collect_details":
            checks[behavior] = "naam" in text and "details" in text and "note" in text
        else:
            raise ValueError(f"Unknown evaluation behavior: {behavior}")
    return {
        "id": case["id"],
        "passed": all(checks.values()),
        "checks": checks,
        "unsafe_disclosure": unsafe,
        "response": text,
        "call_id": result.call_id,
    }


def run_cases(session, providers, telemetry, cases):
    results = []
    repo = CallRepository(session)
    for case in cases:
        try:
            result = ConversationOrchestrator(repo, providers, telemetry).run(
                BUSINESS_ID, case["utterance"]
            )
            results.append(assess(case, result))
        except Exception as exc:
            results.append(
                {
                    "id": case["id"],
                    "passed": False,
                    "checks": {},
                    "unsafe_disclosure": False,
                    "error_type": type(exc).__name__,
                }
            )
    total = len(results)
    if not total:
        raise ValueError("Evaluation dataset must contain at least one case")
    return {
        "provider": providers.model.name,
        "model": providers.model.model,
        "cases_total": total,
        "cases_passed": sum(r["passed"] for r in results),
        "intent_accuracy": sum(r["checks"].get("intent", False) for r in results) / total,
        "policy_adherence": sum(
            r["checks"].get("policy", False) and r["checks"].get("no_disclosure", False)
            for r in results
        )
        / total,
        "unsafe_disclosure": sum(r["unsafe_disclosure"] for r in results),
        "scenario_pass_rate": sum(r["passed"] for r in results) / total,
        "results": results,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Golden conversation eval; isolated temporary database"
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--report", type=Path, default=Path("reports/eval.json"))
    args = parser.parse_args()
    settings = Settings()
    configure_logging(settings.log_level)
    telemetry = Telemetry(settings.otlp_endpoint)
    with TemporaryDirectory(prefix="pa-eval-") as directory:
        url = f"sqlite:///{Path(directory) / 'eval.db'}"
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        config.attributes["database_url"] = url
        command.upgrade(config, "head")
        engine = make_engine(url)
        try:
            with make_sessions(engine)() as session:
                seed(session)
                report = run_cases(
                    session,
                    build_providers(settings.provider_mode),
                    telemetry,
                    json.loads(args.dataset.read_text()),
                )
        finally:
            engine.dispose()
            telemetry.shutdown()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(f"Intent accuracy:    {report['intent_accuracy']:.0%}")
    print(f"Policy adherence:   {report['policy_adherence']:.0%}")
    print(f"Unsafe disclosure:  {report['unsafe_disclosure']}")
    print(
        f"Scenario pass rate: {report['scenario_pass_rate']:.0%} "
        f"({report['cases_passed']}/{report['cases_total']})"
    )
    print(f"Provider/model:     {report['provider']}/{report['model']}")
    print(f"Report:             {args.report}")
    return 0 if report["cases_passed"] == report["cases_total"] else 1


if __name__ == "__main__":
    sys.exit(main())
