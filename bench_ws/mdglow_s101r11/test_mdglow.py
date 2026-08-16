from mdglow import convert_heading, convert_bold

def test_convert_heading():
    assert convert_heading("Hello") == "# Hello"
    assert convert_heading("World") == "# World"
    print("test_convert_heading passed")

def test_convert_bold():
    assert convert_bold("Hello") == "**Hello**"
    assert convert_bold("World") == "**World**"
    print("test_convert_bold passed")

if __name__ == "__main__":
    test_convert_heading()
    test_convert_bold()
