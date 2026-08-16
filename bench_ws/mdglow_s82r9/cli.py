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
        
        output = []
        for line in lines:
            stripped = line.rstrip('\n')
            processed = convert_heading(stripped)
            processed = convert_bold(processed)
            output.append(processed + '\n')
        
        print("".join(output), end='')
    except FileNotFoundError:
        print(f"Error: File {filename} not found.")

if __name__ == "__main__":
    main()
