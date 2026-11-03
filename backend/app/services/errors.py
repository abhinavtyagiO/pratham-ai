from enum import StrEnum


class ErrorCategory(StrEnum):
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
    def __init__(self, call_id: str, trace_id: str, category: ErrorCategory, stage: str):
        self.call_id = call_id
        self.trace_id = trace_id
        self.category = category
        self.stage = stage
        super().__init__(f"Call {call_id} failed at {stage}: {category}")


class BusinessNotFound(Exception):
    pass
