import argparse
from mdglow import convert_heading, convert_bold
import sys

def main():
    parser = argparse.ArgumentParser(description="Process a markdown file with mdglow conversions.")
    parser.add_argument("input", help="Path to the input file.")
    parser.add_argument("--type", choices=["heading", "bold"], help="Type of conversion to apply.")
    args = parser.parse_args()

    try:
        with open(args.input, 'r') as f:
            lines = f.readlines()
        
        output = []
        for line in lines:
            if args.type == "heading" and line.startswith("Heading: "):
                output.append(convert_heading(line.replace("Heading: ", "").strip()) + "\n")
            elif args.type == "bold" and line.startswith("Bold: "):
                output.append(convert_bold(line.replace("Bold: ", "").strip()) + "\n")
            else:
                output.append(line)
        
        print("".join(output))
    except FileNotFoundError:
        print(f"Error: File {args.input} not found.", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
