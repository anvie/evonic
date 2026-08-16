# Changelog

## [1.2.0] - 2026-08-14

### Features

- Added visibility into running background jobs, opt-in job monitors, and completion notifications for the originating chat.
- Added custom slash-command bindings for panel actions.
- Added durable keyed memory facts with automatic supersession.
- Added rendered Markdown and source editing tabs to the plan-file viewer.
- Added shared WhatsApp routing controls, sender identity resolution, and direct-message-only agent support.
- Added focused highlighting for potentially dangerous code in approval prompts.
- Added a per-call LLM retry override.

### Bug Fixes

- Fixed sandbox artifact-registry access, background-job command parsing, and missing job-exit notices.
- Improved Codex stream failure reporting and provider/SSE diagnostics, including failing vision-model identification.
- Enforced external-channel file delivery and blocked local filesystem paths from chat links.
- Corrected workspace and tunnel file handling, including remote binary reads and virtual-path policy enforcement.
- Improved WhatsApp handling for attachments, documents, status broadcasts, newsletters, and shared-channel sender expiry.
- Corrected scheduler tidy timing and constrained Explorer cancellation and oversized output handling.

### Dependencies

- Added headless QR decoder support for ID-card processing.

## [1.1.2] - 2026-08-02

### Features

- feat: add AgentState task lifecycle foundation (#742) (1030932)
- feat(agent-state): enforce task lifecycle in tool loop (11f53b6)
- Task 744 synchronize agent state execution paths (fb1bbdf)
- Task 745 add task lifecycle feedback (05c9e70)
- Task 746: configure Vault Janitor schedule (c6e26c9)
- Task 746: expose Vault Janitor schedule in settings (53fbaaa)
- feat(kanban): add image attachments to kanban tasks (task #739) (c60816a)
- feat(kanban): modernize task image picker for task 739 (1ce20bf)
- feat(kanban): enforce English output for AI task enhancer (#740) (b59f4fd)
- feat(kanban): add comment attachments for task 741 (126635c)
- Forward Kanban follow-up attachments (e7e0837)
- feat(settings): group vision model dropdowns under Vision Models section (task #737) (305bca5)
- feat(#735): add copy code block buttons to article/document/artifact viewers (dc9ed2f)

### Bug Fixes

- fix(agent-state): self-heal stale task state on session wake (b456105)
- fix(llm_loop): protect eager skill tools (Explore) from mid-turn pruning (30712fb)
- fix(settings): prevent Vision Models mobile overflow (#737) (804c04c)
- fix(agent-sidebar): buffer busy events lost in page-load race (#738) (92b1c24)
- fix(kanban): use global fallback for enhance (task #734) (373fed8)
- fix(kanban): restore task creator controls (task #734) (2ba95b1)
- fix(templates): repair corrupted quotes in evaluate settings page (7ec212e)
- fix(scheduler): SEFTON tidy trigger uses last KB filing instead of last active (b2eca83)

### Chores

- chore: rebuild evonic.css via doctor and document scalar-array summarization (f326709)

## [1.1.0] - 2026-07-31

### Features

- feat: add evaluation model setting (#732) (d0e1bbf)
- feat(settings): add global default model fallback (57f1b53)
- feat(whatsapp): refine response style instructions (5529c85)
- feat(settings): add WhatsApp safe delivery controls (942bfec)
- feat(whatsapp): add outbound delivery safeguards (3df64e9)
- feat(settings): add WhatsApp safe-delivery API settings (6cb1458)
- feat(whatsapp): route outbound text through dispatcher (56c0a4f)
- feat(whatsapp): add global safe-delivery config, dispatcher, and natural formatter (893a189)
- feat(muktamar): add secret-free token verification endpoint and tests (dc89c63)
- feat(muktamar-api): cache photo validation results (6c5985b)
- feat(api): expose photo validation model index (#765) (01f7efa)
- feat(photo): lower minimum dimensions (7f31dca)
- feat: add agent channel debug listener (3cecb61)
- feat: make muktamar photo API stateless (d43677f)
- feat: expose muktamar photo validation API (243127a)
- feat(describe_image): auto-convert non-JPEG/PNG images and compress large files (db74c8f)
- feat: reusable workflow_guard plugin with generic core lifecycle hooks (e703ff8)
- feat(panel): add confirm_dialog support for panel actions (6402235)
- feat(ui): add hidden_slash_commands & disabled_slash_commands fields to Settings tab (#728) (19bf319)
- feat: per-agent slash command visibility and disable control (372cf0e)
- feat(agent_state): add random stale-task nudge for in_progress > 3 min (13c3be7)
- feat(ui): intraline diff highlighting for thinking-trail patch and str_replace (965d924)
- feat: add agent JSON export and import (aa78816)
- feat(direxplorer): support path alias in Read tool (28a1c3e)
- feat(plugins): add on_enable/on_disable lifecycle hooks with SDK injection (14ee6cb)
- feat(docker): persistent sandbox containers across sessions and restarts (f7cc781)
- feat: LLM-based operation classifier for set_mode trivial bypass (1727d03)
- feat(whatsapp-bridge): auto-recover from degraded (zombie-connected) state (551d973)
- feat: LLM-based operation classifier for set_mode trivial bypass (0d062f7)
- feat(settings): add Vision Fallback Model dropdown in System Settings -> General (0b1bbe3)
- feat: add vision fallback Web UI configuration in System Settings → General (0d35fbb)
- feat(describe_image): add secondary vision model fallback via env vars (#714) (1caa1c7)
- feat(llm_loop): optimize token consumption with tool pruning and skill SYSTEM.md compactor (#713) (edaa229)
- feat(active_context): enable enforced mode by default (2e09fc9)
- feat(#742): Enable direct image paste in Sessions page chat input (f65bcef)
- feat(doctor): add super agent core skills migration (section 10b) (1e91947)
- feat: super agent selective skill assignment (#739) (02abd77)
- feat: add always_execute flag to AgentState to decouple CMP from plan/execute mode (#737) (429d3da)
- feat(runtime): add active-context shadow projection (#711) (8815766)
- feat(settings): paginate shared channel routes (#708) (0e05cd1)
- feat: enhance /model slash command — no-args shows current model, /model list shows full listing (bb2731d)
- feat: support configurable systemd service management (fe72dd9)
- feat(runtime): add configurable full trail history mode (8347756)
- feat(cmp): improve path navigation and retrieval (5bde86b)
- feat: debug listener for shared channels WhatsApp inbound monitoring (6fc6217)
- feat(kanban): add agent search to selection modal (#697) (c1f8da3)

### Enhancements

- Improve guided slash command execution (a1c77b1)
- Add rebase and checkout safety hooks (868f1b7)
- Restore third vision model fallback (557fb51)
- Extract muktamar_api plugin from main repository (6c790f5)
- Fix sub-agent session index sync (e927abf)
- Fix Codex provider selection (7452903)
- Add ls alias for model listing command (3913e21)
- Move send_file path policy into core (a896adc)
- Add opt-in per-agent send_file path guard (d49d3d1)
- Fix assigned tools with disabled built-ins (b984c3a)
- Remove clear-memory slash command (#760) (43348dc)
- Remove Workflow Guard navbar menu entry (14d8b01)
- panel: switch button grid to single-column layout (8aa713d)
- Unify effective LLM request payloads (#725) (d725aff)
- Fix pruning of lazy skill tools (5ab7254)
- Make doctor fix output compact (6b45cb5)
- Improve Token Monitor usage accuracy and performance (0444740)
- Support sudo for restart when using systemd as service provider (5d5c2ca)
- Fix bidirectional escalation routing (#710) (95516a0)
- Require atomic implementation tasks (Task #735) (fa143a6)
- Improve batched task readability (Task #735) (987958a)
- Reset task list on set action (cc4a997)
- cmp: query-relevant waypoint auto-pinning + transcript excerpts (90eaaf4)
- Filter low relevance recall results (d064809)
- Add agentic_inbox lazy skill for Cloudflare Agentic Inbox email management (ed3ad16)
- #725: Perbaiki tampilan section User → Agent Routes — konversi ke tabel (e11f019)
- Add kanban_bulk_create_tasks tool to stop false "board error" failures (8b56cf0)

### Bug Fixes

- fix(tests): resolve remaining CI failures in unit test suite (d7f1560)
- fix(setup): add timezone validation and EVONIC_TIMEZONE persistence to run_setup (2b31a6b)
- fix: backport SlashCommand metadata (parameters, accepts_args, to_dict) and frontend parameter hints (32dc53f)
- fix(#700): re-apply troubleshooting URLs for vision errors (partial) (06af209)
- fix(#699): attachment button now reacts to settings toggle without page refresh (71ec1df)
- fix(describe_image): fall back to next model on rate limits and other transient errors (cad4322)
- fix(notifier): preserve explicit session on inactive channel instead of silent fallback (65564e1)
- fix(whatsapp): preserve throttled outbound queue items (046f3da)
- fix: expose photo validation reason codes (#764) (b07b904)
- fix(slash): archive sessions only with clear ar (2ff764e)
- fix: stabilize WhatsApp message lifecycle (d05f0b9)
- fix: render array values in tool results (5564bc3)
- fix: stabilize WhatsApp debug listener status (d129857)
- fix: return raw plugin configuration values (3435e04)
- fix: reconcile WhatsApp connection status (#761) (649fb92)
- fix: relay WhatsApp slash command responses (9620569)
- fix: harden photo API and completion routing (a87dd35)
- fix(background-jobs): support sandbox log retrieval (a3e22bf)
- fix(task-730): signal Bash process groups immediately on user stop (eeb02b0)
- fix: restore enabled agent messaging tool injection (4a31560)
- fix: strengthen autofill prevention for agent detail filters (4104f80)
- fix: prevent browser autofill on agent detail filter inputs (kb-graph-search, kb-filter, tool-filter) (93a7e91)
- fix(patch): use anchor position for read_offset hint instead of stale old_start (85e2e13)
- fix: filter messaging tool definitions by assignment (207d952)
- fix: validate kanban assignee is a real agent, prevent name-as-ID storage (4a521d4)
- fix: handle unavailable skills during agent import (c689b36)
- fix(codex): convert multimodal user content for Responses API (5bf37a1)
- fix: sanitize dict-format task strings in _sanitize_task_text and update_tasks (522bfb0)
- fix: allow explicit exec without plan file (task 729) (02b0bb2)
- fix: hide workflow tools for always-execute agents (8f16e82)
- fix: hide disabled ATG and CMP builtins (300f220)
- fix: restore runtime.py from truncated state causing NameError (d28e64d)
- fix(agent_state): strengthen _check_atomicity — check ALL tasks, add / delimiter (1775a1b)
- fix(#728): hide slash command autocomplete when all commands are disabled (077cad9)
- fix(bwrap): run keeper and nsenter as workspace owner to fix inaccessible bind sources (362b156)
- fix: persist attachment metadata in model context, summary, prefetch, and CMP transcript (05513a0)
- fix: respect ATG and CMP tool availability settings (20931ff)
- fix: enforce task atomicity in update_tasks via tool description, runtime heuristic, and render nudge (e2b80ff)
- fix(agent-state): enforce single active task (#727) (064e79c)
- fix(cmp): archive classifier calls with session context (#726) (bafbe2a)
- fix: honor message wrapper disablement (19f9aab)
- fix: prevent self-investigation (b108fc8)
- fix: prevent tool pruning from removing plan/execute transition tools (7e40076)
- fix: surface WhatsApp reach-out restrictions (5e2c2f3)
- fix: stabilize chat thinking indicator lifecycle (5e8f9cc)
- fix: emit terminal WhatsApp failure diagnostics (dc55733)
- fix: diagnose WhatsApp reach-out restrictions (bdc66b2)
- fix: retry WhatsApp 463 using alternate JID (6047c29)
- fix: preserve WhatsApp inbound JID routing (bdafff6)
- fix: prevent restart greeting loops (#9) (d1d0ca5)
- fix: initialize command list in _systemd_command to avoid UnboundLocalError (34233e2)
- fix(vision): allow fallback on api_error and add vision_supported check to priorities 1-2 (4fbb1a9)
- fix timezone issue (4806870)
- fix(#753): gate artifact tools behind artifacts_enabled flag (0ee7faa)
- fix(llm): preserve skill tools during pruning and prevent use_skill/unload_skill compaction (9defd84)
- fix: handle JSON decode errors in LLM client and model test route (f5878d9)
- fix(whatsapp): stop retrying on ACK 463 and fix Baileys 7.x Format B code extraction (5a7c51f)
- fix(whatsapp): bypass EventBuffer via pino logger hook for ACK 463 NACKs (9d201ee)
- fix(whatsapp): recover early ACK 463 failures (cb645ca)
- fix(shared-channels): item not disappearing after assign + add toast notifications (b99efcf)
- fix(whatsapp): track outbound delivery lifecycle (738d4f4)
- fix(ui): replace type=search with type=text to prevent WebKit pseudo-elements from overlapping placeholder (075dea0)
- fix(whatsapp): preserve auth while restoring message delivery (a4750d4)
- fix: reset textarea height after clearing input in sessions.html (8b9f618)
- fix(whatsapp): re-apply phone-JID fallback, add init-query detection, add send logging (ca336fb)
- fix timezone issue (75d6b84)
- fix(whatsapp): prevent 401 infinite loop and zombie sidecar processes (230a1f6)
- fix(patch): silently discard trailing LLM garbage in parse_hunks() (7a3af2b)
- fix: add missing setSaveLoading function to fix ReferenceError in knowledge editor (61599f4)
- fix(#753): gate artifact tools behind artifacts_enabled flag (83cc1eb)
- fix: sub-agent model inheritance — respect in-memory agent model_id (26bf014)
- fix: add Office document extensions to file accept attribute on sessions page (ef6a00f)
- Revert "feat(describe_image): add secondary vision model fallback via env vars (#714)" (16774b3)
- Revert "feat: add vision fallback Web UI configuration in System Settings → General" (0ee3029)
- fix(ui): prevent search icon overlapping placeholder text in filter inputs (1d443c3)
- fix(scheduler): use EVONIC_TIMEZONE for built-in cron jobs (janitor, attachments cleanup) (afe50e4)
- fix: remove direxplorer from super-agent core skills auto-assignment (590294a)
- fix: exclude logwriter.go from headless builds on darwin (127f7c1)
- fix(eval): dedicated code_output extraction format for the coding domain (7895963)
- fix(#740): remove duplicate 'Always Execute' setting in agent advanced settings (688af8a)
- fix(#739): replace super agent blanket authorization bypass with assigned-skills-based injection (0044e70)
- fix: add exponential backoff + liveness check in bwrap keeper spawn (ead1830)
- fix(bwrap): add exponential backoff to nsenter capability probe (7ac643d)
- fix(#712): bound parallel tool future waits (97964be)
- fix(settings): compact shared channel routes table (#708) (4905cbb)
- fix(chat): reject stale thinking restoration (445511f)
- fix explorer model concurrency gating (9954eef)
- fix inter-agent completion routing lookup (d9197e3)
- fix bwrap keeper startup diagnostics (af59f2d)
- Revert "Improve batched task readability (Task #735)" (2ee9ee3)
- fix(cmp): execute trivial task branches directly (d6df40d)
- fix(sandbox): finalize Explorer container reuse (#709) (7c3347b)
- fix(sandbox): isolate attachment staging and explorer containers (6ac7674)
- fix: record Codex usage for Token Monitor (#734) (15cdde3)
- fix(chat): restore thinking bubble after refresh (#733) (b869ecb)
- fix(bwrap): report keeper startup cause (#731) (9d52538)
- fix(bwrap): wait for nsenter readiness (#731) (567827a)
- fix: auto-complete stale in-progress tasks to done on task list reset (2ff138c)
- fix(shared-channels): improve route names UI (686ff92)
- fix(explorer): pass real host path to explorer build_config (fixes #707) (0426304)
- fix(agent-detail): re-apply auto-save on Settings tab (d6b0596)
- fix: prevent pre/code overflow in right sidebar panels (c152e22)
- fix(sessions): prevent stale trailing thinking bubble on session switch (857cb8f)
- fix: guard agent chat attachments (#730) (a608190)
- fix: reject disabled attachment drops (#730) (c32182c)
- fix(evonet): stabilize idle websocket connections (#729) (4f2c3bd)
- fix: preserve WhatsApp quoted context (#728) (7353102)
- fix(shared-channel): use confirm dialog for route removal (#696) (1f3da1f)
- fix: map group_id to full group JID instead of sender bare digits (a7e1045)
- fix: use cf-access-jwt-assertion header instead of Authorization: Bearer (e86961d)
- fix(#727): resolve group_id -> sender JID in _jid_map for buffered worker path (fe757e6)
- fix(#726): skip disabled agents in nightly SEFTON tidy job (e78944f)

### Tests

- test(guided-slash): align model output assertion with current handler format (e85a40c)
- test(whatsapp): extend dispatcher safeguard coverage (a882dee)
- test(whatsapp): cover safe outbound delivery (4afd8d6)
- test: cover WhatsApp restriction warning delivery (0db4334)
- test: add regression tests for Explore.py path resolution (#704) (54331b0)

### Chores

- chore: remove evomem consolidation notes (5e73dec)
- chore(evonet): bump version to 1.2.7 (21a1fe5)

## [1.0.0] - 2026-07-17

### Features

- feat(scratchpad): enforce per-agent /tmp scratchpad for script tools (3479430)
- feat(agent-detail): instant auto-save on Settings tab + fix chat sidebar collapse (a1165f5)
- feat(ui): redesign background process modal (73d763b)
- feat(chat): support multiple image uploads (#89) (5ea3d88)
- feat(ui): live context bar via state_changed SSE event; fix Agent State header (4c97d95)
- feat(#722): auto-sync attachments to remote workplace on upload (8e00589)
- feat(#721): add attachment path resolution for portal_copy (b6418c4)
- feat: improve ChatView command and Bash modes (#88) (fc3433a)
- feat(cmp): per-path Actual token figure in the Session Path Map (2e3f34b)
- feat(cmp): add trail mode to strip tool_call/tool_output from LLM context (396309a)
- feat(cmp): four-state node lifecycle with wall-clock decay + lineage restore (93799c2)
- feat: extend chat drag-drop accept to include document formats (docx, xlsx, pptx, odt, ods, odp, rtf, epub) (5167fb3)
- feat: make scheduler lazy-loaded, disable hello_world skill (task #688) (1b64eee)
- feat(cmp): give the boundary detector recent-dialogue context (06b046c)
- feat: invalidate prefetched context when summary watermark advances (#687) (32192fe)
- feat(cmp): move Full/Offload tokens to the bottom on mobile (5e1d0de)
- feat(cmp): mobile-friendly Session Path Map modal (5d3f625)
- feat: add chat message timestamps (25b8443)
- feat: add background process modal with live log viewing (db06c23)
- feat: add WhatsApp disconnect badges and warning banner (1842a8e)
- feat(settings): add visual separator between provider groups on Models page (f2d6d44)
- feat(cmp): Full/Card token breakdown in the map detail panel (36db035)
- feat(cmp): per-path context token usage in the map detail panel (c623272)
- feat(cmp): opaque node fills, loaded-chain highlight, agent avatar hub (9db0334)
- feat(cmp): radial mind-map layout for the session path map modal (680c1a9)
- feat: add background process modal with live log viewing (80affd7)
- feat(cmp): name paths properly at creation (fd694aa)
- feat(api): context-usage fallback to compiled context after session clear (80d3bc6)
- feat(cmp): dynamic agent name as the session map root label (9c2356a)
- feat(cmp): level-based path map (A1/B1/C1) with parent edges + ancestor loading (f98c9d9)
- feat: add WhatsApp disconnect badges and warning banner (37376fc)
- feat(cmp): diagnostic logging for boundary decisions and classifier calls (eecfdd8)
- feat(ui): context usage monitor in Session State panel (7585f08)
- feat(cmp): user-selectable Path Detection Model in System Settings (22a0f22)
- feat(token_monitor): group test_* agents in By Agent chart (001f6f2)
- feat(cmp): session path map badge + graph modal in Session State UI (fbefbca)
- feat(settings): add visual separator between provider groups on Models page (5fd935d)
- feat(cmp): M4 — soft offload and card-first rehydration (0ea5636)
- feat(cmp): M3 — hybrid boundary detection and path lifecycle (0dc2d63)
- feat(cmp): M2 — compactor (interface-preserving path cards) (2f2b591)
- feat(cmp): M1 — session path store, navigable map, manual navigation (9a12e49)
- feat(codex): surface reasoning/CoT in the thinking bubble (594d632)
- feat(providers): provider/model hierarchy + OpenAI Codex OAuth integration (6c280bd)
- feat(atg): re-arm the plan/compile cycle for new complex tasks mid-session (4d8c563)
- feat(atg): Atomic Task Graph planning and execution layer (arXiv 2607.01942) (a86944b)
- feat(chat): use paper plane icon for chat send buttons (f3a6197)
- feat(sessions): support drag-and-drop file upload in session detail chat (bf45e84)
- feat(recall): expose KB source_file + snippet hint, cap results at top 5 (a8bcb3d)
- feat(session-state): show only running background processes as terminal-style rows (920e3b6)
- feat(chat): render send_file attachments as previewable cards (f743d7e)
- feat(describe_image): add attachment directory fallback with Levenshtein 'Did you mean?' suggestion (#683) (d20ea22)
- feat(skills): add core marker to explorer and direxplorer skill.json (#680) (afb065d)
- feat(kanban): archive session on kanban:finish (5e395e0)
- feat: broaden coding evaluator to multi-language, add KaTeX auto-render (4351b27)
- feat(agent-detail): align tab nav with System Settings design (04273d9)
- feat(conversation): add persona/writing-style tests (L1-L5) (9310805)
- feat(conversation): add English tests (L1-L5) + language-aware fluency (40cf195)
- feat(explorer): add inherit_parent_model skill variable (#659) (fda5e7b)
- feat(context): inject recall-enforcement rules into built-in Memory Retrieval Protocol (bce36c9)
- feat(bgjobs): auto-monitor background processes and surface them in Session State UI (40c425a)
- feat(shared-channel): support concurrent multi-number shared WhatsApp channels (b3b0e5b)
- feat: add cursor:pointer to all tab-btn elements in agent detail page (4b8e44e)
- feat(ui): vertical tab button list in left panel on agent detail page (f2bb7a9)
- feat: implement workplace-aware attachment file transfer for read_attachment and describe_image (f718922)
- feat: enforce 2-minute max timeout on describe_image tool (97ce923)
- feat(ui): revamp System Settings into a settings console (d24037c)
- feat(channels): shared WhatsApp channel — one number serving multiple agents (a0d5618)
- feat: add workplace_id, sandbox_enabled, bash_exec_enabled to update_agent tool (7b24528)
- feat(whatsapp): group awareness and sender attribution in agent context (c0a46ed)
- feat(kb): show last filed & last organized times in Knowledge tab (cd74427)
- feat(panel): hourglass loading indicator on running button; echo bash command in terminal (8967bec)
- feat: implement /dump slash command with real-time attachment delivery (af89655)
- feat(sandbox): persistent bwrap keeper so daemons survive between commands (5f557ba)
- feat(workplaces): add bwrap workplace type for sandboxed local execution (618c3c8)
- feat(sandbox): add lightweight bubblewrap execution backend (c8a150c)
- feat(panel): per-run hover button to forward output to the chat test session (3b4c822)
- feat(exec): optional on_output streaming callback; live panel output (5c36629)
- feat(models): migrate model ids to provider/model_name format (a8e101f)
- feat(panel): render ANSI colors and honor the agent's workplace on execution (9ed6887)
- feat(panel): fall back to system default user when run_as_user is unset (af483b8)
- feat(panel): revamp Panel tab into two-column palette + terminal log (3432c80)
- feat(plugins): let plugins provide agent tools via tools_file manifest key (3f28abd)
- feat: add MCP client tools and plugin (8c8da28)
- feat(audio): route voice messages through transcribe_audio tool instead of inline multimodal (3ba50c7)
- feat: add send_channel_message and list_sessions agent tools (08c4102)
- feat(chat): add drag-and-drop file upload to agent chat UI (0b37b73)
- feat(chat): add upload progress indicator on message bubble (6fa8655)
- feat(kanban): implement comment deletion (#707) (85e855a)
- feat(whatsapp): use group ID for allowlist check in group messages (a05db54)
- feat: add panel skill with agent tools for panel action CRUD (#705) (9582aea)
- feat(panel): add Panel Plugin core — plugin manifest, DB layer, Flask routes with threaded execution (#704) (fb3b221)
- feat(#702): add agent_tabs support to plugin system (backend + context processor) (f2ff915)
- feat(whatsapp): group mention fallback and debug logging (170fc8c)
- feat: WhatsApp group mention-only responses and reply context enrichment (43da770)
- feat(scheduler): skip idle agents in nightly SEFTON tidy (2bb1b1b)
- feat(cli): add Docker availability check to evonic doctor command (#634) (1cad1d2)
- feat(describe_image): prompt agent to write query in English (749de83)
- feat: change default KB organizer mode from agentic to sefton (dbc1ed5)
- feat(doctor): validate memory engine + KB organizer compatibility (21d86cc)
- feat(kb): add SEFTON organizer mode, KB Janitor, system alerts, and delete_file tool (7be4d71)
- feat: migrate 7 tool mocks from JavaScript to Python (dae4f15)
- feat(training): split dataset by source + drop incomplete turns (cafc5cf)
- feat(training): archive byte-exact LLM I/O for SFT dataset collection (da0fb2d)
- feat(agents): auto-assign describe_image tool when vision is toggled (9e63e3d)
- feat(#637): implement /kb-organize slash command for manual KB organizer trigger (ce6f8e9)
- feat(kb): cap KB listing to 5 recent files, point to recall for the rest (3062a99)
- feat: add Export button to skill detail page (#692) (c76ef1d)
- feat(#691): add 'evonic skill export' CLI command (a76a239)
- feat(safety): add root filesystem scan detection for find / and tree / commands (fcb7a87)
- feat(doctor): add orphaned tool assignment check (section 10) (031edd6)
- feat(settings): global KB organizer model with fallback to default (345d21e)
- feat: add Discord channel support (#79) (c5aad6f)
- feat(kb-graph): render thumbnail images on graph nodes with pulsing search highlight (c7bef33)
- feat(kb-organizer): thumbnail support end-to-end, skip no-op edits, sanitize examples (e2121a2)
- feat(memory): per-agent memory engine + KB organizer mode, with persistent cooldown (c9294cb)
- feat(ui): render wiki-links as clickable previews in chat messages (211610a)
- feat(kb-organizer): diff recent conversation too + log raw diff for analysis (c3b6256)
- feat(ui): add client-side pagination to KB docs list view (ff148b3)
- feat(ui): add node type filter chips to KB graph (7c8f173)
- feat(kb-organizer): reconcile dangling wiki-links to docs under a different name (c12103b)
- feat(kb-organizer): encourage entity extraction + wiki-linking existing docs (5cce2bc)
- feat(agents): add per-agent messaging ACL (whitelist/blacklist) (3caae30)
- feat(kb-organizer): feed context-padded delta, gate by delta score, fix vault Grep (3ff5f84)
- feat(ui): add search clear button and auto-pan to matched node in KB graph (5f0e832)
- feat(doctor): protect core Explore skills + skill/plugin binary requirement hooks (4a46fab)
- feat(ui): revamp Knowledge tab with graph-first layout and overlay controls (ca14886)
- feat(memory): dedupe self-duplicated KB docs (317130a)
- feat(memory): KB Organizer sub-agent to stop duplicate docs (615d711)
- feat(#633): add image embedding rules to KB Coaching section (aed7234)
- feat(#688): Add universal base64 blob filtering in LLM context injection (c19d756)
- feat(pinchtab): add output_mode parameter to pinchtab_screenshot (#687) (c64453f)
- feat(write_file): add name-collision warning when sibling file/dir exists (#632) (d256b61)
- feat(memory): add photo embedding rule to _AUTHOR_DOCS_PROMPT (81067bf)
- feat(#629): use evomem hybrid search for smarter KB deduplication (4897f6d)
- feat(kb): support 'event' doc type end-to-end (97e5a9f)
- feat(eval): add Knowledge Builder evaluation domain (2f1472c)
- feat(ui): explicit "override default summarizer prompt" checkbox (282975b)
- feat(kb): progress logging for extraction so it's visibly working (1d272a2)
- feat(kb): show entities + typed edges in the KB graph view (45c8a32)
- feat(kb): mandatory Entities section in summaries + remember→direct extraction (b1fce7f)
- feat(kb): entity/typed-edge graph extraction + retain named entities in summaries (5de571b)
- feat(kb): log KB extraction outcome at INFO for visibility (63c4f77)
- feat(kb): summarizer extracts knowledge into a unified KB; consolidate brain/→kb/ (94e06f8)
- feat(#626): KB File Modal two-tab view (Preview + Edit) (5c1f7c2)
- feat(#622): allow /_self/ directory listing and fuzzy path suggestions for typos (cbe75d0)
- feat(chat-ui): auto-expand tool results inline in thinking bubble (62fb26a)
- feat(version): show commit offset since last bump on dev branch (7daf8d8)
- feat(token_monitor): shorten large numbers with K/M/B suffixes in Token Monitor dashboard (2b324a5)
- feat: add delete button to artifact viewer modal (7ad4f48)
- feat(memory): instant remember() session pin + summarizer entity coreference (903bf09)
- feat(kb): full-width Knowledge tab and evomem force-sync button (f7a775f)
- feat(chat): add editor mode for composing long messages (68de08c)
- feat(doctor): check evomem against latest GitHub release and auto-update on --fix (74c2168)
- feat: evomem memory-engine auto-provisioning + setup integration (#78) (f723ab2)
- feat(kb): enforce mandatory frontmatter (title/description/type) and color graph nodes by type (1655fca)
- feat(describe_image): add vision model fallback on connection errors (#669) (9f435a8)
- feat(logging): add dedicated evomem log file via EVONIC_LOG_ROUTES (c6e676c)
- feat: render bot attachment inline in chat UI (6edc075)
- feat(explorer): model badge in agent state, explorer token source, default-enabled (4ecbc99)
- feat(explorer): inherit delegator's execution environment (e839d42)
- feat(skills): enforce skill dependencies on enable + dependency dialog (0a8d54c)
- feat(skills-ui): select dropdowns for model vars + render textarea type (6b8ecd0)
- feat(skills): add Explorer skill for path-targeted read-only exploration (f1ea361)
- feat(#605): tambah tombol hapus (trash icon) di artifact detail viewer modal (11f3013)
- feat(describe_image): add vision model fallback on connection errors (#669) (462eb93)
- feat(logging): add dedicated evomem log file via EVONIC_LOG_ROUTES (ae109a0)
- feat: render bot attachment inline in chat UI (170aa0f)
- feat(explorer): model badge in agent state, explorer token source, default-enabled (9f88ff7)
- feat(explorer): inherit delegator's execution environment (55f8890)
- feat(skills): enforce skill dependencies on enable + dependency dialog (b478312)
- feat(skills-ui): select dropdowns for model vars + render textarea type (777bad6)
- feat(skills): add Explorer skill for path-targeted read-only exploration (286ef1e)
- feat(#605): tambah tombol hapus (trash icon) di artifact detail viewer modal (28e72f7)

### Enhancements

- Rebuild tailwind.css to include missing .w-80 sidebar width; bump cache to v=8 (88067b5)
- Fix right sidebar overflow — add overflow-hidden to sidebar container + overflow-x-hidden/break-words to inner content cards (59e8dbf)
- Enlarge Session Path Map modal — 700x550px → min(90vw,1100px) × min(85vh,800px), bump cache to v=5 (2afb391)
- merge: resolve conflict in agent_detail.html (519b18b)
- Merge branch 'dev' into current: resolve conflict in backend/channels/whatsapp.py (5ce4d63)
- cmp: tune boundary routing — ordered rubric, map parentage, 2-word guard (f924e22)
- Bump MAX_PRESERVED from 2 to 3 (113d550)
- cmp: change preserved->archived from time-based to count-based cap (445bbd8)
- tag CMP LLM calls as cmp source in token monitor and archive training examples to session_archive.db (c8b06b4)
- doctor: remove optional env var checks (SANDBOX_NETWORK, LOG_FULL_THINKING, LOG_FULL_RESPONSE) (f4deb19)
- Trim JSON tool descriptions: list_artifacts, fetch_artifact, describe_image (2c9beaa)
- Trim JSON tool descriptions: str_replace, patch, save_artifact, send_file (60714de)
- Trim JSON tool descriptions: bash, runpy, read_file, write_file (a704616)
- Trim built-in tool descriptions for token efficiency (654d32d)
- Remove create_collection and switch_collection built-in tools (6912492)
- diag(concurrency): self-reporting deadlock watchdog + paused-gate logging (0fe8314)
- Set new model concurrency default to 3 (bb86321)
- Fix WhatsApp disconnect banner refresh (a38852b)
- Refine agent state and session interfaces (b19ad93)
- Refine agent state and session interfaces (eab9f23)
- skill unloader awareness (8cce2d1)
- kanban: wrap global state access with RLock (d781c37)
- add limit to recall output to max 6 items (67686a2)
- Remove Quick Actions section from dashboard (#684) (1677068)
- kanban: add dependency gating on trigger route, fix silent exception swallowing (2c59f58)
- kanban: add built-in Task Creation procedure to SYSTEM.md (192e31f)
- task#682: describe_image now resolves /_self/ virtual paths (baac47c)
- knowledge_builder: prioritize exact slug match over fuzzy title match in _find_doc (795f189)
- remove deprecated KB unit tests (test_kb_index.py, test_kb_listing.py) — 813 lines deleted (d905240)
- Tambahkan tombol Clear Incomplete di halaman riwayat evaluasi (975769f)
- task(#657): remove KB listing functions and call site from context.py (b0724b8)
- Fix external link di wiki-link preview modal — tambah delegated click handler (4df045d)
- Separate Skill Settings and System Prompt into side-by-side columns (e110fd2)
- build assets (e4421e8)
- fix tool calling mock for eval test add more conv test (dfd6cfa)
- Fix skill tools namespace conflict for relative imports (8cebe19)
- diag(whatsapp-shared): log active channel_id + route keys on route miss (8460123)
- clean up code (b9e6347)
- fix runtime error caused by bad docstring (333aaf3)
- bump update (f830554)
- Replace 'notes.md' with 'evonic.md' in tool description/error strings (02416b7)
- context: remove notes.md injection block from _build_kb_instructions (B1) and update Message Wrapper Protocol instruction (C1) (d83b915)
- Remove _NOTES_MD_TEMPLATE variable and notes.md creation block from api_create_agent (ec99363)
- Bump version to 1.2.5 (version.go + FyneApp.toml) (eaaf169)
- bidirectional smart-quote matching for str_replace (3300fba)
- evonet: add SIGINT/SIGTERM handler for CTRL+C shutdown on Linux (41c1a64)
- Fix Model Distribution card displaying UUIDs on dashboard (e765fba)
- fix bwrap resolveconf (f93322a)
- fix whatsapp upload image issue (test Fable) (949bc8e)
- bump evonet version to 1.2.1 (8cc482b)
- messaging tool not enabled (e7a4a5b)
- github_webhook: add CI event support (workflow_run, check_suite, status) (bd16a55)
- bump update (d35213a)
- Panel Plugin — Agent Detail Template: Plugin tab injection + JS loader (c7262bf)
- filter slash command (4cfcadf)
- Change default Chat Perspective from B to A on /sessions page (2539dde)
- Remove attachments_supported model field completely (a917997)
- Fix #645: Reset KB state on soft agent switch + epoch guards (6976050)
- #644 Fix PASS 2 extraction prompt selection to respect expected answer type (reasoning level 2 & 4) (2607f69)
- Conditionally assign fetch_artifact only for agents with workplace or sandbox (#642) (0cf155b)
- Replace em dashes with semicolons in KB Organizer description (195ea6e)
- #693 — Fix export filename format: {id}-evonic-plugin-v{version}.zip (4b5b7ec)
- #693 — Consolidate skill actions into dropdown menu (a29fe3e)
- Remove deprecated 'Add Tool' button from System -> Tools tab (#690) (6362601)
- Rename Cancel button to Close in Chat Editor Modal (b452046)
- bump update (426e77f)
- #689 auto-assign save_artifact & send_file to all agents as built-in tools (a029cba)
- E3: Replace notes.md references in test_kb_index.py (87694be)
- E3: Replace notes.md references in test_kb_listing.py (ae05b6c)
- G1: Replace notes.md with evonic.md in agent_info.json (7e1fb58)
- F1: Replace notes.md placeholder with example.md in agent_detail.html (81331e0)
- Replace 'notes.md' with 'evonic.md' in tool description/error strings (1289329)
- task(D2): replace 'notes.md' with 'evonic.md' in kb_graph.py test case (54680e7)
- context: remove notes.md injection block from _build_kb_instructions (B1) and update Message Wrapper Protocol instruction (C1) (339607c)
- Remove _NOTES_MD_TEMPLATE and both notes.md creation blocks from super_agent_tools.py (722d2b1)
- Remove _NOTES_MD_TEMPLATE variable and notes.md creation block from api_create_agent (cf1ac53)
- runtime: replace notes.md reference with remember() in WRAPPER_PREFIX (c04a43e)
- Reserve session/group types for collections; never mint flat session docs (a0091c4)
- Fix staleness flag: compare tz-aware and naive timestamps safely (b8b123f)
- Migration: convert legacy flat session/group docs into collection folders (2801ec4)
- Format KB doc preview: frontmatter card + rendered inline wiki-links (a605376)
- Use fixed _DEFAULT_KB_GUIDANCE for doc authoring (bbfba63)
- Adopt evomem doc model: inline-link docs + collections, drop entities/notes (1593420)
- Consolidate knowledge pipeline with inline wiki-links and auto-sync (03b0789)
- bump update (0c9cac0)
- add copy button to artifact text modal view (3ed174f)
- Revert "feat(chat-ui): auto-expand tool results inline in thinking bubble" (b5e6855)
- bump update (99cd60e)
- Fix KB graph node click 'File not found' — strip kb/ prefix from evomem slugs (cd97142)
- Fix KB graph node title — use # Heading or frontmatter description instead of filename (ad092d4)
- 607: Exclude runpy input from injection guard (output scanning preserved) (4430f95)
- 606: Exclude bash input from injection guard (output scanning preserved) (a0f8922)
- token-monitor: remove explorer badge, color agent name instead; explorer color in source chart (75b44df)
- 607: Exclude runpy input from injection guard (output scanning preserved) (4aede3d)
- 606: Exclude bash input from injection guard (output scanning preserved) (7a39f68)
- token-monitor: remove explorer badge, color agent name instead; explorer color in source chart (8665b1a)

### Bug Fixes

- fix(agent-detail): re-apply auto-save on Settings tab (d14dc15)
- fix: prevent pre/code overflow in right sidebar panels (810f5f6)
- fix: expose attachment IDs in model context (#690) (2f9a543)
- fix(#724): preserve explorer workspace from user-requested path for remote workplaces (3aa7b5a)
- fix: set desktop CMP map modal dimensions (c1577dd)
- fix: harden agent session routing (#723) (5b53652)
- fix: support Explore on remote workplaces (13b77b8)
- fix(#720): expose workplace_path in binary attachment metadata (7df058e)
- fix: stream agent state updates for task #689 (756e068)
- fix: anchor WhatsApp group sessions to group ID instead of individual sender (1c08e72)
- fix(ui): restore CMP and ATG toggles dropped by merge conflict resolution (35fb642)
- fix(cmp): route new sub-questions as dep_branch on the active path (cd2e372)
- fix(chatlog): compact tool outputs when history is rebuilt from the chatlog (77635d5)
- fix: resolve 3 failing unit tests on dev (3f6b393)
- fix: seed default system settings during setup and add doctor check (8679fe5)
- fix: allow channel-targeted sends through shared channels (ownership check) (9aaa258)
- fix: create separate session for WhatsApp group messages using group JID (9364b45)
- fix: auto-reconnect WhatsApp bridge on connection loss instead of manual re-pair (e21f05b)
- fix: make CMP session path map node graph readable in light theme (69c79d8)
- fix: raise CMP classifier max_tokens to 1024 to prevent generation_timeout (4935926)
- fix(sse): tolerate non-serializable values in SSE event payloads (79c6235)
- fix(bg-jobs): capture multi-line spawn commands; simplify process modal UI (42d44fc)
- fix(cmp): short retry/ack messages never switch paths (15c7375)
- fix(cmp): branch on subject change, not size — schedule->invoice case (feebd92)
- fix(whatsapp): detect slash commands in group messages before context wrapping (#685) (d9ea066)
- fix(whatsapp): preserve authentication across restarts (8016a4b)
- fix: populate botLid from creds when sock.user.lid is empty (2600568)
- fix(cmp): per-path (non-cumulative) Full/Card token display (d573dd8)
- fix: estimate context tokens locally when the provider omits usage (6a3016d)
- fix: context monitor frozen on Codex models + drop tool dumps from CMP rehydration (95a2170)
- fix(cmp): retry classifier calls once at doubled budget on generation_timeout (7104eb4)
- fix: robust first-object JSON extraction for all ATG/CMP LLM parsers (320d922)
- fix(cmp): token headroom for implicit-reasoning classifier models (4761434)
- fix: prevent reasoning_content fallback from stealing XML tool calls (#84 regression) (65ffb28)
- fix(panel): sanitize {{param}} substitution with shlex.quote (512aea7)
- fix(cmp): session map modal — Agent root + proper tree layout (8b59d1b)
- fix: populate botLid from creds when sock.user.lid is empty (1a92b2c)
- fix(cmp): approval guard only covers short messages (7a2f2bf)
- fix(cmp): deliverable-based boundary definition + recent-reply context for L3 (ce42c50)
- fix(cmp): LLM-led boundary detection — drop keyword topic heuristics (b8a0e5e)
- fix(ui): escape shortcode dots in /model list so markdown doesn't renumber them (db69ed5)
- fix(codex): retry on timeout instead of falling back immediately (21bccc7)
- fix(ui): compact recall result cards (f3cdd26)
- fix(codex): update fallback model list for Codex fetch-models (ab4b06d)
- fix(atg): trailing system message 400 on strict templates + string error detection (9dfaa6f)
- fix(atg): enforce compile_task_graph in save_plan for flagged complex tasks (106acf2)
- fix(atg): steer plan-mode agents to compile_task_graph instead of save_plan (e466faa)
- fix(chat-drop): textarea message input with Enter-to-send and auto-grow (d5d4dbe)
- fix: stop evonic FD-leak self-shutdown becoming a zombie (#81) (8fae744)
- fix: handle Qwen models putting response in reasoning_content (#84) (5a1680f)
- fix: stale thinking bubble deadlock in idle poll (130caf7)
- fix(safety): always require approval for find / and tree / root scans (db62541)
- fix(investigate): clear target session before submitting investigation request (7995afe)
- fix(dashboard): top-align main grid columns to remove empty stretch gap (3da8a1b)
- fix(use_skill): check skill existence before enabled status (043494a)
- fix(describe_image): use absolute path for attachment fallback suggestion (3bb2051)
- fix(send_file): route through workplace backend for remote agents (67ea221)
- fix(explorer): disable message wrapper for explorer sub-agents (2207405)
- fix(sandbox): skip .scratch/ redirect for explorer sub-agents (#39) (77d6727)
- fix(chat): keep wide markdown tables inside the bubble with L/R scroll (415bf60)
- fix(skill-detail): move Tools into right column under System Prompt (228a16c)
- fix(chat): perbaiki table scroll — containment di wrapper, bukan parent (7979722)
- fix(chat): cegah table overflow di chat bubble (0fa01a1)
- fix(chat): perbaiki syntax error unbalanced parenthesis di renderers.js (520822b)
- fix(#667): render KaTeX math (LaTeX) di chat UI (38a3882)
- fix(knowledge_builder): add slug fallback in _find_doc() for update actions (#666) (9d88324)
- fix: inject actual CWD path into agent system prompts and tool descriptions (74d3994)
- fix(approvals): make modal safety-net independent of SSE (busy-gate was self-defeating) (84267cc)
- fix(#665): fix remaining thinking section overflow in evaluation modal (7a4ab99)
- fix(evaluator): scope run matrix to the run's selected domains (d1b2129)
- fix(approvals): add pull-based safety-net so the web approval modal always appears (2eb5799)
- fix(kanban): prevent duplicate progress reminder nudge (088937d)
- fix(#665): prevent horizontal overflow of LLM output in evaluation detail modal (5f9f141)
- fix(#664): hide Resume button after Clear by clearing incomplete runs from DB (c18ad92)
- fix(#662): external link di KB editor preview tab buka tab baru (72c15f1)
- fix(sidebar): switch busy-spinner to shared RealtimeClient, fix SSE fallback bugs, resolve race condition (2cfa6e3)
- fix(evaluator): remove contradictory '2 malam' from tool_date_booking_3 prompt (bde6583)
- fix(evaluator): cap max_tokens on two-pass extraction and scoring summary (19d62d0)
- fix(evaluator): cap max_tokens on forced-answer and extraction LLM calls (95e93af)
- fix(stop): make /stop halt running bash/runpy near-instantly (b13f54a)
- fix(attachments): prevent upload when disabled, hide attach button in UI (87df520)
- fix(memory): KB organizer sub-agent now respects parent agent's default model (bc72235)
- fix: expand tilde in workspace paths and open external links in new tab (a99a8a2)
- fix(ui): remove vertical accent bar on active settings nav item (8beb830)
- fix(ui): make agent detail tabs responsive — horizontal scrollable on mobile, vertical panel on desktop (480fc48)
- fix(#655): preserve chat input text when opening file upload modal via drag-drop (b2a2bc0)
- fix(chat): recover session slug collision across channels (shared channel) (de3657f)
- fix(whatsapp): log exceptions from the callback handler thread (d33becd)
- fix(whatsapp): resolve phone JID from LID map when senderPn is missing (v7) (2b8ca38)
- fix(whatsapp): upgrade Baileys to 7.0.0-rc13 to fix send error 463 (7a8a3bf)
- fix(whatsapp): reply to phone JID for LID DMs (ack error 463) (972b03f)
- fix: fail fast on LLM connection errors so fallback kicks in quickly (82139c9)
- fix(whatsapp): retry sends on transient bridge disconnect (503) (aec4916)
- fix(whatsapp-shared): capture unrouted groups in inbox + diagnose reply loss (c81dc3b)
- fix: stop WhatsApp bridge from wiping session on transient 401 disconnects (c50e5a0)
- fix: add misfire_grace_time=3600 to built-in scheduler jobs (1be79c7)
- fix(#654): reset chat input height after message submission (0a82d2c)
- fix: remove m-auto from header-left-side so sidebar toggle button aligns flush-left with the 64px sidebar (668ccbc)
- fix: make AgentAPI sessions stateful — preserve chat history (f05490a)
- fix: auto-reject tool approvals for AgentAPI consumers (69e9273)
- fix(ui): add horizontal padding to quick search agent input (d67de08)
- fix(tests): mock bwrap availability check in sandbox workplace policy tests (a2ac012)
- fix(bwrap): add bwrap checker (d8bcfb0)
- fix(kb): organizer debounce refused every agent's first run on fresh-boot machines (7c5c6db)
- fix(evonet): surface RunRun error in pre-configured auto-run instead of exiting silently (dbb6d88)
- fix(tests): use monkeypatch to isolate organizer state in test (665da45)
- fix(tests): mock explorer.resolve_tool_ids in spawn test (8f4b44d)
- fix(tests): use _organizer_guard atomically for state cleanup (87fae1b)
- fix(tests): use pop/discard for cleaner organizer state cleanup (bfbea8c)
- fix(tests): patch _organizer_running leak and db dep in spawn test — resolves last 2 CI failures (e5c318a)
- fix(tests): also patch get_engine in brain fixture — resolve_memory_engine falls through to get_engine() when agent has no memory_engine key (eb201a4)
- fix(tests): also patch evomem_available in brain fixture so _author_docs doesn't exit early in CI (a6f4edd)
- fix(evaluator): update knowledge_builder system prompt with image harvesting instructions (084025c)
- fix(ci): upgrade Python from 3.9 to 3.11 (05678a3)
- fix: resolve 4 CI test failures in KB graph, organizer, and memory manager (5794a7c)
- fix(panel): keep plugin tab active on refresh; reload panel after agent soft-switch (585f3e6)
- fix(panel): prevent long button labels from overflowing under output terminal (06b1bc2)
- fix(evonet): stop login-env shell from stealing the TTY, breaking CTRL+C on Linux (4ed175f)
- fix: exclude WhatsApp bridge banner from fullscreen toggle selector (17feb46)
- fix: WhatsApp disconnected banner showing after exiting fullscreen chat (dec3e31)
- fix(kb): carry conversation images into SEFTON/non-agentic KB filing (4b4a804)
- fix(ui): align sidebar toggle button flush above avatar column on mobile (acfa874)
- fix(chat): prevent messages overflowing input on short mobile viewports (685391f)
- fix(agents): stop injecting send_as_bot as an assignable tool (6fe95be)
- fix(llm_client): strip reasoning_content on Cerebras requests (62fb9ba)
- fix(llm_client): drop Anthropic-format thinking field from OpenAI payloads (0e1e670)
- fix(agent_vars): stop duplicate injection_guard_enabled from case mismatch (4f836bf)
- fix: allow update_agent to reset model_id to global default (2ecdf10)
- fix(explorer): release model-gate during sync Explore wait to avoid deadlock (29f00df)
- fix(approval): scope approval requests to super-agent channel only (383753c)
- fix(whatsapp): clear typing state on every send path, not just direct replies (f9ea6a6)
- fix(github_webhook): suppress info log when agent not configured (d0ebf06)
- fix(tools): fix invalid Python snippets in run-as-user file ops (f75631c)
- fix(panel): make panel buttons work, live-update tab, add /panel slash command (dc31d2d)
- fix(tools): route file tools through exec backend for run-as-user agents (92c5d7b)
- fix(whatsapp): stop phantom typing indicator after final response (246cfca)
- fix(gui): About dialog Close button overlap and add native macOS About menu (9392da6)
- fix(whatsapp): unwrap ephemeral messages, LID mention fallback, verbose logging + doctor check (f3b0b11)
- fix(gui): crash on server switch via dropdown and widen server select (253e2dd)
- fix(whatsapp): self-heal bridge after logout and surface disconnect status (aa076e6)
- fix(explorer): prevent duplicate response in sync mode (64b525e)
- fix(chat): constrain drop modal size to prevent overflow (75dff6e)
- fix: summarizer deadlock when tail captures summary boundary (#651) (17b541a)
- fix(#650): Add user_message events for compaction → fallback visibility gaps (d719817)
- fix(#703): replace bp.make_response() with flask.make_response() in panel plugin (1971a6d)
- fix(#703): set g.agent_id in agent_detail route so plugin tabs render (7b2b88d)
- fix(#706): approval modal naming mismatch & shared RT regression (358d66b)
- fix(whatsapp): handle quote-only mention and reply-to-bot in groups (1f5c36d)
- fix: add auto-restart for WhatsApp bridge on unexpected crash (#700) (852454c)
- fix: cancel stale debounced typing timer after WhatsApp response (af58c09)
- fix: inject [Attached: id=N] line into web upload message text (#695) (110adb9)
- fix(#694): approval modal delivery — always escalate to both web SSE and messaging channels (418a249)
- fix: inject run_as_user awareness and fix HOME env for sudo -E (751310e)
- fix(model-router): use full /plugin/ prefix paths in README and plugin.json (25e25cd)
- fix: describe_image tool not sent to LLM + webp MIME detection fallback (5269a5e)
- fix: prevent duplicate variable keys in agent detail page (347bb60)
- fix(ornith-v3): disable native reasoning mode and add <thinking> tag support (#638, #639) (d954d16)
- fix(kb-organizer): deterministic 'link' op to stop no-op retro-link edits (80bc235)
- fix(skills): allow .git dir in export and install, keep validator-only fix (2fb7308)
- fix(skills): skip .git/ entries in skill zip export, validation, and extraction (fc8c0dd)
- fix(update): stop bogus 'update available' for git-describe builds ahead of the tag (e09a210)
- fix(token-monitor): group kb_organizer sub-agents per delegator in By Agent chart (1ea4dc1)
- fix(ui): hide New/Sync/Upload KB buttons on mobile view (5b869b3)
- fix(chat): prevent double final-response render in sessions view (f30b6c1)
- fix(kb-organizer): delta = truly-new lines only, stop old info reappearing (b1d7e2b)
- fix(memory): make the KB Organizer reliably capture & apply ops (6ad8879)
- fix(memory): feed the KB organizer real user+assistant turns (66e5323)
- fix(chat): don't 500 reading agent state for a removed sub-agent DB (1e164b4)
- fix(subagent): explorers are read-only; distinct kb_organizer identity (05ebb3c)
- fix: add from __future__ import annotations for Python 3.9 compatibility (830122d)
- fix(context): omit empty graph relation lines from KB listing (f1d81bb)
- fix(ui): discard stale tab data after soft agent switch (c83f81e)
- fix(ui): replace inline add variable with modal dialog; fix saveAdvanced race condition (b496a61)
- fix(#628): guard JS mock against empty stdout; replace throw with graceful JSON error in read_file.json (9737f89)
- fix(kb): use model-default max_tokens for KB extraction LLM calls (fbc5213)
- fix(#627): naikkan z-index confirm dialog ke z-[250] agar di atas modal viewer (a4b9b64)
- fix(chat-ui): render recall think/graph results instead of just item count (3b0a2d0)
- fix(sessions): load sub-agent/explorer chat from parent DB (edeb302)
- fix: optimize JSONL poll — skip full-file scan on fresh page load, flush writes for reader visibility (c5d3925)
- fix(chat): explorer sub-agent final response not auto-displaying (1d4a0d3)
- fix(update): stale update banner after server restart (c7fad15)
- fix(#621): delete legacy .evobrain.db before init_evomem to force re-init with .evomem.db (4271fd7)
- fix(#620): add init_evomem() auto-initialization in sync() (57d7114)
- fix(summarizer): prevent deadlock when cut falls on already-summarized boundary (#619) (0f1be01)
- fix(#618): forward agent reply to exact session via session_id metadata (3582d06)
- fix(#617): prefer VERSION file over git describe when it indicates a newer release (f5e9013)
- fix(sessions): show parent-agent name and #N for explorer/sub-agent captions (27a6651)
- fix: add logging in kb-sync failure paths (0b7ba3e)
- fix(plan): show full plan in UI viewer and raise LLM-context cap to 15k (9d6a239)
- fix: resolve attachment file_path to absolute path for agent context (3dc42fe)
- fix(send_file): resolve /_self/ paths to agent's local directory (33055e5)
- fix(#682): extend explorerDisplayName to handle sub-agents alongside explorers (f6586a0)
- fix(write-tools): add missing 'import logging' and 'logger' definition (5a2ba8f)
- fix(#684): use <path:filename> converter for KB file routes to support subdirectories (60257ff)
- fix(#682): more robust explorer detection — catch raw agent_name, fix hdr-agent (9fcf5ef)
- fix(#682): explorerDisplayName helper for inter-agent session titles (cf61f36)
- fix(#680): add dark mode styling to Cancel buttons across all modals (9f1fa1f)
- fix: hard-truncation safety net for tool outputs, Explore RTK filter, Grep size cap (#38) (33e3c22)
- fix(tests): add source_dir to test seed data to match evomem_client queries (0f88538)
- fix(#610): add INFO-level diagnostic logging to evomem sync pipeline (dbed2f5)
- fix(#614): preserve inter-agent routing in resolve_report_to_for_subagent_spawn (a8b5ef9)
- fix: bot file messages rendering as plain text instead of inline images (38010d5)
- fix(send_file): use SANDBOX_WORKSPACE instead of hardcoded /workspace (26641e6)
- fix(kb-graph): use source_dir instead of page_type to include note-classified KB pages (d40f5c6)
- fix(#610): trigger evomem sync after KB file edits via write_file, str_replace, and patch tools (1f3bc8b)
- fix(explorer): add {{query}} placeholder and focused guidance to system prompt and task directive (#609) (29fb090)
- fix: inject sessions page messages into active agent turn (task #608) (91193c6)
- fix(subagent): correct idle TTL docs to 30m; token monitor: highlight explorers (cd58732)
- fix(explorer): resolve /workspace and relative paths in Explore (ad57a51)
- fix: hard-truncation safety net for tool outputs, Explore RTK filter, Grep size cap (#38) (3088e46)
- fix: eval engine bugs — tool_calling errors, reasoning extractors, In… (#70) (d0e6aea)
- fix(tests): add source_dir to test seed data to match evomem_client queries (ea7b698)
- fix(#610): add INFO-level diagnostic logging to evomem sync pipeline (b3c8519)
- fix(#614): preserve inter-agent routing in resolve_report_to_for_subagent_spawn (0810c53)
- fix: bot file messages rendering as plain text instead of inline images (ee98890)
- fix(send_file): use SANDBOX_WORKSPACE instead of hardcoded /workspace (1138a31)
- fix(kb-graph): use source_dir instead of page_type to include note-classified KB pages (fa84172)
- fix(#610): trigger evomem sync after KB file edits via write_file, str_replace, and patch tools (f05b1b1)
- fix(explorer): add {{query}} placeholder and focused guidance to system prompt and task directive (#609) (1b2697f)
- fix: inject sessions page messages into active agent turn (task #608) (a83f9b0)
- fix(subagent): correct idle TTL docs to 30m; token monitor: highlight explorers (b42516b)
- fix(explorer): resolve /workspace and relative paths in Explore (209ab3c)
- fix(install): create local main and dev tracking branches after shallow clone install (4992a2a)

### Performance

- perf(atg): bound compile_task_graph latency (88fcca0)
- perf: extend WhatsApp QR polling interval from 3s to 10s (4a0b228)
- perf(agent_detail): drop 5s /busy poll in favor of unified status stream (ba31cb4)
- perf(kb): skip per-entity coreference LLM call by default (graph extraction speed) (1c7212c)
- perf(dashboard): optimize skills/skillset count (#683) (c4ea81b)
- perf(dashboard): optimize skills/skillset count (#683) (f16d3a8)

### Refactoring

- refactor(cmp): single-pass turn op + immutable graph invariants (fd4ce56)
- refactor(cmp): fully LLM-led boundary detection — no keyword guards at all (9014646)
- refactor(codex-oauth): singleton callback server with state-keyed auth flow (bd7f547)
- refactor(memory): remove passive long-term memory injection (1df2e4d)
- refactor: detect core skills from manifest with CORE_SKILL_IDS fallback (#681) (770bf9b)
- refactor(dashboard): streamline dashboard UI and add schedule widget data (521f09f)
- refactor(mcp_client): make plugin self-contained via plugin tools mechanism (10df0de)
- refactor(tools): remove built-in read tool, consolidate KB reading into read_file (f648162)
- refactor(eval): merge Long Conversation Recall into Needle in Haystack (b25f492)
- refactor(eval): merge SQL domain into Coding (4e3b759)
- refactor(eval): unify Knowledge Builder expectations into expect_actions object (bb20f21)
- refactor(eval): domain-level system prompt + entity-coverage scoring for Knowledge Builder (a822959)
- refactor(tools): unify kb_graph into recall(mode='links'); fix edge_type enum (4f890b6)
- refactor: unify skill-tool sync, always-group explorers, mandatory Explore query (6110307)
- refactor: rename _GUARDED_TOOLS to _ARG_GUARDED_TOOLS in injection_guard (d42e602)
- refactor(explorer): source worker tools from DirExplorer instead of cloning (923bd50)
- refactor: unify skill-tool sync, always-group explorers, mandatory Explore query (91aa642)
- refactor: rename _GUARDED_TOOLS to _ARG_GUARDED_TOOLS in injection_guard (fcc4982)
- refactor(explorer): source worker tools from DirExplorer instead of cloning (21090ae)

### Docs

- docs: add sefton mode and filing interval to .env.example (480e084)
- docs(readme): update with recent features and fix stale CLI references (edb8832)
- docs: correct graph_lookup/synthesize_memory comments to recall(mode=...) (466803d)
- docs(explorer): remove {{query}} and {{key}} placeholder notation from SYSTEM.md (dba7f39)
- docs(explorer): remove {{query}} and {{key}} placeholder notation from SYSTEM.md (2c128ff)

### Tests

- test: mock non-lazy scheduler skill in auto-assign tests (ff5eb7f)
- tests: adapt CMP lifecycle tests for count-based preserved cap (3c63c38)
- test(eval): add edit/rename/dedupe op cases to knowledge_builder domain (428d060)
- test(eval): add thumbnail scoring + cases to knowledge_builder domain (6a498df)
- test: update assertions after WRAPPER_PREFIX no longer mentions notes.md (8daf859)
- test: update test_kb_graph.py references from notes.md to evonic.md (f724df9)
- test: doc-model knowledge pipeline integration + update suite for new model (94f6ba8)

### Chores

- chore(tests): sanitize live-session names from fixtures to dummy names (bb808cb)
- chore(cmp): rename Card token label to Offload in the map detail panel (5a24a79)
- chore(evonet): bump version to 1.2.6 (d195757)
- chore: update readme (6ba5117)
- chore(kb-organizer): default min interval between filing runs to 30 minutes (8e046f1)
- chore: sanitize example personal name in memory prompts/comments (226fb24)
- chore(eval): resync knowledge_builder domain with live _AUTHOR_DOCS_PROMPT (6259994)
- chore(memory): remove legacy FTS5 KB prompts and helpers (c563a1f)
- chore: sanitize real personal name/path from examples and test fixtures (5c0bda6)
- chore: bump version v0.8.7 (7ea5270)


---

# Changelog

## v0.8.0

192 commits

### New Features (7)

- **Evomem Knowledge Graph Memory** — a comprehensive long-term memory engine with primary and fallback storage backends, knowledge-graph integration, and semantic recall. Agents now remember facts across conversations and can traverse relationships between entities, making them genuinely stateful over time.
- **KB System v2** — three new tools deepen the knowledge base experience: a graph traversal tool lets agents follow wiki-link connections between KB documents, enhanced listing surfaces staleness and graph-awareness metadata, and a canonical `_kb_index.md` index keeps the knowledge graph navigable. Agents also receive coaching prompts to maintain KB graph links automatically.
- **Sub-Agent System** — the `/sub` slash command lets you spawn sub-agents directly from chat for parallel work. Sub-agents execute without planning delays, deliver responses through inter-agent forwarding, and are protected by naming-pattern enforcement. New `inter_agent_clear_context` and `builtin_tools_enabled` settings give fine-grained control over agent behavior.
- **/detach Slash Command** — move long-running background processes (builds, downloads, compilations) out of the agent loop so you can keep chatting while work continues. Progress is tracked persistently and the agent notifies you when the job completes.
- **/investigate Slash Command** — inspect any agent's context from chat with `/investigate <agent-id> <context>`, surfacing session state, tool configuration, and runtime diagnostics without leaving the conversation.
- **Syntax Highlighting & Rich Terminal in Chat** — code blocks now render with syntax highlighting via highlight.js. Bash execution output appears in a dark terminal-styled block. Copy buttons appear on code blocks and blockquotes. A live artifacts strip appears between thinking and final response.
- **Kanban Task Workflow** — tasks now carry a `created_by` owner column with owner-based delete permission. `task_id` is returned at the top level of creation responses. Agents auto-post their final answer as a kanban comment when a task completes.

### Plugin's Features (3)

- **Token Monitor** — per-agent and per-model-source token usage tracking with a cost dashboard, giving visibility into LLM spending across all agents (token-monitor plugin).
- **Evonet Multi-Server Manager** — a dropdown UI and server manager GUI for the Evonet connector, letting you switch between multiple remote devices without reconfiguration (evonet plugin).
- **Evonet Exactly-Once Execution** — tool execution across WebSocket reconnects is now idempotent, preventing duplicate command runs when the tunnel re-establishes (evonet plugin).

### Enhancements (32)

- **Flat Repository Architecture** — the legacy supervisor daemon, release-mode detection, and multi-directory app-root resolution have been removed. The codebase now follows a flat single-repo structure, simplifying deployment paths and eliminating an entire class of path-resolution bugs.
- **Security Hardening Suite** — API rate limiting protects all endpoints with tiered limits and atomic enforcement. Security audit logging records authentication and authorization events for forensic traceability. User blocking prevents abusive accounts from accessing the platform. Login rate-limiter state persists across restarts via SQLite.
- **PromptPurify ML Always-On** — the L5e injection guard classifier now runs unconditionally, catching prompt injection patterns that regex-based guards miss, with a false-positive fix for benign security terminology.
- **PEM Private Key Detection** — the platform detects when private keys appear in tool output or file operations and routes through a user approval flow, preventing accidental key exposure to LLM providers.
- **Workspace Boundary Enforcement** — the Read, Grep, and Glob tools now enforce workspace directory boundaries, preventing agents from reading files outside their sandbox.
- **Session Archive** — `/clear` data is now archived to a dedicated `session_archive.db` instead of being permanently deleted. Recover cleared conversations when needed.
- **agent_info Tool** — agents can inspect any other agent's full configuration (tools, skills, channels, KB, artifacts, models) from within a conversation, enabling self-diagnostic workflows.
- **fetch_artifact Tool** — the reverse of `save_artifact`: agents can fetch files from the host artifacts directory back into the sandbox for inspection or processing.
- **Collapsible Inter-Agent Messages** — `[AGENT/...]` messages in chat now collapse into a compact header, reducing visual noise in multi-agent conversations.
- **System Prompt Full-Screen Editor** — a modal editor with dirty-check confirmation, ESC-to-close, and Ctrl+S save support for editing agent system prompts without cramped textareas.
- **Injected System Variables** — `{{key}}` placeholders in system prompts are expanded from message metadata, enabling dynamic prompt injection per conversation turn.
- **CRUD Rate Limit Raised** — the CRUD endpoint rate limit increased from 30 to 120 requests per minute, reducing friction during bulk operations.
- **Blocked User Admin UI** — an admin interface for viewing and managing blocked users, integrating with the user-blocking enforcement system.
- **Public History Warning** — a warning dialog informs users before they enable public session history, preventing accidental exposure of private conversations.
- **Performance: Chat Messages Index** — a composite database index on `(session_id, created_at DESC)` accelerates message pagination queries.
- **Doctor Improvements** — five new diagnostic sections: evomem safety check, promptpurify model check, list_artifacts consistency, asset build check, and LLM provider check (now optional). Doctor also suggests `--fix` commands after running.
- **Tailwind CSS v4 Build Pipeline** — a `build_tailwind.sh` script builds the UI stylesheets from the Tailwind v4 source, replacing ad-hoc CSS management.
- **Process Tracker Hardening** — enhanced process group and container cleanup for both local and Docker backends, reducing orphaned process leaks.
- **Avatar Compression** — avatars are now stored with compression variants, reducing bandwidth and improving load times on slow connections.
- **Active Session Indicator** — a green gradient on the sidebar highlights which agent session is currently active, so you always know where the conversation is happening.
- **Kanban Skeleton Loading** — the Kanban board shows animated skeleton placeholders while tasks load, giving immediate visual feedback instead of a blank screen.
- **Lightbox Filename Overlay** — image filenames appear in the lightbox overlay for quick identification when browsing multiple images.
- **Knowledge Tab Searchbar** — a search bar on the agent detail Knowledge tab lets you filter KB documents by name without scrolling through the full list.
- **/cd and /cwd for Remote Workplaces** — the directory navigation slash commands now work with agents on remote or tunnel-connected workplaces.
- **Attachment Info Injection** — file path metadata is injected into agent context when files are uploaded via the web chat UI.
- **Resume Evaluation** — the evaluation system now accepts domain-level input for more accurate session resumption.
- **Image Serving Concurrency** — images and avatars are served concurrently with caching, improving page load performance.
- **Sub-Agent Direct Execution** — sub-agents skip the planning phase and execute directly, reducing turn latency for delegated tasks.
- **Summarizer Filters** — `bash_exec` and `slash_command` messages are filtered from recap and summary context, keeping recaps focused on conversation content.
- **Fallback Model Reset** — the active fallback model flag resets on inter-agent clear, preventing stale model assignments.
- **Built-In Tools Toggle** — each agent can now independently enable or disable built-in tools via an advanced setting, rather than a global flag.
- **Bash Command Param** — the bash tool now supports a `command` parameter for direct command execution alongside the existing `script` parameter.

### Bug Fixes (43)

- **More Robust Image Attachment Handler** — the image feed is decoupled from the LLM pipeline. A dedicated `describe_image` tool gives agents control over when and how images are processed, fixing inconsistent image handling across different models and providers.
- **SSE Connection Storm** — stale connection counts now reset on startup, and the connection cap was raised, stopping the `too_many_sse_connections` error storm that flooded logs.
- **SSE Exponential Reconnect** — the SSE client uses exponential backoff for reconnects, preventing connection-limit exhaustion during network interruptions.
- **SSE Chat Sequence Gaps** — a contiguous `_chat_seq` counter in the unified chat producer eliminates phantom gap-fill requests that caused duplicate message rendering.
- **Intermediate Response Chunks** — `response_chunk` events no longer prematurely end the live turn, fixing truncated agent responses mid-generation.
- **Sidebar Layout** — the sidebar now uses absolute positioning anchored to the app shell, filling the full viewport height without empty space, and works correctly on mobile.
- **System Balloon Chevron** — the system message balloon chevron stays right-aligned when the balloon is expanded.
- **Download Button Position** — the chat image download button moved from top-right to top-left, no longer overlapping with image content.
- **Phantom Turn Resumption** — the `system` message type is now included in unreplied-type checks, preventing phantom turn resumption after system events.
- **Injection Guard False Positive** — a P0 false positive on benign security terminology (e.g., "bypass" in normal context) has been eliminated.
- **Qwen Parser Validation** — extracted tool-call identifiers from Qwen models are now validated, preventing corrupted parameter injection.
- **Gemma4 Parser Fallback** — the LLM loop checks for Gemma4 parser availability before falling back to Qwen, fixing parse failures on Gemma models.
- **Orphaned Tool Calls** — the tool-call repair logic now properly restores orphaned calls, preventing HTTP 400 "insufficient tool messages" errors.
- **Loop Detection Forwarding** — force-stop termination from loop detection now properly forwards to the delegating agent.
- **Calculator Routing** — the calculator tool routes to the real math backend instead of a broken Python mock.
- **CRUD Rate Limit Race** — `check_rate_limit` is now atomic, eliminating UNIQUE constraint violations under concurrent requests.
- **Chat Reads Exclusion** — cheap chat read/poll requests are excluded from the 10/min chat rate-limit tier, preventing rate-limiting of normal browsing.
- **CSRF Cookie SameSite** — the CSRF cookie `SameSite` attribute changed from `Strict` to `Lax`, fixing cross-origin navigation issues while maintaining protection.
- **CRLF Sanitization** — carriage-return characters in URL parameters are sanitized, preventing HTTP header injection.
- **Health Endpoint Redaction** — Docker version and disk usage details are redacted from `/api/health`, closing an information disclosure vector.
- **Approval Flow** — `approval_resolved` events now emit before re-executing approved tools, preventing race conditions in the approval workflow.
- **Kanban Avatars** — agent avatars now display correctly on the Kanban board, with initial-based fallbacks for agents without custom avatars.
- **Kanban Sub-Agent Tasks** — parents can update sub-agent tasks, sub-agents can update parent-assigned tasks, and unassigned task status updates are properly guarded.
- **Sub-Agent Session Index** — the session index now records the sub-agent's own ID instead of the parent's, fixing session lookup for sub-agent conversations.
- **Sub-Agent Artifacts** — artifact tools and routes for sub-agents correctly use the parent agent's ID, ensuring artifacts are accessible.
- **/sub Command Visibility** — super agents can now see and use the `/sub` command, and it's listed in `/help`.
- **Evonet Ping/Pong** — ping and pong control frames are no longer dispatched as RPC requests, preventing spurious errors in evonet logs.
- **Evonet Shell Environment** — `exec_bash` and `exec_python` on remote devices now honor the user's shell environment variables.
- **Doctor evobrain→evomem Rename** — the doctor command check uses the correct `evomem` binary name after the codebase-wide rename.
- **Doctor list_artifacts Check** — a new doctor section detects when the `list_artifacts` tool is missing from agents that have `save_artifact`.
- **Scheduler Timezone** — deterministic timezone handling prevents UTC-conversion errors that caused schedules to fire at wrong times.
- **Double Slash Command Response** — race conditions between SSE and POST delivery no longer produce duplicate slash command responses.
- **System Prompt Modal** — the system prompt editor modal now closes after a successful save.
- **Lightbox Single Image** — prev/next navigation buttons are properly hidden when the lightbox contains only one image.
- **Lightbox Navigation** — prev/next now works correctly across image artifacts, not just chat-embedded images.
- **Mobile Image Overflow** — `max-width:100%` on chat image skeletons prevents horizontal overflow on mobile screens.
- **CTRL+G Quick Search** — agent search is now case-insensitive, searches by both ID and name, and the modal position is lowered for better reachability.
- **Sidebar Height Fixes** — multiple iterations corrected the sidebar height from `100vh` through `calc(100vh - 100px)` to the final `calc(100vh - 56px)` with absolute positioning.
- **File Upload Context** — file paths are now injected into agent context when files are uploaded via the web chat UI.
- **Skills Tab Toasts** — persistent "Saved!" labels in the Skills tab are replaced with proper toast notifications.
- **Tool ID Encoding** — the `toolId` parameter is now properly encoded in the `editTool` API call, and alerts are replaced with toast notifications.
- **Evomem Recall Fields** — recall field normalization and capture title sanitization prevent YAML frontmatter parse errors in memory entries.
- **Secret Key Detection Tests** — pre-existing test failures in the secret key leak detection suite have been fixed, and tests are converted to proper pytest format.

## v0.7.0

158 commits

### New Features (10)

- **Image Lightbox** — full-featured image viewer with prev/next navigation, thumbnail sizing, and download button for chat images and artifacts. Browse visual content inline without opening new tabs or losing context.
- **Anthropic API Format Translation** — the LLM client now translates between OpenAI and Anthropic API formats, configurable per-model via an API format dropdown in the model modal. Connect Claude and other Anthropic-compatible models natively without a proxy.
- **Per-Agent Run-As-User Isolation** — configure a Linux user per agent for bash and runpy execution, with environment variables preserved across sudo boundaries. Each agent runs sandboxed under its own OS account.
- **Ctrl+G Agent Quick Search** — keyboard-driven overlay for instant agent search and navigation. Type a partial name and jump directly to any agent without touching the mouse or leaving the current page.
- **Scheduler Auto-Extend Trigger** — new trigger type that automatically extends running schedules, enabling perpetual scheduling patterns without manual renewal.
- **List Artifacts Tool** — new tool lets agents browse their artifact directory. Automatically granted to any agent that has the save_artifact tool.
- **Agent Sidebar Unread Indicators** — a blue dot and selection ring on sidebar avatars show which agents have pending responses, so you never miss a completed task while browsing elsewhere.
- **/shutdown Slash Command** — super agents can cleanly shut down the entire Evonic server from within a conversation, no terminal access needed.
- **Workplace CLI Subcommand** — manage workplaces from the command line with `evonic workplace`: list, inspect, and configure workplaces without the web UI.
- **Scheduler Log Tab** — the scheduler detail view now includes activity execution details, captured output, and timing for each scheduled run, making it possible to troubleshoot failures directly from the UI.

### Plugin's Features (2)

- **Exa-Search** — AI-powered web search capability for agents, enabling real-time information retrieval from the internet with structured JSON output and semantic content extraction (exa-search skill).
- **Obscura** — lightweight headless browser for web scraping, JS rendering, CDP server (Puppeteer/Playwright), and MCP server. A lighter alternative to PinchTab with no dependencies and a single binary (obscura skill).

### Enhancements (34)

- **Realtime SSE Consolidation** — five separate realtime event streams merged into one unified SSE endpoint, reducing browser connection overhead and eliminating race conditions between event sources.
- **PROMPTPurify L5e Injection Guard** — a compact ML classifier runs as a second-pass injection guard, catching prompt injection patterns that regex-based guards miss. Semantic analysis adds a layer beyond simple pattern matching.
- **CSRF Protection** — double-submit cookie pattern protects all state-changing endpoints against cross-site request forgery attacks. Automatically disabled during test runs.
- **Auto-Assign Non-Lazy Skill Tools** — when a non-lazy skill is assigned to an agent, its tools are now automatically registered without manual assignment. Prevents silently broken skills caused by forgotten tool configuration.
- **Evonic Doctor Consistency Checks** — two new diagnostic checks detect orphaned tool assignments: artifact tool consistency (section 9) and non-lazy skill tool consistency (section 10). Both support `--fix` to auto-correct mismatches.
- **Stale Session Injection Detection** — the runtime detects when an agent's session has been idle long enough for the context to be stale, injecting a staleness-aware prefix to keep the agent grounded. Configurable per-agent with sensitivity settings.
- **Save Artifact Source Path Routing** — artifacts can now be saved directly from file paths through sandbox and tunnel backends, eliminating the base64 encoding bottleneck for large files.
- **Evonet.md Default KB** — new super agent setups now ship with evonet.md as a default knowledge base, providing instant context about the Evonet tunnel architecture.
- **In-Place Agent Switching** — navigating between agents now swaps content without a full page reload, with soft-switch support for the super agent. Dramatically reduces wait time when bouncing between agents.
- **Unified Chat State/Summary** — `/chat/state` and `/chat/summary` merged into a single API call, halving network overhead on every chat turn.
- **Configurable Sidebar Agent Limit** — maximum visible agents in the sidebar is now configurable from System Settings instead of hardcoded.
- **Server-Side Search/Filter** — agent search and filtering moved to the backend, fixing the bug where search only matched the currently visible page.
- **Avatar Initials** — agents now display colored name-initial circles instead of generic placeholder icons, making agent identity instantly recognizable across the platform.
- **Chat Image Download Button** — every image in chat messages now has a download button overlay for one-click saving without right-click menus.
- **Build Operations Rule Injection** — agents with bash or runpy tools automatically receive instructions to run compilations inside tmux or screen sessions, preventing the agent loop from blocking during builds.
- **Artifacts Pagination** — the artifacts tab now paginates large collections with server-side search and filtering, keeping the UI responsive even with hundreds of files.
- **KB File Modal Auto-Grow** — the KB file editor textarea now auto-grows to fit content, eliminating nested scrollbars.
- **CSS Concatenation Build Script** — unified CSS build step produces a single minified stylesheet from modular source files.
- **cat_file_bytes Streaming Transfer** — file transfers across all backends use streaming instead of docker cp/shutil.copy2, supporting larger files without temporary disk copies.
- **Smart Quote Normalization** — curly/smart double quotes normalized before markdown parsing, preventing broken formatting from copy-pasted or small-model-generated text.
- **Scheduler Full Output Capture** — session_prompt output now fully captured and visible in the scheduler detail view for troubleshooting.
- **Summarization Diagnostic Logs** — skip reasons logged when summarization is bypassed, making summarization behavior debuggable.
- **Stale Boundary Event Stripping** — stale boundary events stripped from `/chat/events` to prevent ghost thinking bubbles after `/clear`.
- **Memory NULL-Dimension Backfill** — existing memories without dimension vectors backfilled so conflict detection catches all duplicates.
- **Relative Avatar Path Storage** — avatar_path stored as relative for backup/restore portability across different server deployments.
- **Telegram Auto-Populate Display Name** — agent display name automatically populated from Telegram profile data on first connection.
- **sudo -E Environment Preservation** — environment variables survive sudo elevation when running commands with run_as_user.
- **Toast on Agent Enable/Disable** — enabling or disabling agents from the detail page now shows a toast confirmation instead of silent action.
- **Python -c Instead of Heredoc** — bash execution uses `python -c` to keep stdin available for interactive `input()` calls.
- **Download Button Repositioned** — chat image download button moved to top-right overlay, keeping it accessible without cluttering the image area.
- **Allow Soft-Switch to/from Super Agent** — sessions no longer reject mode/agent change when switching to or from the super agent.
- **Workplace Detail Tab Alignment** — workplace detail page tabs now match agent_detail styling for visual consistency across the platform.
- **Slow-Request Logging** — requests exceeding 500ms logged with full path and timing for bottleneck identification.
- **Verbose Logging by Default in CLI** — CLI mode now matches GUI log output verbosity, giving consistent debugging output regardless of how you launch.

### Performance (11)

- **Agent Detail Page Speedup** — eliminated database write contention and redundant queries on agent detail page loads, cutting load time significantly.
- **SQLite Performance Tuning** — WAL mode, synchronous, and cache size PRAGMAs tuned for the platform's read-heavy workload. Thread-local connection pooling reduces WAL checkpoint pressure.
- **Buffer Events.Log Writes** — event log writes buffered to reduce filesystem directory churn on high-traffic deployments.
- **Cache app_settings** — SettingsMixin caches app_settings to avoid hitting the database on every page load.
- **Strip Empty Tool Descriptions** — OpenAI tool definitions omit empty description strings, reducing token overhead on every request.
- **DB Connection Lifecycle** — connections closed after requests with anchor to prevent WAL checkpoint stalls and file descriptor exhaustion.
- **Compiled Regex + Tool JSON Cache** — regex patterns compiled at module level and tool JSON definitions cached with mtime invalidation, eliminating repeated serialization.
- **Lazy Image Loading with Skeleton Shimmer** — chat images load on-demand with skeleton shimmer animation placeholders, improving initial page render time on image-heavy conversations.
- **O(log N) Event Boundary Lookup** — bisect-based boundary search in `get_events_in_range` for faster event retrieval.
- **LLM Client Settings Cache** — context_length, prompt_buffer, and max_retries cached with 30s TTL to avoid redundant settings reads.
- **Skill Manifest & Tool-Def Parsing Cache** — skill manifest JSON and tool-def parsing cached to avoid repeated filesystem reads on every tool invocation. Fixed a mutable cache bug where shared tool-def dicts were accidentally mutated across agents.

### Bug Fixes (36)

- **Sidebar prevents empty chat space** — max-height and align-self: flex-start on the sidebar container stops it from pushing empty space into the chat room on tall viewports.
- **PID start conflict** — single-instance prevention uses flock for atomic PID file access, fixing race conditions between parallel starts. Automatically skipped under pytest.
- **10 CI test failures resolved** — MagicMock leak across tests, API delete endpoint handling, PID file cleanup, and `_tlocal->_tls` typo in test fixtures all fixed.
- **Default KB not copied on web agent creation** — new agents created via the web UI now properly receive default knowledge base files, matching CLI behavior.
- **mkToggle race on agent pages** — rapid-toggle race condition on agents, plugins, and skills page toggles fixed.
- **Native confirm() replaced** — eager skill activation uses Evonic showConfirm() instead of browser's native confirm(), matching platform styling.
- **Browser autofill on search inputs** — autocomplete disabled on all search fields to prevent browser autofill from injecting unrelated values.
- **Continuation nudge disabled** — auto-continuation prompt injection deactivated to prevent unwanted agent behavior.
- **/summary accurate when summary unchanged** — slash command returns the correct message instead of a misleading error when nothing changed.
- **Missing clear_all_memories** — `/clear-memory` slash command now properly removes all memories instead of silently failing.
- **Contiguous per-session chat sequence** — sequence numbers contiguous per session, preventing SSE from seeing phantom gaps that triggered unnecessary re-fetches.
- **/summary AttributeError fix** — resolved `'AgentRuntime' has no attribute '_maybe_summarize'` crash.
- **Artifacts tools managed by feature toggle** — artifact-related tools controlled by the plugin feature toggle system instead of manual assignment.
- **Persistent 'Saved!' label replaced** — Tools tab uses disappearing toast notifications instead of a static label.
- **Path traversal escape in portal resolution** — path resolution hardened against directory traversal attacks escaping the portal root.
- **save_artifact error message improvements** — five fixes for unclear errors: missing filename, invalid filename, missing content, text-as-path misuse, and general exception context.
- **read_file directory error** — returns an actionable message when targeting a directory instead of a vague I/O exception.
- **Auto reply-back removed** — inter-agent auto-reply removed to prevent infinite ping-pong loops between agents.
- **str_replace/patch smart-quote robustness** — curly/smart double quotes in code no longer break str_replace and patch, especially for small models.
- **Flash-of-border on non-remote agent badge** — chat header badge no longer shows a brief border flash during initial render.
- **Lightbox window scope** — Lightbox exported to window scope so artifacts tab and non-chat views can invoke it.
- **SSE/polling leak on navigation** — SSE and polling connections properly closed on page navigation, preventing connection leaks.
- **Injection guard false positive** — base64-encoded file paths in CLI output no longer trigger the injection guard.
- **web_test bubble popup navigation** — notification bubble from web tests navigates to agent detail instead of sessions page.
- **Badge visibility for local agents** — workplace type badge resets className instead of using classList.add, fixing stale visibility state.
- **Stale runpy reference removed** — outdated descriptions referencing removed functionality cleaned from runpy tool documentation.
- **Enter-key on session reply input** — mobile/desktop Enter-key distinction now applies to session page reply input as well.
- **Kanban assignee blocked on done tasks** — completed and archived Kanban tasks can no longer have their assignee changed.
- **Auto-extraction from plan markdown removed** — task auto-extraction from plan markdown removed, fixing unintended task creation.
- **Early guard for missing file_path in read_file** — prevents AttributeError when `read_file` is called without a `file_path` argument.
- **Verbose lock debug removal** — `[LOCK] _llm_lock` debug logs silenced to reduce log noise.
- **Remove exa-py dependency** — unused exa-py removed from requirements.txt after exa-search skill migration.
- **Remove redundant artifacts injection** — duplicate artifacts SYSTEM.md injection removed from agents.py.
- **Replace Tailwind arbitrary classes** — arbitrary-value Tailwind classes replaced with inline CSS for more predictable thumbnail and lightbox styling.
- **Sidebar position:fixed** — sidebar positioning changed from CSS flex to `position:fixed`, preventing it from contributing to the flex container height and eliminating empty whitespace in the chat area.
- **Bypass is_skill_enabled in auto-assign** — `_exec_assign_skills` now bypasses the `is_skill_enabled()` gate when assigning tools, fixing an edge case where tools would silently fail to assign for newly-enabled skills.

## v0.6.78

145 commits

### New Features (13)

- **Agent Sidebar** — a persistent left sidebar showing agent avatars across all Evonic pages, with filtering, avatar images, toggle persistence via localStorage, and light/dark mode styling (#455). This gives you one-click access to any agent from anywhere in the platform, eliminating the need to navigate back to the agents page.
- **Message Wrapper Protocol** — every agent response now includes a structured wrapper with pre-response checks for memory storage and preference tracking. Configurable per-agent or globally, with automatic skip for short messages under 4 words (#465). This ensures your personal preferences, facts, and instructions are never missed across conversations.
- **Bubble UI Popup** — a notification bubble appears on the sidebar avatar when an agent sends a final response, with callout balloon styling and auto-suppression when you are already on that agent's page (#468). You will never miss a completed agent task while working elsewhere.
- **File & Image Upload in Agent Chat** — upload files and images directly in the agent detail chat interface (#490). No more switching to the sessions page just to attach a file to your conversation.
- **Audio & Video Multimodal Input** — agents can now process audio and video files as input for multimodal models. Extends the platform beyond text and images to handle voice recordings, video clips, and other rich media.
- **Semantic Memory Conflict Detection** — the memory system now automatically detects when a new memory contradicts an existing one, preventing inconsistent or conflicting facts from polluting your agent's knowledge over time.
- **Auto-Inject Agent Env Vars** — agent-specific environment variables are now automatically available in `bash` and `runpy` tool executions, with proper documentation in the system prompt. No manual export needed.
- **Health Endpoint** — a new `/health` endpoint reports database connectivity, disk space, and Docker container status (#61). Deployments can now integrate with uptime monitors and alerting systems.
- **Plugin Hot Reload** — plugins now reload automatically during development when source files change (#30). Plugin authors can iterate without restarting the server after every edit.
- **Outbound File Sending** — agents can now send files to Telegram and WhatsApp channels (#458). Your agents can deliver generated reports, images, or documents directly to your messaging apps.
- **Pre-Commit Safety Checks** — automated safety validation scripts for git commits (#493). Catches common issues before they reach the repository.
- **Clickable Plan Badge with Editor** — the plan badge in the Session State UI is now clickable, opening a full markdown editor modal where you can view and modify the active plan without leaving the page.
- **Skeleton Loading Placeholders** — the agent sidebar now shows animated skeleton placeholders while content loads, providing immediate visual feedback instead of blank space.

### Plugin's Features (1)

- **pinchtab_eval** — execute arbitrary JavaScript in browser tabs for advanced automation and evaluation scenarios (pinchtab plugin).

### Enhancements (41)

- **Thinking Bubble Auto-Expand/Collapse** — thinking bubble now auto-expands on message submit and auto-collapses when the turn completes (#494)
- **Evaluation Settings Tab** — new tab in system settings for configuring evaluator worker count (#488)
- **Sticky Fallback Model** — retry-aware persistence with intelligent context detection and dumb-truncation safety net, preventing infinite loops when fallback models are activated
- **Image/Vision Retry** — minimum 3x retry for image processing before falling back, with proper crash handling during model fallback (#480, #479)
- **Agent Model Columns Cleanup** — dropped legacy columns, renamed for consistency (#489)
- **Agents/Workplaces Tab Navigation** — standalone Workplaces page now has tab navigation between Agents and Workplaces (#484)
- **Schedule ID in Detail Modal** — schedule UUID now visible in the detail modal for easier reference (#483)
- **Confirmation Dialog for Non-Lazy Skills** — activating a skill that is not lazy-loaded now prompts a confirmation dialog (#481)
- **Auto-Inject Skill Cleanup Instruction** — all agent system prompts now include automatic skill load/unload cleanup rules (#469)
- **Eager Skill Tools Auto-Injection** — eagerly loaded skill tools are now auto-injected with skills_manager singleton reuse, eliminating redundant disk enumeration
- **Chat Input Enter-Key Behavior** — separate handling for mobile (newline by default) vs desktop (send by default) (#463)
- **Web Chat File Attachments** — file attachments displayed as downloadable cards in the web chat UI (#459)
- **Sidebar Agent Limit** — agent avatar list limited to max 15 entries for cleaner UI
- **Skill Unload Icon** — unload (X) icon added to skill badges in Session State UI (#457)
- **Fallback Model Reset Icon** — reset icon added to fallback model badge in Agent State UI (#456)
- **Sidebar Toggle Alignment** — toggle button width aligned to 64px to match sidebar width
- **Sub-Agents Force Execute Mode** — sub-agents now start in execute mode by default, reducing plan/execute friction
- **Evaluator Two-Pass Extraction** — exposed in UI and docs (#38)
- **Full-Stack Developer Skillset** — new pre-configured skillset template for full-stack development agents
- **PinchTab Evaluate Auto-Enable** — evaluate endpoint auto-enabled when disabled
- **PinchTab Occluded Element Guidance** — agents now receive hints for handling occluded elements and stale references
- **Tool JSON Definitions Cache** — cached with mtime invalidation, eliminating repeated JSON serialization (#474)
- **reencode_unicode_escapes Optimization** — list lookup + isascii fast path for unicode normalization (#471)
- **Evaluator Parallelization** — domain-level tests run in parallel with sleep removed (#475)
- **Compiled Regex Patterns** — regex compiled at module level in sql_executor for faster repeated matching (#477)
- **O(log N) Event Boundary Lookup** — bisect-based boundary search in get_events_in_range (#478)
- **LLM Client Settings Cache** — context_length, prompt_buffer, max_retries cached with 30s TTL (#472)
- **Skills mtime Hash Cache** — avoids re-enumerating skill files from disk on every query (#473)
- **Agent Config Cache** — agent config cached in run_tool_loop to avoid redundant DB reads (#476)
- **Session Index Elimination** — cross-DB ATTACH/UNION ALL removed for session aggregation (#460)
- **Dashboard Query Optimization** — connection reuse, SQL pushdown, correlated subquery fix (#461)
- **Lazy Migration + PRAGMA** — lazy migration, removed redundant polling, SQLite URI PRAGMA optimization (#462)
- **KB Frontmatter in System Prompt** — KB frontmatter description requirement documented in system prompt
- **/_self/artifacts/ Virtual Path** — new virtual path alias for agent artifacts directory (#419)
- **Safety Toggles Moved** — Safety Checker and Injection Guard toggles relocated to Advanced Settings (#399)
- **Toast Notifications** — improved toast system with button re-enable and robust error parser (#395)
- **Model Test Connection Feedback** — loading spinner, success/error states with icons (#395)
- **Download UI Consolidation** — URL and button merged into one row, curl hint removed
- **Auto-Scroll Log View** — dark green color scheme with auto-scroll to bottom (#394)
- **Evonet Tunnel Awareness** — system prompt dynamically aware of Evonet tunnel workplaces
- **README Update** — CLI commands corrected, missing features documented, architecture diagram improved

### Bug Fixes (39)

- **`[Image]` Placeholder for Image-Only Messages** — chat now displays `[Image]` placeholder instead of empty messages for image-only responses
- **Gemma4-12B Bold Markdown Spacing** — fixed extra space after `**` in bold markdown output via post-processing regex anchored with negative lookbehind to avoid eating the space after closing bold markers, applied in both `llm_client.py` and `gemma4_parser.py`
- **Kanban Task ID Type Mismatch** — task_id normalized to string to prevent comparison bugs (#51)
- **Unreplied Chat Session Resume** — unreplied chat sessions now resume properly on server startup
- **Plugin Settings Attribute Error** — replaced non-existent `_plugins` attr with `list_plugins()` in agent plugin settings
- **Absolute Path Resolution** — `resolvePath` now handles absolute paths correctly
- **Slash Command Response Display** — slash command responses now appear immediately in sessions chat UI
- **`.env` File Permission Warning** — warns about insecure `.env` permissions when SECRET_KEY is auto-generated (#60)
- **Shallow Clone Remote Fetch** — install.sh reconfigures remote fetch after shallow clone to track branches (#59)
- **Thinking Budget Cast** — added `int()` defensive cast for thinking_budget in model config (#492)
- **/_self/ Path Resolution** — fixed eager SYSTEM.md migration and sub-agent effective ID handling
- **`[DONE]` Frontend Leak** — suppressed `[DONE]` from llm_response_chunk to prevent leaking into the UI
- **Multimodal Content in Wrapper Prefix** — `_apply_wrapper_prefix` now handles multimodal content (list type)
- **Scheduler Silent Message Drop** — fixed silently dropped messages with embedded routing info at creation (#487)
- **Scheduler Timezone Awareness** — bare run_date strings now properly timezone-aware (#486)
- **Sidebar Height on Mobile** — auto height on mobile to eliminate blank space (#485)
- **Workplace DB CHECK Constraint** — 'tunnel' type no longer rejected as invalid (#54, #482)
- **Plan Editor Modal Layout** — fixed height and textarea flex layout in agent detail (#418)
- **Read/Read_File Token Compression Exclusion** — these tools correctly excluded from token compression per user preference
- **KB Frontmatter Mandatory** — KB files now require frontmatter in agent instructions
- **Skeleton Placeholder Dark Mode** — fixed invisible skeleton placeholders with bg-gray-400 fallback (#466)
- **Plan Editor Event Listeners** — wrapped in DOMContentLoaded to prevent null element errors
- **Tomli Fallback** — added tomli fallback for tomllib on Python < 3.11
- **Context-Exceeded Retry Guard** — llm_error retry now guards against context-exceeded errors
- **Sub-Agent Chat History** — sessions page now renders sub-agent chat history readably
- **Unreplied-Chat Scan Scope** — startup scan limited to human-facing sessions (#32)
- **File I/O Routing** — file I/O tools routed through workplace backend even when sandbox is disabled (#464)
- **SSE Response Bubble** — final response bubble now renders synchronously from SSE stream
- **Dead TONES Lookup** — removed dead TONES lookup that broke Next on Super Agent setup step (#50)
- **Python 3.9 Compatibility** — PEP 604 union types (`str | None`) replaced with `Optional[str]`
- **SVG Avatar XSS Prevention** — SVG avatar uploads rejected to prevent stored XSS (#52)
- **Heredoc stdin Rebind** — stdin rebound to /dev/tty in pass_setup() for heredoc compatibility
- **Sidebar Light-Mode Styling** — fixed styling issues in sidebar light mode (#455)
- **Remote/Tunnel Workplace Check** — local filesystem workspace check skipped for remote/tunnel workplaces (#432)
- **Root Project Update** — fetch+reset replaces pull --ff-only for more reliable updates
- **Same-Version Update Block** — blocked redundant same-version updates with daemon crash log surface
- **Evaluator Sandbox** — mock test runner sandboxed with AST validation (#46)
- **Template File Allowlist** — `.env.example` template files correctly allowlisted for read_file access
- **Audio OGG-to-WAV Conversion** — Telegram and WhatsApp voice messages (OGG/Opus) are now automatically converted to WAV before being sent to multimodal LLM APIs that only support WAV/MP3 input formats. Includes graceful degradation when ffmpeg is unavailable (#500)

## v0.5.24

24 commits

### Enhancements (11)

- **Injection Guard toggle** — enable/disable Injection Guard per-agent with a simple toggle in Advanced Settings (#397)
- **Recall tool result contents in thinking bubble** — tool result contents visible directly inside the thinking bubble for easier context tracking (#398)
- **Auto-scroll + dark green log view** — log viewer now auto-scrolls to bottom with a dark green color scheme (#394)
- **Evonet tunnel workplace awareness** — system prompt now includes workspace information when using Evonet tunnels
- **`/_self/artifacts/` virtual path** — new virtual path alias for agent artifacts directory accessible via file tools (#419)
- **Plan badge clickable modal** — clicking the plan badge in session view opens a modal with full plan details (#418)
- **Safety/Injection Guard toggles moved to Advanced Settings** — relocated toggles from top-level to Advanced Settings section (#399)
- **Toast notifications + robust error parser** — improved toast notification system with a more robust error message parser (#395)
- **Model test connection visual feedback** — test connection button now provides clear visual feedback during model testing (#395)
- **Download URL/button merged into one row** — consolidated download URL and button into a single row for cleaner UI
- **Removed curl sample hint** — removed the curl example hint from the download section

### Bug Fixes (11)

- **UnboundLocalError on lazy skill unload** — fixed variable reference error when unloading skills that were never loaded
- **Approval modal 409 stuck** — fixed approval modal getting stuck on HTTP 409 conflict responses
- **`.env.example` read access denied** — fixed file read access error when accessing `.env.example`
- **Summarizer JSON template crash** — fixed crash caused by invalid JSON template processing in the summarizer
- **SSE thinking spinner stuck** — fixed thinking spinner getting stuck during Server-Sent Events streaming
- **Stale symlink false update banner** — fixed false "update available" banner caused by stale symlink references
- **[DONE] response content recovery** — recovered response content that was lost after [DONE] signal in streaming
- **Plan files per-agent sandbox path** — fixed plan file paths to use per-agent sandbox paths instead of shared paths
- **False positive `git add .gitignore`** — fixed false positive detection in git operations involving `.gitignore`
- **`str_replace` unicode escape mismatch** — fixed unicode escape sequence handling in str_replace tool
- **Update race guard + timeout** — added race condition guard and timeout to the update manager

## v0.5.0

255 commits

### New Features (11)

- **Agent Artifacts** - persistent file output system with `save_artifact` tool, artifact modal viewer, `read_attachment` tool with cross-agent isolation, delete endpoint with auth check, and attachment cleanup on session delete
- **RTK Token Compressor** - 8-stage modular compression pipeline with TOML schema, Python and Rust builtin filters, agent-specific and project-level filter overrides via KB, token savings tracking API (`/api/rtk/gain`), config knobs (`RTK_NO_COMPRESS`, `RTK_VERBOSE`), and safety net fallback
- **Thinking Budget Cap** - per-model round-based budget enforcement for small model efficiency (Phase 2)
- **Quality Monitor with Auto-Correction** - automatic correction and output parser for improved response quality
- **Long-running command guardrail** - detects build/compile commands and suggests tmux/screen alternatives
- **`/exec` slash command** - switch agent mode from plan to execute directly via chat
- **`forget_memory` tool** - long-term memory deletion for soft-deleting stale or irrelevant memories
- **`assign_skills` / `unassign_skill` super-agent tools** - assign and remove skills from agents programmatically
- **Evonic Backup System** - CLI-based backup, restore, and verification with `evonic backup` command (`evonic-backup-[YYYYMMDD]-[HHMM].tar.gz` naming format)
- **File upload in web chat UI** - upload files directly from the chat interface
- **Per-agent model fallback** - configurable fallback chain with 1 retry, persistence across sessions, and UI badge indicator

### Plugin’s Features (2)

- **Model-router plugin** - per-model base system prompts (`SYSTEM_PROMPTS`), model list endpoint, and token widget UI overhaul (model-router plugin)
- **Plugin widget mechanism** - auto-load `*_widget.html` in plugin detail page for custom UI

### Enhancements (53)

- **Search bars on /plugins and /skills pages** - client-side filtering for quick navigation (#362, #365)
- **Compact plugin and skill cards** - redesigned to match /agents card pattern with compact layout (#361, #364)
- **Token list SVG icons** - replaced text Edit/Delete buttons with SVG icons in API token list (#333)
- **Test Model feedback modal** - loading spinner, success/error states with icons, dark mode support (#371)
- **`/model` command simplification** - removed model UUID from output, formatted list as Markdown (#372)
- **Prompt-only skill badges** - show skills without tools as badges with divider line between Tasks and Skills
- **State API with loaded skills** - `/api/state` now exposes `loaded_skills` with skill badges rendered in sessions page (#359)
- **SSE bridge state-change trigger** - add `use_skill`/`unload_skill` to SSE state-change trigger list (#358)
- **Remove `Regular` category badge** - removed from non-system plugin cards (Robin feedback)
- **User-directory plugin UI refactor** - migrated to evonic standard styling with profile section cleanup (#331)
- **Evonet GUI improvements** - Clear button in toolbar (#343), version number in window title, FyneApp.toml for macOS metadata
- **Translate remaining Indonesian to English** - all Indonesian copy in `cli/commands.py` translated (#342)
- **Backup file format** - standardized naming to `evonic-backup-[YYYYMMDD]-[HHMM].tar.gz` (#341)
- **`send_agent_message` focus mode guard** - reject message when target agent is in focus mode
- **Script placement rule** - all scripts must be in `scripts/` directory; migrations in `scripts/migrations/`
- **Smart quote normalization** - `normalize_code_quotes` replaces smart quotes with ASCII equivalents in `str_replace` and `patch`
- **Remove TONE_PRESETS mechanism** - replaced with `{communication_style}` placeholder in super agent prompt template
- **Add `tmux` to tools Dockerfile** - added for long-running command execution support
- **Long-running guard error message** - inline run script into error message for weaker models
- **Upload evonic helpers to SSH** - auto-upload evonic helpers to remote SSH host on first `run_python` call
- **`nohup` PID file fallback** - nohup fallback now saves PID to file for cross-session tracking
- **Migration scripts cleanup** - moved all migration scripts to `scripts/migrations/`
- **Remove `_scripts/` directory** - consolidated into `scripts/`
- **Auto-load all skill tools for super agent** - skills auto-loaded for super agent; fix scheduler config type handling
- **Get version from GitHub Releases API** - instead of local git tags for more reliable update detection
- **Make `patch` handle JSON `\\uXXXX` escapes** - handles LLM double-escaping and JSON unicode escapes in context matching
- **`portal_copy` tool** - binary file transfer between workspaces and portals
- **Treat code files as plain text** - in artifacts explorer for better inline preview (#312)
- **Improve `read_attachment` tool** - with file parsing, access checks, and cross-agent isolation
- **Delete attachments on session clear** - purge files on session delete and clear-all sessions
- **Add `gh` CLI installation guide** - comprehensive guide for all OS in GitHub skill KB
- **Add EVONIC_BANNER refactor** - deduplicate banner, import from `cli.commands`
- **Structured logging for agent_messaging** - add agent_messaging tool inclusion in agent log routes

- **Intent-based Skill Injection** - dynamic tool guidance by injecting relevant skill context based on agent intent
- **Write-vs-Edit guard** - `write_file` now refuses to overwrite existing files, guiding agents to use `str_replace` or `patch` for surgical edits
- **Improved `patch` tool** - tiered fuzzy matching with exact, indent-tolerant, and unescape-tolerant fallback tiers
- **Process tracker** - immediate `/stop` interrupt for running tool executions via PID-based process tracking
- **Dynamic edit tool suggestion** - writes overwrite guard dynamically suggests the best edit tool based on agent's assigned tools
- **Channel user identity injection** - inject channel user identity into agent context for personalized responses
- **Dynamic enabled-agent roster injection** - inject live list of enabled agents into super agent system prompt
- **Cloud \u2192 Tunnel rename** - full rename across DB schema, migration, routes, config, templates, tests, KB files, and README
- **Scheduler `session_prompt` action type** - trigger full LLM sessions from scheduled jobs with tool access
- **Scheduler detail modal** - display `static_message` content in scheduler detail view
- **Improve `webhook` input filter** - per-event-type JSON filter configuration for webhook payloads
- **Sanitize Docker/container language** - remove container terminology from tool descriptions for non-sandbox agents
- **Telegram username allowlist** - enhance Telegram user allowlist to include username-based filtering
- **Accurate tiktoken token counts** - compiled context now shows memories and summary with precise token counts
- **Extract user-directory plugin** - moved user-directory plugin to its own independent git repository
### Bug Fixes (44)

- **False-positive continuation nudge** - fixed on report-style responses, completion/summary responses, and permission-seeking responses (sessions 67fd3ea1, 25ac767d)
- **Continuation nudge negation fix** - `PLANNING_RE` nudge negation broke out of loop instead of falling through
- **Spaced character evasion false positive** - fixed false positive on normal words
- **Safety pipeline import graceful fallback** - all tool files (`bash.py`, `patch.py`, `str_replace.py`, `write_file.py`) now wrap `safety_pipeline` import in try/except with warning log and graceful degradation
- **`_skip_safety` flag hardening** - requires strict boolean `True` to skip safety checks
- **Kanban `tool_guard` self-heal** - clears stale pending status for done/reassigned tasks
- **Dark mode UI fixes** - hover text on Advanced Settings button (#352), hover styling for session items (#360), fix dark mode for user-directory plugin modals and table
- **EvoNET build fix** - fixed evonet build and `portal_copy` for absolute paths
- **Old CHECK constraint migration** - handle old CHECK constraint during cloud-to-tunnel workplace migration
- **`/clear` chat input fix** - clear chat input after `/clear` command submission (#392)
- **Task text sanitization** - prevent inconsistent status indicator rendering from sanitized task text
- **Loaded skill badge persistence** - clear in-memory `_session_skill_mds`/`_session_skill_tools` in slash command handler (#373)
- **Add `from __future__ import annotations`** - for Python 3.9 compatibility
- **Max_lines zero head/tail** - fix max_lines=0 behavior in token compressor
- **`ls -la` regex fix** - handle `ls -la` output in token compressor filter
- **`git add` empty-input** - handle empty input in git add operations
- **False-positive SSH path detection** - fix false positive for normal paths containing `.ssh`
- **Artifact CSS fix** - missing `group-hover:opacity-100` CSS rule for artifact action buttons (#344)
- **Replace native `confirm()` with `showConfirm()`** - in `deleteArtifact()` for consistent UI (#344)
- **Fix misleading `Execution stopped by user`** - for sudo/signal deaths that were not user-initiated
- **`/help` command visibility** - fix `/help` showing `/cd` and `/cwd` commands to non-super agents
- **Auto-detect task completion status** - from embedded markers in text
- **Tool date booking L3 test fix**
- **Fix smart quote in `showTab()`** - replaced smart quote with regular quote
- **Use Jinja `{{ plugin_id }}`** - instead of global `PLUGIN_ID` in widget scripts
- **Fix `spaced_character_evasion` rule** - false positive on normal words
- **Fix nohup fallback** - PID file for cross-session tracking
- **Update token compressor filters** - fix filters for ls command
- **Fix agent detail Advanced Settings** - dark mode hover text
- **Fix eval page real-time logs** - escape HTML in Real-Time Logs (#335)
- **Fix session state task list display** - not shown in chat UI right panel (#226)
- **Fix `renderAgentState`** - now passes `session_id` and renders task list correctly (#226)
- **Fix portal Add button** - 6 JavaScript/HTML ID mismatches causing silent failure
- **Forward sub-agent replies to parent agent** - ensure replies reach correct session
- **Fix `escalate_to_user`** - deliver messages to both channel and web sessions
- **Fix slash command interception** - in `send_as_user` and scheduler routing for real users
- **Fix session persistence for mobile web** - ensure mobile chat state survives navigations
- **Fix trailing newline in patch.py** - when no lines remain after patch application
- **Fix normalize curly quotes** - in SQL answer extraction for reliable parsing
- **Fix restart ready message** - proper web chat thinking bubble for slash commands
- **Show webhook secret as plain text** - instead of masked for copy-paste (#212)
- **Fix missing build script** - chat-ui.js not regenerated after changes
- **Re-route SSE adapter after turn_split** - maintain real-time updates in monolith mode
- **Update progress persistence** - survive crashes during update with progress tracking and pre-flight checks


## v0.3.43

24 commits

### Enhancements (9)

- **Auto-refresh after saving valid workspace directory** (#232)
- **Migrate Tailwind Play CDN to pre-compiled CSS build** (#235)
- **Remove redundant CSS reset in style.css** (#235 follow-up)
- **Add divider between session list items in chat room sidebar** (#236)
- **Add `!important` to divider border to beat Tailwind CDN preflight reset** (#236)
- **Reposition lazy badge to right of skill name in skill card** (#230)
- **Refactor CLI** — deduplicate `EVONIC_BANNER`, import from `cli.commands`
- **Update ASCII art**
- **Docs: update AgentAPI README** — session behavior clarification

### Plugin’s Features (1)

- **AgentAPI stateless by default, opt-in stateful via `X-Session-Id`** (AgentAPI plugin)

### Bug Fixes (9)

- **Drop orphaned and duplicate tool messages from reconstructed context**
- **Guard JSONL history rebuild when prefetch cache is used**
- **Count semantic messages for JSONL tail scan limit, not raw entries**
- **Authorize lazy skill tools** — update `assigned_tool_ids` on load/unload/restore
- **AgentAPI plugin: treat system message as user message**
- **Inject synthetic tool responses for interrupted tool calls in history**
- **Add authorization guard in `real_executor`** — block unassigned tools
- **Fix(#277): use single braces for `current_datetime` in `DEFAULT_SUMMARIZE_PROMPT`**
- **Fix(#229): remove stale evonic shell helper references**


## v0.3.19

113 commits

### New Features (2)

- **Portal feature** — virtual path mapping for agent file I/O, enabling external filesystem access through agent tools
- **recall_sessions built-in tool** — query session summaries from database with keyword search

### Plugin's Features (2)

- **Webhook input filter** — per-event-type JSON filter configuration for webhook payloads (Github Webhook plugin).
- **AgentAPI token management UI** — create, edit, delete, and inspect API tokens for agent access (AgentAPI plugin).

### Enhancements (13)

- **Session State** — migrate mode/plan_file/tasks from Agent State to a dedicated Session State; rename Session Recap to Session State with mode badge and task/plan file display
- **Skill briefs and lazy/eager guard** — skill descriptions with load behavior control and visual badges
- **Lazy badge on skill cards** — visually indicate which skills use lazy tool loading
- **Stale sandbox cleaner** — robust cleanup of orphaned containers with clear-sandbox CLI command
- **Sandbox awareness injection** — inject sandbox environment notice into agent system prompt
- **Show skill ID in skills page card list** — display skill identifier alongside name (#213)
- **Render task text as markdown** — in Session State panel for rich formatting
- **Update navbar logo** — use mascot.png for improved branding (#208)
- **Structured logging** — add agent_messaging tool inclusion in agent log routes
- **Add LICENSE (AGPL-3.0) and COMMERCIAL.md** — clear licensing with commercial terms
- **Simplify sandbox naming** — use evonic-<session-id> pattern (#227)
- **Plugin export/import (.evop)** — package and distribute plugins as portable archive files
- **Push notification system** — proactive push notifications to users via scheduler with period/channel configuration

### Bug Fixes (28)

- **Inject current date/time into summarization prompt** — prevents LLM date hallucination in session summaries
- **Security audit fixes** — resolve C-1, C-2, M-4, M-6, M-7, H-5 findings from production readiness audit
- **Add .env file protection to file operation tools** — prevent credential leaks via read_file/write_file
- **Security: path traversal in skill installation** — prevent arbitrary file overwrite during install
- **Security: command injection in update manager** — prevent code injection via crafted version strings
- **Security: improve version comparison** — handle pre-release versions safely with backward compatibility
- **Ensure ~/.evonic is on main branch** — after clone/update operations to prevent detached HEAD
- **Preserve unsummarized assistant context** — in conversation tail for session continuity
- **Fix infinite loop from empty PLANNING_RE** — missing nudge counter increment caused runaway loop
- **Resolve 7 audit-identified bugs** — in runtime, llm_loop, and context subsystems
- **Fix thinking bubble position and LLM loop continuation** — various rendering state bugs
- **Fix session state task list display** — not shown in chat UI right panel (#226)
- **Fix renderAgentState** — now passes session_id and renders task list correctly (#226)
- **Make evonic importable in non-sandbox mode** — fix runpy import path (#228)
- **Fix portal Add button** — 6 JavaScript/HTML ID mismatches causing silent failure
- **Forward sub-agent replies to parent agent** — ensure replies reach correct session
- **Remove model UUID from /model output** — when called without args (#205)
- **Fix escalate_to_user** — deliver messages to both channel and web sessions
- **Add dark mode support** — for agent state UI text and evaluation conversation blocks (#209)
- **Fix slash command interception** — in send_as_user and scheduler routing for real users
- **Fix session persistence for mobile web** — ensure mobile chat state survives navigations
- **Fix trailing newline in patch.py** — when no lines remain after patch application
- **Fix normalize curly quotes to ASCII** — in SQL answer extraction for reliable parsing
- **Fix restart ready message** — proper web chat thinking bubble for slash commands
- **Show webhook secret as plain text** — instead of masked for copy-paste (#212)
- **Fix missing build script** — chat-ui.js not regenerated after changes
- **Re-route SSE adapter after turn_split** — maintain real-time updates in monolith mode
- **Update progress persistence** — survive crashes during update with progress tracking and pre-flight checks


## v0.2.6

126 commits

### New Features (4)

- **Sub-agent system** — ad-hoc sub-agent spawn/destroy/list via dedicated skill with SubAgentManager singleton; sub-agents inherit parent's SYSTEM.md, KB, tools, and skills; session visibility, lifecycle cleanup, and parent-only messaging
- **/status slash command** — displays agent state info including model, description, tool count, and channel count (#159)
- **Update available notification system** — real-time progress UI with current-to-latest version display and auto-triggered status check
- **Clone model** — duplicate existing model configurations (#153)

### Enhancements (26)

- **Task complexity classifier** — automatically skips planning phase for trivial tasks to reduce latency
- **3-column agent selector modal** — revamped with avatar display and auto-select on click (#186)
- **Evaluation summary dark mode** — properly styled for dark theme (#150)
- **Chat input draft persistence** — saves draft in localStorage across page navigations (#157)
- **Optimistic comment append** — instant UI feedback with loading state on submit button (#156)
- **Model card action buttons** — changed from vertical stack to horizontal row positioned at right-bottom (#198)
- **/status output format** — separated Telegram vs web output format for better readability (task #200)
- **Remove Active badge from agent card** — cleans up agent card item UI (#188)
- **Delete interrupted evaluations** — interrupted/canceled evaluations are cleaned up immediately (#187)
- **GUI connection status** — updates status text after successful WebSocket connection (#158)
- **Left padding on "Connected." text** — via `NewPadded` for consistent alignment (task #158)
- **SECRET_KEY auto-generation during setup** — generated persistently during setup flow (#162)
- **.env file safety check** — `read_file` tool now checks for .env access to prevent credential leaks
- **Console log noise reduction** — suppresses `apscheduler.*` logs via `EVONIC_LOG_CONSOLE_QUIET` setting
- **install.sh now auto-detects latest stable release** — fetches latest tagged release for fresh installs instead of hardcoded version (#67)
- **Qwen XML tool call parsing** — adds support for Qwen-style XML tool call format in LLM client and evaluator
- **Agent Queue Workers setting** — configurable in system settings UI with DB-backed persistence (#169)
- **Max tool iterations setting** — configurable in web UI with DB-backed persistence (#169)
- **Manual save for non-toggle settings** — settings that don't use a toggle now have explicit Save button (#173)
- **About modal** — displays version, description, creator, and community links (#160)
- **User approval for inter-agent restart** — require explicit approval before one agent restarts another (#171)
- **Search bar in Workspaces page** — filter workspaces by name (#155)
- **Session ID in /status** — shows current session ID in slash command output (#192)
- **Allow partial assignee in Kanban** — empty assignee allowed in Assign Agents modal for partial assignment (#191)
- **Telegram channel pairing** — LLM extracts user name from introduction message during pairing flow
- **Kanban comment tool** — `kanban_get_comments` tool with pagination support (task #161)

### Bug Fixes (48)

- **Disable DEBUG mode by default** — fixes CVE risk of detailed error disclosure in production (#85)
- **Hard-fail when SECRET_KEY is missing** — removes `.secret_key` fallback to prevent insecure defaults (#162)
- **Auto-generate persistent SECRET_KEY** — removes hardcoded default, generates random secret on first run (#162)
- **Read SECRET_KEY from .env** — fallback to prevent key regeneration on restart
- **Do not write empty api_key if not set** — prevents overwriting existing API key with empty value
- **Atomic write in `_update_env_var`** — prevents partial .env corruption on crash (#164)
- **Systemic file descriptor and database leaks resolved** — closes file handles and database connections properly (#11)
- **Handle `None` metadata in restart_handler** — prevents crash when context builder returns no metadata (#166)
- **Sub-agent TTL expiry during active LLM loop** — prevents premature sub-agent destruction while LLM is still streaming
- **Sub-agent nesting prevented** — enforces max 10 sub-agents per parent and prevents recursive spawning
- **Sub-agents restricted to parent-only messaging** — sub-agents can no longer message arbitrary agents
- **Sub-agent session visibility** — sessions now appear correctly on Sessions page
- **Sub-agent tool ID and memory fixes** — runtime fixes for tool ID resolution, variable passing, and log path
- **Sub-agent report-back and lifecycle cleanup** — ensures proper cleanup on sub-agent destruction
- **`/cd` slash command not taking effect** — caused by prefetcher not validating directory change
- **Enable parallel sub-agent execution** — fixes blocking behavior that serialized sub-agent runs
- **Telegram re-add chat error** — fixes issue where removing and re-adding a Telegram chat caused prompt mismatch
- **Restart greeting sends static reply via channel** — prevents LLM-generated greeting after `/restart`
- **Filter slash command messages from LLM context** — prevents re-processing of slash command output as user input
- **Skip restart greeting when using `/restart`** — avoids duplicate greeting on intentional restart
- **Fix user messages interleaved between tool responses** — `_fix_interleaved_user_messages` handles edge cases correctly
- **Anchor slash command response after user message during active stream** — prevents slash responses overwriting stream content
- **Prevent duplicate thinking bubble on page load** — fixes race condition in chat-UI (#149)
- **Re-route SSE adapter to new turn after turn_split** — prevents stale SSE connection on conversation split
- **`/status` now uses agent model settings** — and fixes one-line markdown rendering in channel output (#159)
- **Summary generation failure** — wrong argument passed to `extract_content` in evaluator (#149)
- **Restore bullet points in Plugin Detail About tab** — formatting regression (#168)
- **Swap Logs and HMADS tabs in system settings** — incorrect tab ordering (#152)
- **Show enabled agents only in kanban assignment dropdown** — excludes disabled agents (#154)
- **Optimistic comment append wipes previous comments** — comment list replaced instead of appended (#156)
- **Select theme settings** — theme selection now persists correctly
- **Explicit `stream: false` in OpenAI-compatible payload** — prevents streaming issues with some providers (#8)
- **Use semver comparison for update availability** — fixes incorrect update detection with pre-release versions
- **Move model card action buttons to right side** — positioning fix (#198)
- **Display `file_path` in safety approval dialog** — shows which file is being accessed (#197)
- **Heuristic safety: reduce SQL false positives** — better pattern matching for destructive SQL detection
- **Release-mode path resolution and start parity** — ensures dev and release modes behave identically (#10)
- **Persist daemon PID for status/stop after upgrade** — daemon PID file survives upgrade process
- **Test mocks return proper defaults** — fixes `max_tool_iterations` default in test mocks
- **`fix(evalutor): summary generation always fails`** — wrong arg passed to `extract_content`
- **`fix(#148): read() KB tool path resolution`** — use `os.path.abspath(__file__)` for reliable base directory
- **`fix(#148): read() tool description for remote vs local`** — tailored description for each context
- **`fix(#148): add /_self/ path handling`** — ensures `/_self/` prefix works correctly in KB tool
- **`fix: remove duplicate _is_safe_redirect_url`** — and correct weekend logic in `check_price`
- **`fix(evonet): enable cross-compilation`** — macOS/Windows builds from Linux now work

