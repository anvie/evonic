from mdglow import convert_heading, convert_bold

def test_convert_heading():
    assert convert_heading("# Heading") == "==Heading=="
    assert convert_heading("## Subheading") == "==Subheading=="
    assert convert_heading("Normal text") == "Normal text"
    print("convert_heading tests passed!")

def test_convert_bold():
    assert convert_bold("**bold**") == "[[bold]]"
    assert convert_bold("**bold text**") == "[[bold text]]"
    assert convert_bold("normal text") == "normal text"
    assert convert_bold("*italic*") == "*italic*"
    print("convert_bold tests passed!")

if __name__ == "__main__":
    test_convert_heading()
    test_convert_bold()
