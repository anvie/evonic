import sys
from mdglow import convert_heading, convert_bold

def main():
    if len(sys.argv) < 2:
        print("Usage: python cli.py <filename>")
        return

    filename = sys.argv[1]
    try:
        with open(filename, 'r') as f:
            lines = f.readlines()
        
        if not lines:
            print("File is empty.")
            return

        output = []
        # Process first line as a heading
        first_line = lines[0].strip()
        if first_line:
            output.append(convert_heading(first_line, level=1) + "\n")
        
        # Process remaining lines as bold
        for line in lines[1:]:
            text = line.strip()
            if text:
                output.append(convert_bold(text) + "\n")
            else:
                output.append("\n")

        print("".join(output))

    except FileNotFoundError:
        print(f"Error: File '{filename}' not found.")
    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    main()
