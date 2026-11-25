# Built-in agent Decision Profiles

Evonic can optionally route three bounded agent judgments through the generic Decision Models runtime. These profiles are **opt-in**: absent, disabled, invalid, shadow-mode, timed-out, malformed, incompatible, unavailable, or low-confidence results always preserve the existing deterministic or legacy implementation.

## Profiles

| Key | Result | Bounded Playground input | Legacy fallback |
| --- | --- | --- | --- |
| `agent.task_complexity` | `complexity`: `trivial` or `complex` | `task_text` (2,000 characters) | Existing heuristics and task-classifier call; defaults to `complex`. |
| `agent.path_boundary` | `belongs_to_current`: boolean | `user_text`, `active_goal`, `active_title` | Existing CMP detector. A positive profile result may only keep work on the active path; it never creates or switches a path. |
| `agent.goal_completion` | `decision`: `complete`, `continue`, or `blocked` | Goal, candidate response, at most 12 compact task-status strings, and nudge counts | Existing goal evaluator; it remains fail-open (`complete`) if evaluation fails. |

The `BuiltinDecisionRunner` allowlists these fields, normalizes whitespace, bounds text, and supplies a 3-second deadline. It sends neither conversation history nor raw tool output. Do not add secrets, credentials, attachment contents, or arbitrary metadata to these inputs.

## Profile configuration

Create a profile in the Decision Profiles administration UI using the stable key, the schema shown above, policy version `1`, and a Decision Models-compatible model. Set a conservative timeout (the built-in deadline caps execution at three seconds) and a confidence threshold appropriate for the operation.

Use `shadow` mode to validate a profile without changing agent behaviour. Only `enabled: true` plus `mode: enforce` allows a valid result to alter a consumer. `off` and disabled profiles make no provider request.

## Default decision models

A profile may leave its primary model empty. It then runs on the default decision models, an ordered chain of up to five models set in System > General > Models & Routing (`GET`/`PUT /api/admin/decision-default-models`). Each model in the chain gets one try, in order, within the profile's timeout. An active profile with no model is only valid while the chain is non-empty; without it the profile fails closed to its consumer's fallback.

## Playground presets

The administration Playground should expose only the profile's table inputs. The built-in presets are defined in `backend.agent_decisions.BUILTIN_DECISION_SPECS`; they are intentionally synthetic and contain no session data. Operators should test with similarly minimal, non-sensitive examples.

## Observability

Each invocation logs privacy-safe dimensions: profile key, `profile` versus `fallback` result class, fallback reason, and latency. The generic Decision Runtime also records its existing profile activity and provider/fallback outcome. Inputs and provider raw responses are not included in built-in consumer logs.
