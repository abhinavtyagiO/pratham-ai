"""Reviewed native-Hindi equivalents of the same policy-authorized speech acts."""

from app.domain.conversations import ModelContext, ResponseDraft
from app.domain.types import Action

GREETING_HI = "नमस्ते, {name} के AI assistant से बात हो रही है। बताइए, मैं क्या मदद कर सकता हूँ?"


def render_hindi(context: ModelContext, draft: ResponseDraft):
    """Render reviewed Hindi wording from authorized context and a validated draft.

    Owner attention overrides phrasing, and the optional closing time comes only
    from confirmed business facts. Script substitution changes period words only;
    no model-written sentence or alternate numeric fact enters synthesized speech.
    """
    if context.decision == Action.REQUIRE_OWNER_ATTENTION:
        return (
            "इसकी पक्की जानकारी अभी मेरे पास नहीं है। अपना नाम और सवाल की जानकारी बता दीजिए, "
            "मैं आपका सवाल नोट कर लेता हूँ।"
        )
    text = (
        "जी, रेट की जानकारी के लिए आप दुकान पर आ सकते हैं।"
        if draft.phrasing == "BRIEF"
        else "सर, रेट की पूछताछ के लिए आप दुकान पर आ सकते हैं।"
    )
    if draft.mention_closing_time and context.closing_time_phrase:
        phrase = context.closing_time_phrase
        for latin, hindi in (
            ("shaam", "शाम"),
            ("subah", "सुबह"),
            ("dopahar", "दोपहर"),
            ("raat", "रात"),
        ):
            phrase = phrase.replace(latin, hindi)
        text += f" दुकान बंद होने का समय {phrase} है।"
    return text
