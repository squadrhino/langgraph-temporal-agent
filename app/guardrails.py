import re


INPUT_BLOCKED_MESSAGE = (
    "I can't process that message because it may contain unsafe instructions "
    "or sensitive information."
)
OUTPUT_BLOCKED_MESSAGE = "I couldn't return that response safely."
MAX_OUTPUT_LENGTH = 10_000

PROMPT_INJECTION_PATTERNS = (
    re.compile(r"\bignore (all |the )?(previous|prior|system) instructions?\b", re.I),
    re.compile(r"\breveal (the )?(system|hidden) prompt\b", re.I),
    re.compile(r"\bshow (me )?(your )?(system|hidden) instructions?\b", re.I),
)
SECRET_PATTERN = re.compile(
    r"\b(api[_ -]?key|access[_ -]?token|password)\s*[:=]\s*\S+",
    re.I,
)
CARD_PATTERN = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


class GuardrailViolation(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def validate_input(text: str) -> None:
    if any(pattern.search(text) for pattern in PROMPT_INJECTION_PATTERNS):
        raise GuardrailViolation("prompt_injection")
    if contains_sensitive_data(text):
        raise GuardrailViolation("sensitive_data")


def validate_output(text: str) -> None:
    if len(text) > MAX_OUTPUT_LENGTH:
        raise GuardrailViolation("output_too_long")
    if contains_sensitive_data(text):
        raise GuardrailViolation("sensitive_data")


def contains_sensitive_data(text: str) -> bool:
    if SECRET_PATTERN.search(text):
        return True
    return any(is_valid_card_number(match.group()) for match in CARD_PATTERN.finditer(text))


def is_valid_card_number(value: str) -> bool:
    digits = [int(character) for character in value if character.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False

    checksum = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0
