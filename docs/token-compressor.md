# RTK Token Compressor

Real-Time Kernel for AI command output compression.

## What it does

evonic's tool execution (bash, runpy, read_file, etc.) produces verbose output.  Before
RTK, llm_loop.py blunt-truncated every tool output at 8000 characters — keeping noise
and discarding signal.

RTK replaces blunt truncation with per-command, regex-based filter pipelines that
strip boilerplate, remove ANSI escape codes, discard irrelevant lines, and keep only
what the LLM actually needs.

Projected token savings: 60-80% on common commands (git status, ls -la, pytest, etc.).

## Architecture: split-path output

Every tool execution produces three output paths:

```
                   tool_result (structured dict)
                      |
            +---------+---------+
            |                   |
     json.dumps()        result_dict (raw dict)
            |                   |
      result_str               |
         /  \                   |
        /    \                  |
      DB     RTK compressor    |
  (full)        |          timeline (UI)
          compressed_str
          (LLM context)
```

1. DB path (result_str): Full JSON output, stored for the detail view. Never truncated.
2. LLM path (compressed_str): Compressed output sent to the LLM. Token savings here.
3. Timeline path (result_dict): Full structured dict for the thinking panel UI.

Error outputs (exit_code != 0) always pass through unchanged.

Implementation:
- llm_loop.py lines 1468-1534: primary split-path during tool execution
- context.py build_message_entry() lines 619-656: safety net for DB messages

## Filter pipeline (8 stages)

| Stage | Field | What it does |
|-------|-------|-------------|
| 1 | strip_ansi | Remove ANSI escape sequences |
| 2 | replace | Regex find-and-replace, line by line |
| 3 | match_output | Whole-output pattern match, short-circuits |
| 4 | strip_lines | Remove lines matching any pattern |
| 5 | keep_lines | Keep only lines matching at least one pattern |
| 6 | truncate_lines_at | Cap each line to N chars |
| 7 | head_lines / tail_lines | Keep first N and/or last N lines |
| 8 | max_lines | Hard cap on total lines |
| 9 | on_empty | Replacement text when output is empty |

Stages 4 and 5 are mutually exclusive — strip_lines takes priority.

## How to add a new filter

Create a .toml file in one of three directories (loaded in priority order):

1. Project-specific: .evonic/filters/ in the project root (highest priority)
2. Agent-specific: agents/<agent_id>/kb/filters/ (medium priority)
3. Built-in: backend/token_compressor/filters/builtin/ (lowest priority)

### TOML schema

```toml
[filter]
command = "^git\\s+status"
description = "Compact git status"
strip_ansi = true

[[filter.replace]]
pattern = "^\\t"
replacement = "  "

[[filter.match_output]]
pattern = "nothing to commit"
message = "git status: clean working tree"

strip_lines = [
    "^On branch ",
    "^Your branch is ",
    "^$",
]

keep_lines = [
    "^error",
    "^warning",
]

truncate_lines_at = 200
head_lines = 30
tail_lines = 5
max_lines = 50
on_empty = "git status: clean working tree"
```

### Command regex

The command field is matched against the extracted command string from
extract_command():

| Tool | Example command string |
|------|----------------------|
| bash | git status, ls -la |
| runpy | pytest, python |
| read_file | read_file /tmp/x.py |
| Other | http.get, kanban_create_task |

First filter whose command regex matches wins.

## Environment variables

| Variable | Effect |
|----------|--------|
| RTK_NO_COMPRESS=1 | Disable all RTK compression globally |
| RTK_VERBOSE=1 | Log pre/post compression stats at DEBUG level |

Both are read at module load time in config.py.

## Per-agent filter customization

1. Create agents/<agent_id>/kb/filters/ directory
2. Add .toml files following the same schema
3. Filters with the same command regex replace built-in ones
4. New command regexes are added to the set

Registry loads: built-in -> agent -> project (later wins).

## Troubleshooting

### Compression not happening

1. Check command regex matches: reg.lookup("git status") — None means no match
2. Check exit_code is 0 (errors skip compression)
3. Check RTK_NO_COMPRESS is not set to 1
4. JSON wrapping: llm_loop.py passes json.dumps(tool_result) which wraps output
   in JSON. Line-based patterns don't match inside JSON strings. Use match_output
   for whole-text matching, or the read_file_compressor.py JSON unwrapper.

### Debug logging

Set RTK_VERBOSE=1 to see: "RTK: 'git status' compressed 823 -> 127 chars (85%)"

### Filter not loading

- Validate TOML syntax
- Check file is in correct directory
- Verify command field is present and valid regex

### Exceptions

The compressor is fail-open: any exception returns the original output unchanged.
Check logs for "RTK: exception compressing" warnings.
