import subprocess
import os
import sys

def test_cli():
    input_file = "test_input.md"
    output_file = "test_output.md"
    
    content = """# Header 1
#NoSpace
**bold text**
Normal text
"""
    
    with open(input_file, "w") as f:
        f.write(content)
        
    # Expected output:
    # [HEADER] Header 1
    # #NoSpace
    # [BOLD] bold text
    # Normal text
    
    expected_output = "[HEADER] Header 1\n#NoSpace\n[BOLD] bold text\nNormal text"
    
    try:
        subprocess.run(
            ["python3", "cli.py", input_file, "-o", output_file],
            check=True,
            capture_output=True,
            text=True
        )
        
        with open(output_file, "r") as f:
            actual_output = f.read()
            
        assert actual_output == expected_output
        print("Test passed!")
    except Exception as e:
        print(f"Test failed: {e}")
        sys.exit(1)
    finally:
        if os.path.exists(input_file):
            os.remove(input_file)
        if os.path.exists(output_file):
            os.remove(output_file)

if __name__ == "__main__":
    import sys
    test_cli()
