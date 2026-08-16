def convert_heading(text: str, level: int = 1) -> str:
    """Converts text to a markdown heading."""
    return f"{'#' * level} {text}"

def convert_bold(text: str) -> str:
    """Converts text to bold markdown."""
    return f"**{text}**"

if __name__ == "__main__":
    # Tests
    print(f"Heading 1: {convert_heading('Hello World', 1)}")
    print(f"Heading 2: {convert_heading('Hello World', 2)}")
    print(f"Bold: {convert_bold('Hello World')}")

    # Assertions
    assert convert_heading("Title", 1) == "# Title"
    assert convert_heading("Subtitle", 3) == "### Subtitle"
    assert convert_bold("Bold Text") == "**Bold Text**"
    print("All tests passed!")
