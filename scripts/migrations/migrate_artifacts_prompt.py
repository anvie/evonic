#!/usr/bin/env python3
"""Migration script: inject artifacts prompt into all existing agents' SYSTEM.md files."""

import os
import sys
import sqlite3

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(BASE_DIR, 'shared', 'db', 'evonic.db')

ARTIFACT_PROMPT_TEMPLATE = """
## Artifacts Feature

You have an **Artifacts** feature that allows you to save files you produce during your work. Files are stored in your dedicated artifacts directory and are accessible via the web UI.

### Using save_artifact Tool

Use the **save_artifact** tool to save files:
- `filename`: the name of the file (e.g. 'report.md', 'analysis.txt', 'output.json')
- `content`: the text content of the file (or base64-encoded content for binary files)
- `mime_type`: optional MIME type hint
- `mode`: set to 'text' (default) for text files, or 'base64' for binary files (PDFs, images, etc.)

When to use this tool:
- After completing analysis or research, save the findings as a report
- After generating code, configuration, or any output, save it as an artifact
- After creating images, PDFs, or markdown documents
- Any time you produce a file that the user or other agents may want to reference later
- For binary files (PDFs, images), set `mode: "base64"` and provide base64-encoded content

### Alternative: Using write_file or bash/runpy

You can also save files directly to your artifacts directory using:
- `write_file` with path starting with `/workspace/shared/agents/<YOUR_AGENT_ID>/artifacts/<filename>`
- bash/runpy by writing files to the same directory path

This is particularly useful for binary files (PDFs, images) that you generate via Python scripts.

The files are stored in your dedicated artifacts directory and can be browsed and downloaded from the agent detail page in the Artifacts tab.
"""


def _system_prompt_path(agent_id: str) -> str:
    return os.path.join(BASE_DIR, 'agents', agent_id, 'SYSTEM.md')


def inject_artifacts_prompt(agent_id: str) -> bool:
    """Inject artifacts prompt into SYSTEM.md if not already present. Returns True if changed."""
    path = _system_prompt_path(agent_id)
    if not os.path.isfile(path):
        print(f"  [SKIP] {agent_id}: SYSTEM.md not found")
        return False

    with open(path, 'r', encoding='utf-8') as f:
        sp = f.read()

    prompt_text = ARTIFACT_PROMPT_TEMPLATE.strip()

    if prompt_text in sp:
        print(f"  [OK]   {agent_id}: already has artifacts prompt")
        return False

    sp = sp.rstrip() + '\n\n' + prompt_text + '\n'
    with open(path, 'w', encoding='utf-8') as f:
        f.write(sp)
    print(f"  [INJECT] {agent_id}: artifacts prompt injected")
    return True


def ensure_tool_assigned(agent_id: str) -> bool:
    """Ensure save_artifact tool is assigned to agent. Returns True if added."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Check if already assigned
    cursor.execute(
        "SELECT 1 FROM agent_tools WHERE agent_id = ? AND tool_id = ?",
        (agent_id, 'save_artifact')
    )
    if cursor.fetchone():
        conn.close()
        print(f"  [OK]   {agent_id}: save_artifact tool already assigned")
        return False

    # Assign the tool
    cursor.execute(
        "INSERT INTO agent_tools (agent_id, tool_id) VALUES (?, ?)",
        (agent_id, 'save_artifact')
    )
    conn.commit()
    conn.close()
    print(f"  [ADD]  {agent_id}: save_artifact tool assigned")
    return True


def main():
    print("=== Artifacts Migration Script ===")
    print(f"Database: {DB_PATH}")
    print()

    if not os.path.isfile(DB_PATH):
        print(f"ERROR: Database not found at {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Get all agents with artifacts_enabled=1 (or NULL, defaulting to 1)
    cursor.execute(
        "SELECT id FROM agents WHERE artifacts_enabled IS NULL OR artifacts_enabled = 1"
    )
    agents = cursor.fetchall()
    conn.close()

    print(f"Found {len(agents)} agent(s) with artifacts enabled.\n")

    injected_count = 0
    tool_added_count = 0

    for (agent_id,) in agents:
        print(f"Processing: {agent_id}")
        if inject_artifacts_prompt(agent_id):
            injected_count += 1
        if ensure_tool_assigned(agent_id):
            tool_added_count += 1
        print()

    print("=== Summary ===")
    print(f"Total agents processed: {len(agents)}")
    print(f"SYSTEM.md injected: {injected_count}")
    print(f"save_artifact tool added: {tool_added_count}")
    print("Done.")


if __name__ == '__main__':
    main()
