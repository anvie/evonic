from mdglow import convert_heading, convert_bold

def test_convert_heading():
    assert convert_heading("# Heading") == "==Heading=="
    assert convert_heading("Normal text") == "Normal text"

def test_convert_bold():
    assert convert_bold("**bold text**") == "[[bold text]]"
    assert convert_bold("Normal text") == "Normal text"

if __name__ == "__main__":
    test_convert_heading()
    test_convert_bold()
    print("All tests passed!")
