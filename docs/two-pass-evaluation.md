# Two-Pass LLM Evaluation

## Overview

Evonic's evaluation runner can score model answers in two passes:

1. **Pass 1** — The model answers the benchmark prompt (often with reasoning in Indonesian or English).
2. **Pass 2** — A second LLM call extracts only the final answer in a strict format (number, `ya`/`tidak`, SQL, and so on).

Pass 2 makes scoring reliable when Pass 1 is verbose or formatted inconsistently.

## When it runs

Two-pass extraction is used by the built-in **Two-Pass Evaluator** (`two_pass`), which is the default for domains such as **math**, **reasoning**, and **health** (see `evaluator/domain_evaluators.py` and `test_definitions/evaluators/two_pass.json`).

Custom evaluators can set `"uses_pass2": true` in their JSON definition to opt in.

## Flow

```
Prompt → LLM (Pass 1) → raw response
                              ↓
                    extraction prompt (Pass 2)
                              ↓
                    clean answer → domain scorer
```

If Pass 2 output does not match the expected format, the extractor tries regex fallbacks on the Pass 1 text before marking extraction as failed.

## Configuration

| Setting | Env variable | Default |
|--------|--------------|---------|
| Enable Pass 2 globally | `TWO_PASS_ENABLED` | `1` (on) |
| Extraction temperature | `TWO_PASS_TEMPERATURE` | `0.0` |
| UI override (persisted) | System → Evaluators page | Falls back to env |

The UI toggle writes `two_pass_enabled` to the app settings store and takes effect on the next evaluation without restarting the server.

## Result details

Evaluation results include a `pass2` object when extraction ran:

- `success`, `format`, `extracted_answer`
- `prompt`, `raw_output`, optional `thinking`
- `error` when format validation failed

These fields appear in the evaluation runner, history detail view, and API JSON.

## Disabling two-pass

- Turn off **Two-pass extraction** on `/evaluate/evaluators`, or
- Set `TWO_PASS_ENABLED=0` in `.env` and restart if no DB override exists.

When disabled, scoring uses the raw Pass 1 response (same as `extraction_method: disabled` in logs).

## Related code

- `evaluator/answer_extractor.py` — Pass 2 prompts and validation
- `evaluator/strategies/two_pass.py` — Evaluator strategy
- `tests/test_answer_extractor.py` — Unit tests for format validation
