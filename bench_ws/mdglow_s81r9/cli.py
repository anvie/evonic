import argparse
import sys
from mdglow import convert_heading, convert_bold

def main():
    parser = argparse.ArgumentParser(description="CLI wrapper for mdglow.")
    parser.add_argument("input", help="Path to the input markdown file.")
    parser.add_argument("-o", "--output", help="Path to the output file.")

    args = parser.parse_args()

    try:
        with open(args.input, 'r') as f:
            content = f.read()
    except FileNotFoundError:
        print(f"Error: File {args.input} not found.")
        sys.exit(1)

    lines = content.splitlines()
    processed_lines = []
    for line in lines:
        res = convert_heading(line)
        if not res.startswith("[HEADER]"):
            res = convert_bold(res)
        processed_lines.append(res)

    output = "\n".join(processed_lines)

    if args.output:
        with open(args.output, 'w') as f:
            f.write(output)
        print(f"Processed content written to {args.output}")
    else:
        print(output)

if __name__ == "__main__":
    main()
