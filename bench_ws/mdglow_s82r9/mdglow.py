def convert_heading(text: str) -> str:
    """Converts a markdown-style heading (e.g., '# Heading') to a 'glow' heading."""
    if text.startswith('#'):
        text = text.lstrip('#').strip()
        if text:
            return f"=={text}=="
    return text

def convert_bold(text: str) -> str:
    """Converts a markdown-style bold text (e.g., '**bold**') to 'glow' bold."""
    if text.startswith('**') and text.endswith('**'):
        return f"[[{text[2:-2]}]]"
    return text

