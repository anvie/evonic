"""Small text helpers shared by the channel adapters."""

import re
from typing import Optional


def sanitize_filename(name: str) -> str:
    """Sanitize a filename to a safe ASCII slug, max 120 chars."""
    if not name:
        return 'file'
    cleaned = re.sub(r'[^A-Za-z0-9._-]', '_', name)[:120]
    return cleaned or 'file'


def human_size(size_bytes: Optional[int]) -> str:
    """Render a byte count as a human-friendly string."""
    if size_bytes is None or size_bytes < 0:
        return '0B'
    units = ['B', 'KB', 'MB', 'GB']
    n = float(size_bytes)
    for unit in units:
        if n < 1024 or unit == units[-1]:
            if unit == 'B':
                return f"{int(n)}{unit}"
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{int(size_bytes)}B"


def split_message(text: str, max_len: int = 4096) -> list:
    """Split text into chunks of at most max_len, preferring paragraph, line then word breaks."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = -1
        for sep in ('\n\n', '\n', ' '):
            pos = text.rfind(sep, 0, max_len)
            if pos > 0:
                split_at = pos
                break
        if split_at <= 0:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip('\n')
    return chunks
