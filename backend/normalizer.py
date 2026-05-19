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
