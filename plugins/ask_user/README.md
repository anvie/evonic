# Ask User Question

Lets an agent pause mid-turn and ask the user 1–4 multiple-choice questions, like Claude Code's `AskUserQuestion`. The questions render as an interactive card in the agent's web chat; the agent's turn waits until the user responds, then the answers come back as the tool result and the agent continues in the same turn.

## How it works

The agent calls `ask_user_question` with:

```json
{
  "questions": [
    {
      "question": "Which database should we use?",
      "header": "Database",
      "multiSelect": false,
      "options": [
        {"label": "SQLite", "description": "File-based, zero setup", "recommended": true},
        {"label": "PostgreSQL", "description": "Full relational server"}
      ]
    }
  ]
}
```

Every question gets an automatic free-text "Other" choice. In the web chat a pending card docks above the chat input. With more than one question it becomes a wizard: one question per step, header chips to jump between steps, Back/Next, and Submit on the last step. Clicking a single-select option moves to the next step on its own, and Enter (shown as a keycap on Next/Submit) advances or submits unless the user is typing in the chat input. While a card is docked, the chat input, send and attach buttons are disabled, so the user answers or cancels the card. If the agent stops waiting (for example after a server restart), the card closes as "no longer open" and the input unlocks.

`recommended` is optional. It shows a "Recommended" badge and is never pre-selected. A single-choice question keeps only its first recommendation. A label written as `"SQLite (Recommended)"` (or `(Rekomendasi)`) is turned into the flag, so the answer text stays clean.

The tool registers the question and blocks. It has no timeout; it ends in one of four ways:

| Status | When | Result the agent gets |
|---|---|---|
| `answered` | The user submits the card | `answers`: header, question, answer text, selected labels, other text |
| `replied_in_chat` | The user sends a normal chat message instead (not from the agent page, where the input is locked) | `reply`; the message also follows as the next user message |
| `dismissed` | The user presses **Cancel** (or Esc) on the card | a message telling the agent not to assume answers and to carry on |
| `cancelled` | The user sends `/stop` | an error telling the agent not to assume an answer |

While it waits, the tool journals a small heartbeat event every minute so the stale-turn reaper does not kill the turn. It also journals `question_required` / `question_resolved` on the session's chat stream for any client that wants them. Once the card is answered, closed or no longer open, it simply leaves the dock and the input unlocks. The answer shows in the thinking timeline as the tool's result.

Interactive questions work only in the Evonic web chat. On Telegram/WhatsApp/Discord, API sessions, scheduler runs, agent-to-agent messages and simulations, the tool returns `status: "unavailable"` so the agent asks in plain text.

## Install

The plugin is self-contained and needs no Evonic core changes:

1. Zip the `ask_user` folder (the zip must contain `ask_user/plugin.json`) and upload it on the **Plugins** page, or copy the folder into `plugins/`.
2. Enable **Ask User Question**, then restart Evonic so its routes register.
3. Assign the `ask_user_question` tool (`plugin:ask_user:ask_user_question`) to an agent.

## Web UI

`routes.py` serves `static/ask_user.js` and `static/ask_user.css`. An `after_app_request` hook adds them to agent pages (`/agents/<id>`) while the plugin is enabled. The script polls `/api/ask_user/pending` for the page's chat session: every second while the agent is busy or a card is open, and every 4 seconds otherwise. It docks pending questions above the chat input and locks the composer. It relies only on these chat page element ids: `#chat-input`, `#chat-send-btn`, `#chat-attach-btn`, `#chat-file-preview` and `#chat-messages`. If Evonic renames them, update `ask_user.js`. The Sessions page is not covered.

## Endpoints

- `POST /api/ask_user/answer` — `{"question_id": "...", "answers": [{"selected": ["SQLite"], "other": null}]}`; one entry per question, in order.
- `POST /api/ask_user/cancel` — `{"question_id": "..."}`; closes the question without answering (`dismissed`).
- `GET /api/ask_user/pending?session_id=...&known=id1,id2` — questions still waiting in a session, plus `resolved`: how each `known` id that stopped waiting ended (`expired` if the server no longer knows it, e.g. after a restart).

All three sit under `/api/`, so the app's login and CSRF checks apply.
