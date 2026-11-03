import json

import pytest

from app.evals import DEFAULT_DATASET, assess
from app.seed import BUSINESS_ID

CASES = json.loads(DEFAULT_DATASET.read_text())


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_golden_conversation(case, runner):
    result = runner.run(BUSINESS_ID, case["utterance"])
    assessment = assess(case, result)
    assert assessment["passed"], assessment
