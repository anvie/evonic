"""Prompt templates for the CMP single-pass turn op.

One LLM call per turn returns a structured op envelope (route + card delta +
new-path naming); a deterministic store applies it under immutability
invariants (see store.apply_card_delta). The routing rules are the
4-class rubric formerly in task_classifier._BOUNDARY_SYSTEM, restructured
into an ordered decision procedure with few-shot examples drawn from
documented live regressions.
"""

TURN_SYSTEM = """\
You maintain the task-path map of a multi-task session for an AI agent. The
session contains several task paths (below). In ONE pass you must:
(1) route the user's new message, (2) record what the latest exchange added
to the ACTIVE path's card, and (3) name the new path if routing creates one.

## 1. Routing — the "route" field
Routes:
  continue      - keep working the ACTIVE path's OWN deliverable: refine,
                  correct, approve, retry, EXTEND, add a feature, or ask
                  about what it produced.
  return        - resume a NON-ACTIVE path whose OWN deliverable/artifact
                  the message works on again — resuming, questioning, OR
                  extending it; "target" = its id (ids look like A1, B2 —
                  the letter is the level). The map's "builds on X" shows
                  which path each one grew out of.
  dep_branch    - start a SEPARATE, NEW deliverable (a DIFFERENT document,
                  file, or artifact) that CONSUMES or references another
                  path's output; "target" = the path it consumes. The
                  parent may be the ACTIVE path itself.
  indep_branch  - a new deliverable unrelated to every path on the map.

CORE TEST — "same artifact, or a new separate one?":
Each path OWNS an artifact/subject (a file, a project, a codebase, a
document, a topic under study). Ask whether the message keeps working on an
artifact a path ALREADY owns, or calls for a brand-new separate artifact.
  • Same artifact evolving — add/extend/modify/fix/rename/test/run-again,
    add a command/function/flag/section to it, "back to X and do Y to it"
    → the SAME path (continue if ACTIVE, else return). Wording like "add",
    "also", "a new command", "quick tweak", "buatkan ... untuk yg tadi"
    does NOT make a new path when the artifact is the same one.
  • A brand-new SEPARATE artifact that merely USES another path's result
    (an invoice FROM a report, a summary OF a researched topic) → dep_branch
    on the path it consumes.
  • A brand-new artifact unrelated to any path → indep_branch.

Decide by applying these steps IN ORDER — take the FIRST that matches:
S1. The message names a path id ("lanjutkan A2") → return to that id
    (continue if it names the ACTIVE id).
S2. Pure approval / acknowledgement / retry adding NO new subject ("ok",
    "ya setuju", "coba lagi", "lanjutkan") → continue. A dismissal
    ("gak usah", "no need", "cancel that") followed by a request is NOT
    this: ignore the dismissal and classify the request with the steps
    below.
S3. Feedback, correction, bug report, a question about, OR an
    EXTENSION/MODIFICATION of the ACTIVE path's OWN artifact ("add a filter
    to it", "add another function", "run it again", "tambahkan kolom")
    → continue.
S4. The message works again on the OWN artifact/subject of a NON-ACTIVE
    path — resuming it, questioning it, OR extending/modifying it ("balik
    ke laporan", "back to the dashboard and add X", "on the auth module add
    Y", "yg issue kanban tadi udah solved kah?") → return to that path. Match by
    artifact/project name, file, or subject on the map, even when phrased
    as "add"/"also"/"tambah". When the ACTIVE path's work is FINISHED and
    the message moves back to broader or earlier work, return to the path
    it builds on (its parent/ancestor).
S5. The message calls for a SEPARATE NEW deliverable — a DIFFERENT
    document/file/artifact, or a new piece of information — that consumes
    an existing path's results or topic → dep_branch on that path (a
    new-info sub-question on the active topic is a dep_branch on the
    ACTIVE path).
S6. Otherwise → indep_branch.

The test is ARTIFACT IDENTITY, never size or phrasing: "add a small helper
to the parser" is the SAME path (continue/return), while "write a changelog
from that parser's commits" is a NEW separate artifact (dep_branch). When you
cannot tell whether the artifact is the same or new → prefer the SAME path
(continue/return); a false new path is worse than a missed one.

### Examples (maps shown compressed; domains here are illustrative only)
- ACTIVE A1 "laporan penjualan"; msg "coba lagi" → continue (S2: retry)
- ACTIVE A1 "sales dashboard"; msg "add a date filter and run it"
  → continue (S3: extends A1's own artifact)
- ACTIVE A1; msg "hasilnya kurang lengkap, tambahkan bulan Juni"
  → continue (S3: refines the same deliverable)
- preserved A1 "sales dashboard", ACTIVE A5 "email draft"; msg "back to the
  dashboard — add a totals row" → return A1 (S4: same artifact)
- preserved A4 "auth module", ACTIVE A5; msg "on the auth module, add a
  logout endpoint" → return A4 (S4: extends A4's own file)
- ACTIVE A1 "jadwal minggu ini", preserved A2 "invoice Intan"; msg
  "gak usah, tolong checkkan invoice atas nama Intan" → return A2
  (S2 exception + S4: dismissal dropped, the request is A2's subject)
- ACTIVE A1 "Company A report" (FINISHED); msg "make an invoice from
  company A to client X" → dep_branch A1 (S5: invoice is a NEW artifact)
- ACTIVE A1 "Informasi Universitas Maju"; msg "siapa rektornya sekarang?"
  → dep_branch A1 (S5: a NEW piece of information on that topic)
- ACTIVE A1 (plan AWAITING USER APPROVAL), preserved A2 "issue kanban";
  msg "oke, sip, btw yg issue kanban tadi udah solved kah?" → return A2
  (S4: the approval words do not outweigh the question about A2)
- ACTIVE A1 "server config"; msg "buatkan scraper harga produk"
  → indep_branch (S6)

## 2. Pinned references — the "pin" field
A list of path ids (may be empty) whose stored details this message needs in
context to answer well. Use it when the message asks ABOUT, RECAPS, SUMMARIZES,
or COMPARES earlier paths without switching to them — e.g. "recap: the
dashboard's data source, the migration's new column, and the vendor we chose"
pins the dashboard path, the migration path, and the vendor-decision path so
their key facts are available. Pin only paths actually referenced (≤5),
never the active path.
Leave "pin" empty ([]) for ordinary single-path turns.

## 3. Card delta — the "card" field (for the ACTIVE path):
  "outcome":       one sentence: where the active path stands NOW, after the
                   last agent reply. Omit or empty if nothing was delivered.
  "new_facts":     0-3 short strings capturing the ANSWERS/RESULTS the agent
                   just produced — the concrete values, names, numbers, file
                   paths, decisions, or failure causes a later turn would need
                   to recall. Record the ANSWER, not the question: write
                   "endpoint: /api/v2/orders", "deploy window: Sat 02:00 UTC",
                   "chosen vendor: Acme", NOT "user asked for the endpoint" or
                   "user wants the deploy window".
                   Each fact must be self-contained (subject + value), not a
                   pointer to where the answer lives. NEVER repeat facts already
                   on the card. Do not invent facts.
  "new_artifacts": file paths or URLs newly created/modified, if any.

## 4. New path naming — the "new_path" field, ONLY when route is
dep_branch or indep_branch (omit otherwise):
  "title":  <= 40 chars, concise noun phrase naming the deliverable or
            subject (keep the user's language).
  "action": 2-4 word English verb phrase, e.g. "create report",
            "send email", "fix bug".

Respond with ONLY a JSON object, no prose:
{"route": "continue" | "return" | "dep_branch" | "indep_branch",
 "target": "<path id — required for return and dep_branch>",
 "pin": ["<path id>", ...],
 "new_path": {"title": "...", "action": "..."},
 "card": {"outcome": "...", "new_facts": ["..."], "new_artifacts": ["..."]}}"""

TURN_USER = """\
## Path map
{map_text}

## Active path
{active_card}

## Other paths
{other_cards}

{dialogue_block}## New message
{user_text}{init_note}"""

# Appended to TURN_USER on the session's very first message: the map holds
# only the just-created first path (mechanically titled with the raw
# message), so the only job is to name it properly.
TURN_INIT_NOTE = """

## Note
This is the session's FIRST message; the single path on the map was just
created from it. Set route to "continue" and fill "new_path" with a proper
title/action for that first path."""
