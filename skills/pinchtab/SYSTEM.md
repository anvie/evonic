# PinchTab Browser Automation

## Overview
You have access to PinchTab — a browser automation framework that lets you control real Chrome browser instances through an HTTP API. With PinchTab, you can navigate the web, extract page content, interact with elements, take screenshots, and manage browser tabs.

PinchTab runs as a **sidecar process** — you don't manage its lifecycle. It must already be running at the configured host and port (default: `localhost:9867`).

## When to Use PinchTab Tools

- **Web research:** Navigate to documentation, read articles, search for information
- **Content extraction:** Scrape text or structured data from web pages
- **Form interaction:** Fill out and submit web forms
- **QA testing:** Verify web page content, check for elements
- **Screenshots:** Capture visual state of web pages

## How It Works

PinchTab manages Chrome browser instances with tabs. The typical workflow:

1. **Check health** (`pinchtab_health`) — verify the server is reachable
2. **List instances** (`pinchtab_list_instances`) — find available browser instances
3. **Open a tab** (`pinchtab_new_tab`) — create a new tab (optionally navigate to a URL)
4. **Inspect content** — use `pinchtab_snapshot` (accessibility tree, token-efficient) or `pinchtab_get_text` (plain text)
5. **Interact** — use `pinchtab_click` and `pinchtab_type` to interact with elements
6. **Capture visuals** — use `pinchtab_screenshot` when you need to see the page visually

## Tool Reference

### Connection & Discovery
| Tool | Description |
|------|-------------|
| `pinchtab_health` | Check if PinchTab server is reachable and healthy |
| `pinchtab_list_instances` | List all browser instances with their IDs |

### Tab Management
| Tool | Description |
|------|-------------|
| `pinchtab_new_tab` | Open a new tab in a browser instance |

### Navigation & Content
| Tool | Description |
|------|-------------|
| `pinchtab_navigate` | Navigate a tab to a URL |
| `pinchtab_snapshot` | Get accessibility tree (structured, token-efficient) |
| `pinchtab_get_text` | Extract plain text content |

### Interaction
| Tool | Description |
|------|-------------|
| `pinchtab_click` | Click an element by CSS selector or snapshot `ref` |
| `pinchtab_type` | Type text into an input element |

### Visual Capture
| Tool | Description |
|------|-------------|
| `pinchtab_screenshot` | Take a screenshot (base64-encoded image) |

## Selectors for Click & Type

When using `pinchtab_click` or `pinchtab_type`, the `selector` parameter accepts:

- **Snapshot `ref`** (recommended): Use the `ref` field from `pinchtab_snapshot` results, e.g. `"e21"`, `"e5"`. This is the most reliable way to target elements.
- **CSS selectors**: Standard CSS selectors like `"input"`, `"#submit-btn"`, `".nav-link"`, `"button[type=submit]"`.

**WARNING:** Do NOT use the `nodeId` field from snapshot results as a selector. `nodeId` is Chrome's internal DOM identifier and is not a valid selector. For example, if a snapshot node shows `"ref": "e21", "nodeId": 361`, use `"e21"` — never `"node361"` or `"361"`.

## Token Efficiency

**Prefer `pinchtab_snapshot` over `pinchtab_screenshot`** for content analysis. The accessibility tree is 5-13x more token-efficient than screenshots. Only use screenshots when you actually need to see the visual layout.

## Safety

- URL navigation is safety-checked: `file://`, `chrome://`, `javascript:`, and `data:` schemes are blocked
- Navigation to localhost/internal IPs is blocked to prevent SSRF
- All PinchTab tools run through Evonic's standard authorization guard — agents must be explicitly assigned these tools

## Configuration

PinchTab connection is configured via environment variables:
- `PINCHTAB_HOST` — hostname (default: `localhost`)
- `PINCHTAB_PORT` — port (default: `9867`)

## Error Handling

If a tool returns an error about PinchTab being unreachable:
- Verify PinchTab is running with: `pinchtab serve`
- Check the host/port configuration
- The `pinchtab_health` tool is the fastest way to diagnose connection issues
