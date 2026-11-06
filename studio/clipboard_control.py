"""Portable personal voice clipboard tracking.

The monitor is opt-in. No clipboard text is written to disk, transmitted,
or spoken until a new copy arrives after monitoring is switched on.
"""
from __future__ import annotations

from collections import deque

MAX_CHARS = 50000


class ClipboardSpeechQueue:
    def __init__(self, max_items=20):
        self.last_seen = None
        self.items = deque(maxlen=max_items)

    def arm(self, existing_text):
        """Ignore the text already present when monitoring is enabled."""
        self.last_seen = existing_text or ""

    def observe(self, value):
        value = value or ""
        if self.last_seen is None:
            self.arm(value)
            return None
        if value == self.last_seen:
            return None
        self.last_seen = value
        value = value.strip()
        return value if value and len(value) <= MAX_CHARS else None

    def enqueue(self, value):
        value = (value or "").strip()
        if not value or len(value) > MAX_CHARS:
            return False
        if self.items and self.items[-1] == value:
            return False
        self.items.append(value)
        return True

    def pop(self):
        return self.items.popleft() if self.items else None

    def clear(self):
        self.items.clear()

    def __len__(self):
        return len(self.items)
