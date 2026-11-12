# Exa — AI Search Engine

Exa is a search engine built for AI agents — it returns clean, structured results with full-text content. The Python SDK (`exa-py`) reads the API key from the `EXA_API_KEY` environment variable.

## The `exa_search` tool

For a plain web search you do not need code: call `exa_search` with a `query` (and optionally `num_results`, `category`,
`include_domains`, `start_published_date`). It returns each result's title, url, date and text. Use the SDK below only
for what the tool does not cover (answers, similar pages, structured output).

## Quick Start

```python
from exa_py import Exa
exa = Exa()  # reads API key from environment variable
```

## Core Methods

| Method | What it does |
|--------|-------------|
| `exa.search(q, num_results=N, type=..., **kw)` | Web search |
| `exa.search_and_contents(q, **kw)` | Search + auto-fetch full page text |
| `exa.get_contents(urls)` | Fetch page text from given URLs |
| `exa.find_similar(url, **kw)` | Find pages similar to a URL |
| `exa.answer(q, model="exa-pro")` | AI-generated answer with citations |
| `exa.stream_search(q, **kw)` | Streaming search |
| `exa.stream_answer(q, **kw)` | Streaming answer |

## Key Parameters for `search()`

- `num_results` — how many (default 10)
- `type` — `"auto"` | `"fast"` | `"neural"` | `"deep-lite"` | `"deep"` | `"deep-reasoning"` | `"instant"` (default: auto)
- `category` — `"news"` | `"research paper"` | `"pdf"` | `"company"` | `"financial report"` | `"personal site"` | `"people"`
- `include_domains` / `exclude_domains` — e.g. `["nature.com", "science.org"]`
- `start_published_date` / `end_published_date` — `"2025-01-01"` format
- `start_crawl_date` / `end_crawl_date` — filter by when Exa indexed it
- `include_text` / `exclude_text` — require/exclude words in page text
- `user_location` — `"Jakarta, Indonesia"` for geo-biased results
- `system_prompt` — custom prompt guiding AI behavior
- `output_schema` — JSON Schema dict for structured output
- `contents` — set to `False` to skip text (save bandwidth)

## Result Fields

Each result: `title`, `url`, `score`, `text` (body), `published_date`, `author`, `summary` (AI summary), `highlights`.

Response: `results`, `cost_dollars`, `search_time`, `resolved_search_type`.

## Common Patterns

### Basic search
```python
results = exa.search("quantum computing breakthrough", num_results=5, category="news")
for r in results.results:
    print(f"{r.title} — {r.url}")
```

### AI answer with citations
```python
resp = exa.answer("Compare Rust vs Go for backend", model="exa-pro")
print(resp.answer)
for c in resp.citations:
    print(f"  [{c.title}]({c.url})")
```

### Structured JSON output
```python
resp = exa.answer(
    "Top 3 databases for AI workloads",
    output_schema={
        "type": "object",
        "properties": {
            "databases": {
                "type": "array",
                "items": {"type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "strengths": {"type": "array", "items": {"type": "string"}},
                        "weaknesses": {"type": "array", "items": {"type": "string"}}
                    }
                }
            }
        }
    }
)
# resp.answer is a dict matching the schema
```

### Find similar pages
```python
similar = exa.find_similar("https://docs.python.org/3/", num_results=5, exclude_source_domain=True)
```

### Async usage
```python
from exa_py import AsyncExa
exa = AsyncExa()
results = await exa.search("async python", num_results=3)
```

## Best Practices

- Default `type="auto"` — only override if you have a specific need
- `search_and_contents()` includes full text by default — use `contents=False` on `search()` to skip it
- Always check `cost_dollars` in the response to monitor usage
- `answer()` with `output_schema` is great for structured data extraction
- Date filters: use `published_date` for when content was published, `crawl_date` for when Exa indexed it
- Domain lists use just the domain name — `["github.com"]`, no `https://` prefix
