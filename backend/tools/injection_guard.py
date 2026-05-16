"""
injection_guard.py — Tool-level prompt injection detection using regex patterns.

Self-contained — does NOT import from shared/prompjector.py.
Patterns are ported from prompjector but implemented independently here.

Signature matches plugin_hooks.register_tool_guard():
    injection_tool_guard(agent_id: str, tool_name: str, args: dict) -> dict | None
"""

import re
import logging

_logger = logging.getLogger(__name__)

# ───────────────────────────────────────────────────────────────────────
# Severity Levels
# ───────────────────────────────────────────────────────────────────────

LOW      = "LOW"
MEDIUM   = "MEDIUM"
HIGH     = "HIGH"
CRITICAL = "CRITICAL"

_SEVERITY_ORDER = {
    LOW:      0,
    MEDIUM:   1,
    HIGH:     2,
    CRITICAL: 3,
}

_SEVERITY_SCORE = {
    LOW:      0.2,
    MEDIUM:   0.4,
    HIGH:     0.7,
    CRITICAL: 1.0,
}

# ───────────────────────────────────────────────────────────────────────
# Guarded Tools — only scan these tools for injection
# ───────────────────────────────────────────────────────────────────────

_GUARDED_TOOLS = frozenset({
    "write_file",
    "str_replace",
    "patch",
    "read_file",
    "bash",
    "runpy",
    "send_agent_message",
})

# ───────────────────────────────────────────────────────────────────────
# Per-agent Config Defaults
# ───────────────────────────────────────────────────────────────────────

_DEFAULT_ENABLED      = True
_DEFAULT_MIN_SEVERITY = MEDIUM
_DEFAULT_MODE         = "block"   # "block" | "warn" | "log"

# ───────────────────────────────────────────────────────────────────────
# Regex Builder Helpers
# ───────────────────────────────────────────────────────────────────────

_FLAGS = re.IGNORECASE | re.UNICODE
_ML_FLAGS = re.IGNORECASE | re.UNICODE | re.MULTILINE | re.DOTALL


def _r(pattern: str) -> re.Pattern:
    return re.compile(pattern, _FLAGS)


def _rm(pattern: str) -> re.Pattern:
    return re.compile(pattern, _ML_FLAGS)


# ───────────────────────────────────────────────────────────────────────
# Detection Rules — ported from prompjector, self-contained
#
# Each rule is a tuple:
#   (name, compiled_pattern, severity, category, description)
# ───────────────────────────────────────────────────────────────────────

_RULES: list[tuple] = [

    # ── 1. Direct Instruction Override ─────────────────────────────────
    (
        "ignore_previous_instructions",
        _r(
            r"\b(ignore|disregard|forget|bypass|override|discard|skip|cancel|dismiss)\b"
            r".{0,40}"
            r"\b(previous|prior|above|all|any|earlier|initial|original|system|your)?\b"
            r".{0,20}"
            r"\b(instructions?|prompts?|rules?|guidelines?|directions?|commands?|constraints?|training)\b"
        ),
        CRITICAL,
        "Direct Override",
        "Direct attempt to cancel previous system instructions.",
    ),
    (
        "new_instructions_override",
        _r(
            r"\b(from\s+now\s+on|henceforth|starting\s+now|new\s+instructions?|"
            r"mulai\s+sekarang|instruksi\s+baru|perintah\s+baru)\b"
            r".{0,60}"
            r"\b(you\s+(must|will|should|are|have\s+to)|kamu\s+harus|anda\s+harus)\b"
        ),
        HIGH,
        "Direct Override",
        "Inserting new instructions that replace original behavior.",
    ),
    (
        "do_anything_now",
        re.compile(
            r"\bDAN\b|"
            r"\b(do\s+anything\s+now|you\s+are\s+now\s+free|"
            r"jailbreak|jail\s*break|developer\s+mode|god\s+mode|"
            r"unrestricted\s+mode|unlimited\s+mode|no\s+restrictions?)\b",
            re.UNICODE,
        ),
        CRITICAL,
        "Direct Override",
        "Classic DAN / developer mode jailbreak patterns.",
    ),
    (
        "ignore_all_instructions_explicit",
        _r(
            r"\b(ignore|forget|bypass|override)\s+(all|every|any)\s+(previous|prior|above|earlier|initial)"
            r"\s*(instructions?|prompts?|rules?|guidelines?|constraints?)\b"
        ),
        CRITICAL,
        "Direct Override",
        "Explicit 'ignore all previous instructions' variants.",
    ),

    # ── 2. Role / Persona Hijacking ────────────────────────────────────
    (
        "act_as_persona",
        _r(
            r"\b(act\s+as|pretend\s+(to\s+be|you\s+are)|roleplay\s+as|"
            r"you\s+are\s+now|become|transform\s+into|simulate\s+being|"
            r"berpura.pura|bertindak\s+sebagai|jadilah|anggaplah\s+dirimu)\b"
            r".{0,80}"
            r"\b(ai|assistant|model|bot|system|character|persona|entity|human|person|expert|hacker|villain)\b"
        ),
        HIGH,
        "Role Hijacking",
        "Forcing the AI to adopt a different persona.",
    ),
    (
        "opposite_mode",
        _r(
            r"\b(opposite\s+(mode|version)|evil\s+(twin|mode)|"
            r"reverse\s+mode|anti.?(mode|version)|dark\s+mode\s+persona|"
            r"shadow\s+self|alter\s+ego)\b"
        ),
        HIGH,
        "Role Hijacking",
        "Requesting an 'opposite' version of the AI to bypass safety.",
    ),

    # ── 3. Delimiter / System Prompt Injection ─────────────────────────
    (
        "fake_system_delimiter",
        _rm(
            r"(\[SYSTEM\]|\[\[SYSTEM\]\]|<system>|<\|system\|>)"
            r".{0,400}"
            r"(\[/SYSTEM\]|\[\[/SYSTEM\]\]|</system>|<\|\/system\|>)"
        ),
        CRITICAL,
        "System Prompt Injection",
        "Fake system prompt delimiters attempting to inject system-level instructions.",
    ),
    (
        "system_instruction_injection",
        _r(
            r"\b(system\s+(prompt|instructions?|message)|sistem\s+(prompt|instruksi|pesan))\b"
            r".{0,100}"
            r"\b(is|are|now|updated|changed|replaced|diperbarui|diganti)\b"
        ),
        HIGH,
        "System Prompt Injection",
        "Attempt to declare system prompt as updated or replaced.",
    ),

    # ── 4. Prompt Leaking ──────────────────────────────────────────────
    (
        "prompt_leaking",
        _r(
            r"\b(reveal|tell\s+me|show\s+me|give\s+me|output|display|print|"
            r"what\s+is|repeat|echo|recite)\b"
            r".{0,50}"
            r"\b(your\s+(system\s+)?(prompt|instructions?|rules?|guidelines?|"
            r"training|internal|hidden|original|secret|base))\b"
        ),
        HIGH,
        "Prompt Leaking",
        "Attempt to extract the system prompt or hidden instructions.",
    ),
    (
        "prompt_leaking_id",
        _r(
            r"\b(tampilkan|berikan|sebutkan|keluarkan|bocorkan|katakan|"
            r"apa\s+(itu|adalah))\b"
            r".{0,50}"
            r"\b((system\s+)?prompt|instruksi|aturan|panduan|pedoman|rahasia)\s*(mu|kamu|anda)?\b"
        ),
        HIGH,
        "Prompt Leaking",
        "Attempt to extract system prompt in Bahasa Indonesia.",
    ),
    (
        "training_data_extraction",
        _r(
            r"\b(training\s+data|training\s+set|memorized|verbatim|"
            r"data\s+latih|dataset\s+kamu)\b"
            r".{0,60}"
            r"\b(repeat|output|show|reveal|extract|tell|share)\b"
        ),
        MEDIUM,
        "Prompt Leaking",
        "Attempt to extract training data.",
    ),

    # ── 5. Context Manipulation ────────────────────────────────────────
    (
        "hypothetical_framing",
        _r(
            r"\b(hypothetically|theoretically|in\s+a\s+fictional|"
            r"imagine\s+(if|that|you\s+were|you\s+are)|for\s+(educational|research|academic)\s+purposes?|"
            r"as\s+a\s+(thought\s+experiment|hypothetical)|"
            r"secara\s+hipotesis|bayangkan\s+jika|misalkan\s+saja)\b"
            r".{0,150}"
            r"\b(how\s+(would|could|do)|what\s+(would|steps|method)|"
            r"explain\s+(how|the\s+process))\b"
        ),
        MEDIUM,
        "Context Manipulation",
        "Framing harmful requests as hypothetical/fictional.",
    ),
    (
        "indirect_reference",
        _r(
            r"\b(without\s+saying\s+the\s+word|don.t\s+say\s+but|"
            r"imply\s+(that|how)|hint\s+at|between\s+the\s+lines|"
            r"encode\s+(the\s+)?answer|tanpa\s+menyebut|secara\s+tersirat)\b"
        ),
        MEDIUM,
        "Context Manipulation",
        "Requesting indirect responses to evade filters.",
    ),
    (
        "few_shot_manipulation",
        _rm(
            r"(example\s*\d+\s*:|example\s*:\s*\n|for\s+example\s*:\s*\n|"
            r"input\s*:\s*.+\n\s*output\s*:\s*.+\n.{0,200}"
            r"(ignore|bypass|override|jailbreak))"
        ),
        HIGH,
        "Context Manipulation",
        "Few-shot example manipulation to alter model behavior.",
    ),

    # ── 6. Obfuscation & Encoding ──────────────────────────────────────
    (
        "base64_encoded_payload",
        _r(
            r"\b(base64|b64|decode|dekode)\b.{0,30}"
            r"([A-Za-z0-9+/]{20,}={0,2})"
        ),
        HIGH,
        "Obfuscation",
        "Payload hidden in Base64 encoding.",
    ),
    (
        "hex_encoded_payload",
        _r(
            r"(\\x[0-9a-fA-F]{2}){5,}|"
            r"(0x[0-9a-fA-F]{2}\s*,?\s*){5,}"
        ),
        HIGH,
        "Obfuscation",
        "Payload encoded in hex.",
    ),
    (
        "unicode_escape_obfuscation",
        _r(
            r"(\\u[0-9a-fA-F]{4}){4,}|"
            r"(%[0-9a-fA-F]{2}){4,}"
        ),
        MEDIUM,
        "Obfuscation",
        "Obfuscation using unicode escapes or URL encoding.",
    ),
    (
        "leetspeak_evasion",
        _r(
            r"\b(?=.*[0-9])("
            r"1gn[o0]r[e3]|"
            r"byp[a4]ss|"
            r"j[a4][i1]lbr[e3][a4]k|"
            r"[i1]nstruct[i1][o0]n[s5]|"
            r"[o0]verr[i1]d[e3]|"
            r"h[a4]ck|"
            r"un[s5][a4]f[e3]"
            r")\b"
        ),
        HIGH,
        "Obfuscation",
        "Leetspeak used to evade keyword detection.",
    ),
    (
        "spaced_character_evasion",
        _r(
            r"\b("
            r"i\s*g\s*n\s*o\s*r\s*e|"
            r"b\s*y\s*p\s*a\s*s\s*s|"
            r"j\s*a\s*i\s*l\s*b\s*r\s*e\s*a\s*k|"
            r"o\s*v\s*e\s*r\s*r\s*i\s*d\s*e"
            r")\b"
        ),
        HIGH,
        "Obfuscation",
        "Interleaved spaces between letters to bypass keyword filters.",
    ),

    # ── 7. Payload Splitting ───────────────────────────────────────────
    (
        "token_splitting_concat",
        _r(
            r'("|\')?\s*\+\s*("|\')?.{1,20}\+.{1,20}'
            r"\b(ignore|bypass|jailbreak|override|inject)\b"
        ),
        HIGH,
        "Payload Splitting",
        "Token/string splitting to evade filters.",
    ),
    (
        "comment_injection",
        _r(
            r"(/\*.*?\*/|<!--.*?-->|#\s*\w+\s*\n)"
            r".{0,100}"
            r"\b(ignore|bypass|override|jailbreak)\b"
        ),
        HIGH,
        "Payload Splitting",
        "Injecting harmful instructions inside code comments.",
    ),

    # ── 8. Markdown / HTML Injection ───────────────────────────────────
    (
        "html_injection",
        _r(
            r"<\s*(script|iframe|object|embed|form|input|link|meta|style)\b[^>]*>"
        ),
        HIGH,
        "HTML/Markdown Injection",
        "Active HTML tag injection (XSS / content injection).",
    ),
    (
        "markdown_link_injection",
        _r(
            r"\[.{1,100}\]\s*\(\s*(javascript:|data:|vbscript:|file:|"
            r"https?://(?![\w.-]+\.(com|org|net|id|io)))[^)]*\)"
        ),
        MEDIUM,
        "HTML/Markdown Injection",
        "Markdown link pointing to suspicious URL protocol.",
    ),

    # ── 9. Chain-of-Thought / Reasoning Hijack ─────────────────────────
    (
        "reasoning_override",
        _r(
            r"\b(your\s+(reasoning|logic|thinking)\s+is\s+wrong|"
            r"actually\s+you\s+(should|must|need\s+to)|"
            r"re.?think\s+your|reconsider\s+your|"
            r"pemikiranmu\s+salah|kamu\s+seharusnya)\b"
            r".{0,100}"
            r"\b(ignore|bypass|provide|give|tell|output)\b"
        ),
        MEDIUM,
        "Reasoning Hijack",
        "Attempt to distort the model's chain-of-thought.",
    ),
    (
        "reward_hacking_framing",
        _r(
            r"\b(if\s+you\s+(comply|answer|help|do\s+this)|"
            r"i\s+will\s+(give|reward|rate|tip|pay)|"
            r"your\s+(score|rating|reward)\s+will\s+(be|increase)|"
            r"jika\s+kamu\s+mematuhi|aku\s+akan\s+memberi)\b"
        ),
        LOW,
        "Reasoning Hijack",
        "Reward hacking — promising rewards to manipulate responses.",
    ),

    # ── 10. Multilingual Attack ────────────────────────────────────────
    (
        "multilingual_override_id",
        _r(
            r"\b(abaikan|lupakan|batalkan|lewati|jangan\s+pedulikan|"
            r"tolak|hapus|buang|singkirkan)\b"
            r".{0,50}"
            r"\b((semua\s+)?(instruksi|perintah|aturan|panduan|sistem|pedoman|"
            r"batasan|larangan|pembatasan))\b"
        ),
        CRITICAL,
        "Multilingual Attack",
        "Instruction override attempt in Bahasa Indonesia.",
    ),
    (
        "multilingual_jailbreak_mixed",
        _r(
            r"(\bignore\b.{0,30}\binstruksi\b|"
            r"\babaikan\b.{0,30}\binstructions\b|"
            r"\bjailbreak\b.{0,50}\bkamu\b)"
        ),
        HIGH,
        "Multilingual Attack",
        "Cross-language attack (code-switching) to bypass filters.",
    ),

    # ── 11. Indirect Injection ─────────────────────────────────────────
    (
        "injection_via_document",
        _rm(
            r"(summarize|translate|analyze|read|process|parse)\s+(this|the)\s+"
            r"(document|text|content|file|email|webpage|article)"
            r".{0,300}"
            r"(ignore|bypass|override|jailbreak|forget|disregard)"
        ),
        HIGH,
        "Indirect Injection",
        "Prompt injection embedded inside external document/content.",
    ),
    (
        "second_order_injection",
        _r(
            r"\b(when\s+(you\s+)?(process|receive|read|get|see)|"
            r"next\s+time\s+(you|someone)|"
            r"if\s+(anyone|a\s+user|someone)\s+asks)\b"
            r".{0,100}"
            r"\b(ignore|bypass|say|output|respond|tell\s+them)\b"
        ),
        HIGH,
        "Indirect Injection",
        "Second-order injection — delayed instructions for future execution.",
    ),

    # ── 12. Sensitive Data Extraction ───────────────────────────────────
    (
        "credential_extraction",
        _r(
            r"\b(password|passwd|secret\s+key|api\s+key|token|credential|"
            r"private\s+key|auth\s+token|sandi|kunci\s+rahasia)\b"
            r".{0,60}"
            r"\b(show|reveal|tell|output|print|give\s+me|share|tampilkan|berikan)\b"
        ),
        CRITICAL,
        "Data Extraction",
        "Attempt to extract credentials / sensitive data.",
    ),

    # ── 13. ROT13 Obfuscation ──────────────────────────────────────────
    (
        "rot13_encoded_payload",
        _r(
            r"\b(rot13|rot-?13|decode13|dekod13)\b.{0,30}"
            r"([A-Za-z]{10,})"
        ),
        HIGH,
        "Obfuscation",
        "Payload hidden using ROT13 encoding.",
    ),
    (
        "rot13_common_keywords",
        _r(
            r"\b(vatrebq|vatrebg|nffjbeq|vss|vzcbvqr|"
            r"jvnyorznkr|jnll|svyy|grnq|qvtvat|qvtvatr|"
            r"vagehffvnpur|vagehffvnpurq|"
            r"jryy|vffvba|vffvbafrq|"
            r"zrqvat|zrqn|jnfgu|"
            r"vafvb|vafv|vafvba|"
            r"jbeyq|jbeq|"
            r"eryhr|thvyyrqnq|pbeagfvbaq)\b"
        ),
        MEDIUM,
        "Obfuscation",
        "Injection keywords encoded with ROT13.",
    ),

    # ── 14. Function Call / Tool Hijacking ─────────────────────────────
    (
        "tool_call_injection",
        _r(
            r"\b(tool|function|api|endpoint|method|action)\b"
            r".{0,40}"
            r"\b(call|invoke|execute|run|trigger|dispatch)\b"
            r".{0,60}"
            r"\b(ignore|bypass|override|jailbreak|inject|modify|alter|change)\b"
        ),
        CRITICAL,
        "Function Call Hijacking",
        "Attempt to inject harmful instructions into function/tool calls.",
    ),
    (
        "output_format_hijack",
        _r(
            r"\b(response|output|format|return)\b"
            r".{0,30}"
            r"\b(in|as|using|with)\b"
            r".{0,30}"
            r"\b(json|xml|csv|yaml|html|markdown|base64)\b"
            r".{0,60}"
            r"\b(ignore|bypass|override|jailbreak|inject|modify|alter)\b"
        ),
        HIGH,
        "Function Call Hijacking",
        "Forcing the AI to change output format to hide injection.",
    ),

    # ── 15. Additional Tool-Specific Patterns ──────────────────────────
    (
        "output_full_prompt",
        _r(
            r"\b(output|print|show|display|reveal|dump|return)\b"
            r".{0,30}"
            r"\b(your\s+(full|entire|complete|whole)\s+"
            r"(system\s+)?(prompt|instructions?|context))\b"
        ),
        CRITICAL,
        "Prompt Leaking",
        "Attempt to output the full system prompt/instructions.",
    ),
    (
        "abaikan_semua_instruksi",
        _r(
            r"\b(abaikan|lupakan|batalkan|hapus|buang|tolak|"
            r"jangan\s+(pedulikan|ikuti|patuhi|dengarkan))\b"
            r".{0,20}"
            r"\b(semua|segala|seluruh)\b"
            r".{0,20}"
            r"\b(instruksi|perintah|aturan|panduan|pedoman|batasan|larangan)\b"
        ),
        CRITICAL,
        "Multilingual Attack",
        "Indonesian variant of 'ignore all instructions'.",
    ),
    (
        "tampilkan_system_prompt",
        _r(
            r"\b(tampilkan|perlihatkan|bongkar|bocorkan|ungkapkan|keluarkan)\b"
            r".{0,30}"
            r"\b(system\s+prompt|prompt\s+sistem|instruksi\s+tersembunyi|"
            r"instruksi\s+rahasia|prompt\s+asli)\b"
        ),
        CRITICAL,
        "Prompt Leaking",
        "Indonesian variant of 'reveal your system prompt'.",
    ),
    (
        "call_api_endpoint_to_inject",
        _r(
            r"\b(call|hit|request|invoke|trigger)\b"
            r".{0,30}"
            r"\b(the\s+(api|endpoint|url|service|function))\b"
            r".{0,60}"
            r"\b(to|and|with|using)\b"
            r".{0,40}"
            r"\b(ignore|bypass|override|inject|modify|change|hack)\b"
        ),
        HIGH,
        "Function Call Hijacking",
        "Attempt to hijack function calls to inject malicious instructions.",
    ),
    (
        "output_as_json_containing_injection",
        _r(
            r"\b(output|return|respond|format)\b"
            r".{0,30}"
            r"\b(as|in|using|with)\b"
            r".{0,20}"
            r"\b(json|xml|yaml|markdown)\b"
            r".{0,30}"
            r"\b(containing|that\s+(includes|contains|says|states)|with)\b"
            r".{0,60}"
            r"\b(ignore|bypass|override|jailbreak|you\s+are|system\s+prompt)\b"
        ),
        HIGH,
        "Function Call Hijacking",
        "Forcing output format that contains injection payload.",
    ),
]


# ───────────────────────────────────────────────────────────────────────
# Text Extraction — pull all text-like values from tool args
# ───────────────────────────────────────────────────────────────────────

def _extract_text_from_args(tool_name: str, args: dict) -> list[str]:
    """
    Extract all string values from tool arguments for scanning.
    Handles nested dicts and lists up to depth 5.
    Special handling for send_agent_message (message field) and
    bash/runpy (script/code fields).
    """
    texts: list[str] = []
    _visited = set()

    def _extract(obj, depth=0):
        if depth > 5:
            return
        obj_id = id(obj)
        if obj_id in _visited:
            return
        _visited.add(obj_id)

        if isinstance(obj, str):
            if len(obj) > 10:  # skip trivially short strings
                texts.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                _extract(v, depth + 1)
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                _extract(item, depth + 1)

    _extract(args)

    # For tools with well-known string fields, always re-scan those explicitly
    # even if they're short (to catch e.g. short but dangerous payloads)
    known_text_fields = {
        "write_file": ("content", "file_path"),
        "str_replace": ("old_str", "new_str", "file_path"),
        "patch": ("patch", "file_path"),
        "read_file": ("file_path",),
        "bash": ("script",),
        "runpy": ("script", "code"),
        "send_agent_message": ("message",),
    }

    for field in known_text_fields.get(tool_name, ()):
        val = args.get(field)
        if isinstance(val, str) and val not in texts:
            texts.insert(0, val)  # prioritize known fields

    return texts


# ───────────────────────────────────────────────────────────────────────
# Agent Config Lookup
# ───────────────────────────────────────────────────────────────────────

def _get_agent_config(agent_id: str) -> dict:
    """
    Read per-agent injection guard configuration from agent variables.
    Falls back to defaults.
    """
    try:
        from models.db import db
        agent = db.get_agent(agent_id)
        if not agent:
            return {
                "injection_guard_enabled": _DEFAULT_ENABLED,
                "injection_guard_min_severity": _DEFAULT_MIN_SEVERITY,
                "injection_guard_mode": _DEFAULT_MODE,
            }

        # Also check agent variables for overrides
        vars_dict = db.get_agent_variables_dict(agent_id)

        enabled_raw = vars_dict.get("injection_guard_enabled", "1")
        enabled = enabled_raw not in ("0", "false", "False", "no", "off", "")
        if enabled_raw == "1":
            enabled = True

        min_sev = vars_dict.get(
            "injection_guard_min_severity", _DEFAULT_MIN_SEVERITY
        ).upper()
        if min_sev not in _SEVERITY_ORDER:
            min_sev = _DEFAULT_MIN_SEVERITY

        mode = vars_dict.get("injection_guard_mode", _DEFAULT_MODE).lower()
        if mode not in ("block", "warn", "log"):
            mode = _DEFAULT_MODE

        return {
            "injection_guard_enabled": enabled,
            "injection_guard_min_severity": min_sev,
            "injection_guard_mode": mode,
        }
    except Exception:
        return {
            "injection_guard_enabled": _DEFAULT_ENABLED,
            "injection_guard_min_severity": _DEFAULT_MIN_SEVERITY,
            "injection_guard_mode": _DEFAULT_MODE,
        }


def _is_super_agent(agent_id: str) -> bool:
    """Check if the agent is a super agent (bypasses all guards)."""
    # Known super agent IDs — hardcoded fallback
    _KNOWN_SUPER_AGENTS = frozenset({"siwa"})
    if agent_id in _KNOWN_SUPER_AGENTS:
        return True
    try:
        from models.db import db
        agent = db.get_agent(agent_id)
        if agent:
            return bool(agent.get("is_super"))
        # If agent not found by ID, check if this agent_id matches
        # the currently configured super agent
        super_a = db.get_super_agent()
        if super_a and super_a.get("id") == agent_id:
            return True
        return False
    except Exception:
        return False


# ───────────────────────────────────────────────────────────────────────
# Core Detection Logic
# ───────────────────────────────────────────────────────────────────────

def _detect_injection(text: str) -> tuple[bool, str, str, float, str]:
    """
    Scan a single text string against all rules.

    Returns:
        (is_injected, highest_severity, triggered_rule_name, risk_score, reason)
    """
    if not text or not isinstance(text, str):
        return (False, "", "", 0.0, "")

    best_match = None
    best_severity_order = -1
    best_score = 0.0
    matched_count = 0

    for rule_name, pattern, severity, category, description in _RULES:
        for m in pattern.finditer(text):
            matched_count += 1
            sev_order = _SEVERITY_ORDER.get(severity, 0)
            sev_score = _SEVERITY_SCORE.get(severity, 0.0)

            if sev_order > best_severity_order:
                best_severity_order = sev_order
                best_score = sev_score
                best_match = (rule_name, severity, category, description,
                              m.group(0)[:120])
            elif sev_order == best_severity_order and sev_score > best_score:
                best_score = sev_score
                best_match = (rule_name, severity, category, description,
                              m.group(0)[:120])

    if best_match is None:
        return (False, "", "", 0.0, "")

    # Boost risk score by number of distinct rules matched
    multiplier_bonus = min(0.15 * (matched_count - 1), 0.3)
    risk_score = min(best_score + multiplier_bonus, 1.0)

    rule_name, severity, category, description, matched_text = best_match
    reason = (
        f"Rule: {rule_name} | Category: {category} | "
        f"Matched: '{matched_text}' | "
        f"Description: {description}"
    )

    return (True, severity, rule_name, risk_score, reason)


# ───────────────────────────────────────────────────────────────────────
# Main Guard Function — matches register_tool_guard signature
# ───────────────────────────────────────────────────────────────────────

def injection_tool_guard(agent_id: str, tool_name: str, args: dict) -> dict | None:
    """
    Pre-execution tool guard that scans tool arguments for prompt injection.

    Args:
        agent_id:  The agent ID requesting the tool call.
        tool_name: The name of the tool being called.
        args:      The tool arguments dict.

    Returns:
        dict  — {block: True, error: "..."} if injection detected.
        None  — if clean or guard is disabled for this agent.
    """
    # Only guard specific tools
    if tool_name not in _GUARDED_TOOLS:
        return None

    # Super agents bypass all guards
    if _is_super_agent(agent_id):
        return None

    # Check per-agent config
    config = _get_agent_config(agent_id)
    if not config.get("injection_guard_enabled", _DEFAULT_ENABLED):
        return None

    min_severity = config.get(
        "injection_guard_min_severity", _DEFAULT_MIN_SEVERITY
    )
    mode = config.get("injection_guard_mode", _DEFAULT_MODE)
    min_sev_order = _SEVERITY_ORDER.get(min_severity, 1)

    # Extract all text from tool arguments
    texts = _extract_text_from_args(tool_name, args)
    if not texts:
        return None

    # Scan each text chunk
    for text in texts:
        is_injected, severity, rule_name, risk_score, reason = _detect_injection(text)
        if not is_injected:
            continue

        sev_order = _SEVERITY_ORDER.get(severity, 0)
        if sev_order < min_sev_order:
            continue  # below minimum severity threshold

        score_pct = int(risk_score * 100)
        error_msg = (
            f"Prompt injection detected in tool arguments "
            f"(severity: {severity}, score: {score_pct}%). "
            f"{reason}"
        )

        if mode == "log":
            _logger.warning(
                "INJECTION_LOG agent=%s tool=%s severity=%s score=%d rule=%s",
                agent_id, tool_name, severity, score_pct, rule_name,
            )
            return None  # log only, don't block

        if mode == "warn":
            _logger.warning(
                "INJECTION_WARN agent=%s tool=%s severity=%s score=%d rule=%s",
                agent_id, tool_name, severity, score_pct, rule_name,
            )
            # Warn mode: log and block with a softer message
            return {
                "block": True,
                "error": (
                    f"[WARN] {error_msg}\n"
                    f"This tool call has been blocked by the injection guard "
                    f"(mode: warn). Contact your administrator to adjust the "
                    f"guard policy if this is a false positive."
                ),
            }

        # Default: block mode
        _logger.warning(
            "INJECTION_BLOCK agent=%s tool=%s severity=%s score=%d rule=%s",
            agent_id, tool_name, severity, score_pct, rule_name,
        )
        return {"block": True, "error": error_msg}

    return None
