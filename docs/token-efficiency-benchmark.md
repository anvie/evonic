# Provider Payload Token-Efficiency Benchmark

## Scope

This report measures the behavior-preserving payload reductions implemented for Kanban task #748. The benchmark is deterministic and offline: it performs no provider calls, serializes representative provider-facing messages and OpenAI-compatible tool schemas, counts them with Evonic's local `cl100k_base` estimator, and applies `plugins/token_monitor/pricing.py` prices.

Run both variants with:

```bash
.venv/bin/python scripts/token_cost_benchmark.py --variant baseline --output baseline.json
.venv/bin/python scripts/token_cost_benchmark.py --variant current --output current.json
```

`baseline` recreates the prior artifact instructions, empty schema fields, and receipt wording. `current` measures the optimized forms. Both variants use identical scenarios, conversation content, tool availability, call counts, tokenizer, pricing, and completion assumptions.

## Changes measured

1. Compact repeated artifact instructions while retaining the directory, `/_self/` restriction, save methods, public URL, and image embedding guidance.
2. Recursively remove only semantically empty tool-schema fields: `description: ""` and `required: []`. Tool names, availability, parameters, types, enums, and non-empty constraints remain unchanged.
3. Shorten sanitized active-turn receipt wording while retaining sequence, tool name, success status, classification, and a deterministic 12-hex attribution digest.

The canonical conversation is not mutated. Provider-native tool calls retained in the projected context preserve IDs, order, and result pairing. Projection still validates protocol and fails open to the canonical payload. Retry and fallback scenarios use the same effective payload on every attempt.

## Assumptions

- Tokenizer: `cl100k_base`; counts are deterministic estimates, not provider invoices.
- Pricing: repository defaults in USD per one million tokens. Deployments can override these values.
- Output: 500 completion tokens per provider call in both variants. Input optimizations do not claim output savings.
- Cached-input illustration: 50% input-price discount; output remains full price. This is an explicit comparison assumption, not a guarantee about provider cache eligibility.
- Monthly projection: 10,000 repetitions of the full seven-scenario benchmark mix (11 provider calls per repetition).
- Representative tiers: `gpt-4o-mini`, `gpt-4o`, `claude-sonnet`, and `claude-opus`.
- No paid or network provider calls were made.

## Input-token results

| Scenario | Calls | Baseline | Current | Saved | Saved % |
|---|---:|---:|---:|---:|---:|
| Simple turn | 1 | 1,129 | 998 | 131 | 11.60% |
| Multi-tool loop | 3 | 5,406 | 5,013 | 393 | 7.27% |
| Long tool output | 1 | 23,576 | 23,445 | 131 | 0.56% |
| Loaded skill | 1 | 1,165 | 1,034 | 131 | 11.24% |
| Retry, same payload | 2 | 3,604 | 3,342 | 262 | 7.27% |
| Fallback, same payload | 2 | 3,604 | 3,342 | 262 | 7.27% |
| Compacted long loop | 1 | 6,898 | 6,749 | 149 | 2.16% |
| **Total** | **11** | **45,382** | **43,923** | **1,459** | **3.21%** |

The large raw-output scenario is intentionally dominated by retained tool data, so its percentage is lower. The compacted-loop scenario includes an additional 18-token saving from denser receipts.

## Component attribution

| Component | Baseline tokens | Current tokens | Saved |
|---|---:|---:|---:|
| Conversation/history | 33,014 | 32,996 | 18 |
| Tool schemas | 8,800 | 8,712 | 88 |
| System instructions | 3,531 | 2,178 | 1,353 |
| Loaded skill context | 37 | 37 | 0 |

The ranking demonstrates why no risky capability pruning was attempted: tool schemas remain a meaningful fixed cost, but removing discoverable tools would change behavior. The implemented schema reduction removes only empty metadata.

## Estimated cost

Totals below cover one full benchmark mix, including the unchanged output assumption.

| Model | Baseline uncached | Current uncached | Saved | Projected monthly saved | Baseline with cached-input assumption | Current with cached-input assumption | Projected monthly saved, cached assumption |
|---|---:|---:|---:|---:|---:|---:|---:|
| GPT-4o mini | $0.010108 | $0.009888 | $0.000220 | $2.20 | $0.006702 | $0.006595 | $1.07 |
| GPT-4o | $0.168454 | $0.164808 | $0.003646 | $36.46 | $0.111726 | $0.109902 | $18.24 |
| Claude Sonnet | $0.218646 | $0.214269 | $0.004377 | $43.77 | $0.150573 | $0.148385 | $21.88 |
| Claude Opus | $1.093230 | $1.071345 | $0.021885 | $218.85 | $0.752865 | $0.741922 | $109.43 |

Because completion tokens are held constant, total-request cost savings percentages are smaller than the 3.21% input-token reduction. Actual savings vary with model choice, provider tokenization, cache treatment, completion length, and traffic shape.

## Safety and regression coverage

Focused regressions verify:

- recursive schema compaction removes only empty optional metadata;
- canonical messages remain byte-for-byte equivalent after projection;
- compacted receipts expose no raw arguments or tool output;
- retained tool calls/results remain valid and ordered;
- artifact instructions retain every operational capability while staying below a fixed token ceiling.

The benchmark itself covers a simple turn, multi-tool loop, long tool output, loaded skill, identical retry payload, identical fallback payload, and projected long loop. Its JSON metadata records tokenizer, prices, completion assumption, cache assumption, payload variant, and zero network calls.
