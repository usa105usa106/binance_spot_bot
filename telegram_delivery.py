"""Compatibility facade for safe Telegram delivery helpers in :mod:`bot`."""
from __future__ import annotations

try:
    from . import bot as _runtime
except ImportError:
    import bot as _runtime

CAPTION_LIMIT_UTF16 = _runtime.CAPTION_LIMIT_UTF16
MESSAGE_LIMIT_UTF16 = _runtime.MESSAGE_LIMIT_UTF16
telegram_utf16_units = _runtime.telegram_utf16_units
markdown_to_plain = _runtime.markdown_to_plain
split_telegram_text = _runtime.split_telegram_text
send_photo_with_text = _runtime.send_photo_with_text
send_text_chunks = _runtime.send_text_chunks

__all__ = [
    "CAPTION_LIMIT_UTF16",
    "MESSAGE_LIMIT_UTF16",
    "telegram_utf16_units",
    "markdown_to_plain",
    "split_telegram_text",
    "send_photo_with_text",
    "send_text_chunks",
]
