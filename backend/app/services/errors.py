"""Bounded public failure categories and correlation envelopes."""

from enum import StrEnum


class ErrorCategory(StrEnum):
    """Bounded operational failure taxonomy for the legacy orchestrator and API. Categories
    distinguish policy/context/provider/database failures without embedding sensitive
    exception messages.
    """

    TELEPHONY_ERROR = "TELEPHONY_ERROR"
    STT_ERROR = "STT_ERROR"
    INTENT_ERROR = "INTENT_ERROR"
    POLICY_ERROR = "POLICY_ERROR"
    CONTEXT_ERROR = "CONTEXT_ERROR"
    MODEL_ERROR = "MODEL_ERROR"
    TTS_ERROR = "TTS_ERROR"
    DATABASE_ERROR = "DATABASE_ERROR"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


class CallExecutionError(Exception):
    """Safe public error envelope for a failed legacy call. Retains application call/trace IDs
    and bounded category/stage so operators can retrieve protected diagnostics; the
    underlying exception is not exposed in the API response.
    """

    def __init__(self, call_id: str, trace_id: str, category: ErrorCategory, stage: str):
        """Store correlation IDs and bounded failure classification and build an identifier-
        only message. The orchestrator raises this after attempting durable partial-failure
        recovery.
        """
        self.call_id = call_id
        self.trace_id = trace_id
        self.category = category
        self.stage = stage
        super().__init__(f"Call {call_id} failed at {stage}: {category}")


class BusinessNotFound(Exception):
    """Signal that requested business context does not exist. The API translates this to 404;
    no model/provider work should proceed with an invented business.
    """

    pass
