"""Versioned, reviewed speech templates. Changing wording requires a version bump."""

INTENT_VERSION = "intent_detection:v1"
RESPONSE_VERSION = "response_generation:v1"
SUMMARY_VERSION = "call_summary:v1"
GREETING_VERSION = "greeting:v1"
VOICE_RESPONSE_VERSION = "response_generation:v2"
VOICE_RESPONSE_PROMPT = (
    "Select how to phrase an already authorized Hindi/Hinglish response. "
    "Return one JSON object only: "
    '{"phrasing":"POLITE","mention_closing_time":true}. '
    'phrasing must be "POLITE" or "BRIEF". mention_closing_time must be a boolean. '
    "No other keys, text, prices, inventory assertions, or tools are permitted. "
    "The backend will render the chosen authorized phrases and insert known facts."
)

RESPONSE_PROMPT = (
    "Choose POLITE or BRIEF phrasing and whether to mention closing time. "
    "Return ResponseDraft JSON only. "
    "No tools, prices, inventory, or arbitrary speech are available."
)
SUMMARY_PROMPT = "Acknowledge the supplied structured call result using SummaryDraft JSON."
GREETING = (
    "Namaste, {name} ke AI assistant se baat ho rahi hai. Bataiye, main kya help kar sakta hoon?"
)
PRICE_PHRASES = {
    "POLITE": "Sir, rate ki pooch-taach ke liye aap shop par aa sakte hain.",
    "BRIEF": "Ji, rate ki jankari ke liye aap shop par aakar baat kar sakte hain.",
}
UNKNOWN_RESPONSE = (
    "Iski confirmed jankari abhi mere paas nahi hai. Aap apna naam aur query ki details "
    "bata dijiye; main aapki query note kar leta hoon."
)
