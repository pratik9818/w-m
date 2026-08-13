from aiogram.types import Message


def has_text(message: Message) -> bool:
    """True for messages with non-empty text. Safer than F.text.len() > 0, which
    raises if a non-text message (e.g. a photo) arrives while a text state is active."""
    return bool(message.text)
