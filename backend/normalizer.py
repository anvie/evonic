# Normalize quote-like characters in text before sending to LLM servers.
#
# llama.cpp --jinja parses tool call arguments as JSON server-side. When the
# LLM echoes straight apostrophes or double quotes verbatim inside a JSON
# string value, the parser fails with a 500 "missing closing quote" error.
# Replacing them with semantically equivalent typographic characters prevents
# the model from reproducing them in tool call output.

_QUOTE_TABLE = str.maketrans({
    "\u0027": "\u2019",  # ' straight apostrophe         → ' right single quotation mark
    "\u2018": "\u2019",  # ' left single quotation mark  → ' right single quotation mark
    "\u201b": "\u2019",  # ‛ reversed single quot. mark  → ' right single quotation mark
    "\u0022": "\u201c",  # " straight double quote        → " left double quotation mark
    "\u201e": "\u201c",  # „ low double quotation mark    → " left double quotation mark
})


def normalize_llm_text(text: str) -> str:
    """Replace JSON-unsafe quote characters with safe typographic equivalents."""
    return text.translate(_QUOTE_TABLE) if text else text


# ---------------------------------------------------------------------------
# Smart-quote normalization for code written by agents
# ---------------------------------------------------------------------------
# Small LLM models sometimes generate smart/curly quotes (U+2018, U+2019,
# U+201C, U+201D) when writing code.  This causes SyntaxError in the
# browser/JavaScript runtime because smart quotes are not valid characters
# for string delimiters in most programming languages.
#
# The functions below normalise those typographic quotes back to their
# plain-ASCII equivalents before the content is written to disk.

_CODE_QUOTE_TABLE = str.maketrans({
    "\u2018": "'",   # \u2018 left single quotation mark  -> ' straight apostrophe
    "\u2019": "'",   # \u2019 right single quotation mark -> ' straight apostrophe
    "\u201c": '"',   # \u201c left double quotation mark  -> " straight double quote
    "\u201d": '"',   # \u201d right double quotation mark -> " straight double quote
})


def normalize_code_quotes(text: str) -> str:
    """Replace typographic/smart quotes with plain-ASCII equivalents.

    Converts:
      U+2018/U+2019 (curly single quotes)  ->  U+0027 (straight apostrophe)
      U+201C/U+201D (curly double quotes)  ->  U+0022 (straight double quote)
    """
    if not text:
        return text
    return text.translate(_CODE_QUOTE_TABLE)


# ---------------------------------------------------------------------------
# Re-encode decoded Unicode back to literal \uXXXX escape sequences
# ---------------------------------------------------------------------------
# When an LLM puts \u2022 inside a JSON tool-call argument, the JSON parser
# decodes it to the actual Unicode character (e.g. '•').  If the file on disk
# contains the literal text \u2022 (6 ASCII characters), str_replace will fail
# because the decoded character doesn't match the literal escape.
#
# This function reverses JSON's Unicode decoding: non-ASCII characters are
# converted back to the literal \uXXXX form so they can match file content.

# Precomputed lookup: list indexed by code point for O(1) array lookup
# instead of dict hashing.  ~65K entries, ~1 MB memory — acceptable.
_REENCODE_TABLE = [None] * 0x10000
for _cp in range(128, 0x10000):
    _REENCODE_TABLE[_cp] = f'\\u{_cp:04x}'


def reencode_unicode_escapes(text: str) -> str:
    """Re-encode non-ASCII characters as literal \\uXXXX escape sequences.

    BMP characters (U+0080..U+FFFF) become \\uXXXX.
    Supplementary characters (U+10000+) become surrogate pairs \\uXXXX\\uXXXX.
    """
    if not text:
        return text
    # Fast path: pure ASCII is a no-op — the output is identical.
    if text.isascii():
        return text
    parts = []
    for ch in text:
        cp = ord(ch)
        if cp < 0x10000:
            esc = _REENCODE_TABLE[cp]
            if esc is not None:
                parts.append(esc)
                continue
            parts.append(ch)
        elif cp > 0xFFFF:
            # Supplementary plane: encode as UTF-16 surrogate pair
            cp -= 0x10000
            high = 0xD800 + (cp >> 10)
            low = 0xDC00 + (cp & 0x3FF)
            parts.append(f'\\u{high:04x}\\u{low:04x}')
        else:
            parts.append(ch)
    return ''.join(parts)
