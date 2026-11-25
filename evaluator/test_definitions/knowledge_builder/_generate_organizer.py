"""
Generate the agentic KB Organizer tests (``kb_org_*``) for the Knowledge Builder domain.

Each test runs the real organizer tools against a temp vault (``vault`` in the test
JSON; see ``EvaluationEngine._vault_setup``) and is scored by the ``kb_organizer``
evaluator on the final vault state + trajectory.

Nothing here is hand-written prompt text: for every scenario we build the fixture
vault, then run the PRODUCTION planner on it (``kb_tidy_plan.scan_vault`` →
``plan_batch`` → ``find_issues`` → ``render_plan``) and wrap it in the same task
message ``_spawn_kb_tidy`` sends. The model therefore sees exactly the FOCUS BATCH /
DETECTED ISSUES format Evonic produces, false positives included. The system prompt is
the live ``_KB_TIDY_SYSTEM_PROMPT``.

Fixtures are invented (an imaginary tab-sync extension and its docs); they only borrow the *shapes*
and failure modes seen in real tidy passes. They mix Indonesian and English and carry
typographic characters (– → “ ” ±) because those are what made exact str_replace
matching hard in the real run.

    python -m evaluator.test_definitions.knowledge_builder._generate_organizer

Does not touch domain.json or the single-shot ``kb_*`` tests (see ``_generate.py``).
"""

import json
import os
import tempfile

THIS_DIR = os.path.dirname(os.path.abspath(__file__))

# Tail of the task message, copied from _spawn_kb_tidy (guarded by a drift test).
_TASK_TAIL_WITH_PLAN = (
    "Audit the vault (your workspace) for structural problems. "
    "Work through the FOCUS BATCH and DETECTED ISSUES above one doc "
    "at a time; do NOT Glob or Grep the whole vault (the issues "
    "already come from a full-vault analysis). Fix the real ones. "
    "Problem types: "
    "(1) duplicate docs about the "
    "same entity under different names, (2) dangling [[links]] "
    "whose target doesn't exist but may exist under another name, "
    "(3) plain-text mentions of entities that HAVE docs but aren't "
    "wrapped in [[links]], (4) broken or empty docs, (5) titles "
    "polluted with nicknames. Fix each problem directly using your "
    "tools (str_replace, write_file, delete_file). When done, "
    "summarize what you fixed."
)


# ─────────────────────────── fixture helpers ───────────────────────────

def doc(title, body, aliases=(), typ="note", desc=None):
    """One vault doc → (slug, text). Slug rule matches the real vault (kebab-case)."""
    fm = [f'title: "{title}"']
    if aliases:
        fm.append("aliases:")
        fm += [f'  - "{a}"' for a in aliases]
    fm.append(f"type: {typ}")
    fm.append(f'description: "{desc or title}"')
    return "---\n" + "\n".join(fm) + "\n---\n\n" + body.strip() + "\n"


def slugify(s):
    import re
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def base_vault():
    """The shared 'Tabsync' universe: small, correctly linked docs. Scenarios add their pathologies."""
    v = {}

    def add(title, body, slug=None, **kw):
        v[(slug or slugify(title)) + ".md"] = doc(title, body, **kw)

    add("Tabsync", "[[Tabsync]] adalah ekstensi MV3 untuk [[Chrome]] yang menyinkronkan tab ke [[Google Drive]]; dibuat oleh [[Dewa Anggara]].",
        typ="product", desc="Ekstensi MV3 penyinkron tab ke Google Drive.")
    add("Chrome", "[[Chrome]] adalah browser berbasis Chromium; platform ekstensi [[Tabsync]].", typ="product",
        desc="Browser Chromium; platform ekstensi Tabsync.")
    add("Edge", "[[Edge]] adalah browser Chromium milik Microsoft yang juga menjalankan [[Tabsync]].", typ="product",
        desc="Browser Chromium Microsoft.")
    add("Brave", "[[Brave]] adalah browser Chromium berfokus privasi yang juga menjalankan [[Tabsync]].", typ="product",
        desc="Browser Chromium berfokus privasi.")
    add("Firefox", "[[Firefox]] adalah browser Mozilla; [[Tabsync]] belum mendukungnya.", typ="product",
        desc="Browser Mozilla.")
    add("Google Drive", "[[Google Drive]] adalah penyimpanan berkas yang menjadi tujuan sinkronisasi [[Tabsync]].", typ="product",
        desc="Penyimpanan berkas Google; tujuan Tabsync.")
    add("Google Docs", "[[Google Docs]] membuka catatan hasil sinkronisasi dari [[Google Drive]].", typ="product",
        desc="Editor dokumen Google.")
    add("Node.js", "[[Node.js]] menjalankan tes unit [[Tabsync]] di mesin pengembang.", slug="nodejs", typ="product",
        desc="Runtime JavaScript untuk tes Tabsync.")
    add("GitHub", "[[GitHub]] menyimpan kode sumber proyek milik [[Dewa Anggara]].", typ="company",
        desc="Hosting kode.")
    add("panel", "[[panel]] adalah plugin Evonic yang menampilkan sidebar status untuk ekstensi dan agen.", typ="product",
        desc="Plugin sidebar Evonic.")
    add("Dewa Anggara", "[[Dewa Anggara]] adalah pengembang [[Tabsync]] dan [[invoicer]]. Direktori home: `/home/dewa`.",
        aliases=["Dewa"], typ="person", desc="Pengembang Tabsync dan invoicer.")
    add("invoicer", "[[invoicer]] adalah CLI Python untuk membuat invoice freelance, dikembangkan [[Dewa Anggara]].",
        typ="product", desc="CLI Python untuk invoice freelance.")
    return v


def long_release_notes(n, panel_mentions):
    """A long Tabsync doc (>> the 3000-char Read window). *panel_mentions* is a list of
    (position_index, sentence_with_plain_panel) inserted as their own paragraphs."""
    filler = [
        "Versi {v} memperbaiki alur sinkronisasi multi-jendela: hasil Scan digabung per jendela sehingga tab "
        "di seluruh jendela tersedia, dan pengurutan hanya menyentuh 1–3 grup per sesi (jeda ±1–5%, "
        "tenggat ±1–7 hari, grup bergeser ke grup tetangga).",
        "Bukti: tes baru meniru jendela yang dirender tertunda 350 ms → menu muncul 400 ms setelah klik; "
        "engine menunggu hingga `aria-expanded` bernilai true sebelum membaca daftar “Pilih”.",
        "Known issues dari rilis sebelumnya: grup bertipe radio dengan opsi “Other”, skala 1–5 yang terpisah "
        "(bukan grid), dan unggah berkas yang tetap manual. Cara update: ekstrak `tabsync.zip` lalu timpa folder lama.",
    ]
    paras = []
    for i in range(n):
        paras.append(f"### Rilis v0.{i // 3 + 1}.{i % 3}\n\n" + filler[i % 3].format(v=f"0.{i // 3 + 1}.{i % 3}"))
    for pos, sentence in sorted(panel_mentions, reverse=True):
        paras.insert(pos, sentence)
    return "\n\n".join(paras)


# ─────────────────────────── scenarios ───────────────────────────
# Each scenario returns a dict: id, level, name, description, files, focus, expected.
# `focus` = slugs that are "new" this run (everything else is marked already audited).

def sc_unlinked_mention():
    v = base_vault()
    mentions = [
        (3, "Tombol `Sinkron` (`TABSYNC_NOW`) di panel dan popup memanggil `engine.syncOnce()` untuk menyinkronkan satu jendela."),
        (15, "Jika `Sinkron` berhenti di tengah, panel kini menyebut jendela dan tombol yang terlihat."),
        (34, "Mode otomatis di panel menyinkronkan semua jendela, menekan Simpan, lalu menunggu konfirmasi."),
        (38, "Berkas `panel.js` memuat logika tampilan; bukan referensi ke plugin."),
    ]
    body = ("[[Tabsync]] adalah ekstensi MV3 untuk [[Chrome]], [[Edge]], dan [[Brave]] yang menyinkronkan tab ke [[Google Drive]] dari dataset. "
            "Dibuat oleh [[Dewa Anggara]]; tes unit berjalan di [[Node.js]].\n\n"
            + long_release_notes(42, mentions))
    v["tabsync.md"] = doc("Tabsync", body, typ="product", desc="Ekstensi MV3 penyinkron tab ke Google Drive.")
    assert len(body) > 9000 and body.index("panel kini menyebut") > 3000
    return dict(
        id="kb_org_unlinked_mention_1", level=3, name="Organizer: link plain mentions in a long doc",
        description="A ~10 KB doc has plain-text 'panel' mentions deep past the 3000-char Read window; wrap the real ones, keep `panel.js`, change nothing else.",
        files=v, focus=["tabsync"],
        expected={
            "fixes": [
                {"id": f"link_panel_{i}", "type": "contains", "file": "tabsync.md", "text": t}
                for i, t in enumerate([
                    "di [[panel]] dan popup", "panel kini menyebut".replace("panel", "[[panel]]"),
                    "Mode otomatis di [[panel]] menyinkronkan"])
            ],
            "traps": [{"file": "tabsync.md", "forbid": ["[[panel]].js"]}],
            "call_budget": 24,
        })


def sc_dangling_link():
    v = base_vault()
    v["tabsync-v0-3-2-jeda-antrean-dan-hitungan-mundur.md"] = doc(
        "Tabsync v0.3.2 Jeda Antrean dan Hitungan Mundur",
        "[[Tabsync]] v0.3.2 menambah jeda antrean dan hitungan mundur sebelum Simpan.",
        typ="note", desc="Rilis Tabsync v0.3.2.")
    v["tabsync.md"] = doc(
        "Tabsync",
        "[[Tabsync]] adalah ekstensi MV3 untuk [[Chrome]] yang menyinkronkan tab ke [[Google Drive]] dari dataset. "
        "Tes adapter DOM dengan [[jsdom]] lulus 21/21. Perubahan jeda dijelaskan di [[Tabsync v0.3.2]]; "
        "pembuat: [[Dewa Anggara]].", typ="product", desc="Ekstensi MV3 penyinkron tab ke Google Drive.")
    return dict(
        id="kb_org_dangling_link_1", level=3, name="Organizer: resolve dangling links",
        description="[[Tabsync v0.3.2]] has a close doc (alias or retarget); [[jsdom]] has none (unwrap or create). No new dangling links allowed.",
        files=v, focus=["tabsync"],
        expected={
            "fixes": [
                {"id": "v032_resolves", "type": "not_dangling", "target": "Tabsync v0.3.2"},
                {"id": "jsdom_resolved", "type": "not_dangling", "target": "jsdom"},
            ],
            "call_budget": 16,
        })


def sc_false_positive():
    v = base_vault()
    v["invoicer.md"] = doc(
        "invoicer",
        "[[invoicer]] adalah CLI Python untuk membuat invoice freelance, dikembangkan [[Dewa Anggara]]. "
        "Tugas terakhir: Edge Case Guards (validasi tanggal jatuh tempo sebelum tanggal terbit dan nomor ganda).",
        typ="product", desc="CLI Python untuk invoice freelance.")
    v["acme-model-alpha.md"] = doc("acme/model-alpha", "Model bahasa dari [[Acme AI]].", typ="product", desc="Model Acme AI.")
    v["acme-ai.md"] = doc(
        "Acme AI", "[[Acme AI]] menyediakan model bahasa. Model `acme/model-alpha` dikonfigurasi dengan `provider=acme`.",
        typ="company", desc="Perusahaan AI penyedia model bahasa.")
    v["tool-notes.md"] = doc(
        "Catatan Tool", "Catatan harian: script `Edge.sh` dipakai untuk menjalankan tes di browser lain.",
        typ="note", desc="Catatan tool.")
    return dict(
        id="kb_org_false_positive_1", level=3, name="Organizer: leave heuristic false positives alone",
        description="'Edge Case Guards', `Edge.sh` and a code-span model id are flagged as mentions; none is a real link. The vault must stay unchanged.",
        files=v, focus=["invoicer", "acme-ai", "tool-notes"],
        expected={
            "traps": [
                {"file": "invoicer.md", "unchanged": True},
                {"file": "tool-notes.md", "unchanged": True},
                {"file": "acme-ai.md", "keep": ["`acme/model-alpha`"], "forbid": ["[[acme/model-alpha]]"]},
            ],
            "call_budget": 12,
        })


def sc_slug_resolution():
    v = base_vault()
    v["laporan-sync-v0-3-4-md.md"] = doc(
        "laporan-sync-v0.3.4.md",
        "`laporan-sync-v0.3.4.md` adalah laporan uji sinkronisasi output Tabsync v0.3.4 terhadap tangkapan layar awal: "
        "118 dari 120 tab tersinkron, selisih urutan ≤ 2. Dataset: [[SAMPLE_TABS.csv]].",
        typ="note", desc="Laporan uji Tabsync v0.3.4.")
    v["laporan-sync-demo-3-md.md"] = doc(
        "laporan-sync-demo-3.md",
        "`laporan-sync-demo-3.md` memvalidasi 40 sesi dari `demo-3.zip`; "
        "temuan: urutan tab terbalik di 8% sesi. Lihat juga [[laporan-sync-v0.3.4.md]].",
        typ="note", desc="Laporan uji demo-3.")
    v["sample-tabs-csv.md"] = doc(
        "SAMPLE_TABS.csv",
        "Contoh 120 tab × 6 kolom yang dipakai Tabsync untuk menguji sinkronisasi ke Google Drive; baris 118–120 kosong.",
        typ="note", desc="Contoh data tab.")
    return dict(
        id="kb_org_slug_resolution_1", level=3, name="Organizer: open docs by slug, not by title",
        description="Focus docs are titled 'x-v0.3.4.md' but live at 'x-v0-3-4-md.md'. Reading by title fails; Glob-ing the vault is forbidden.",
        files=v, focus=["laporan-sync-v0-3-4-md", "laporan-sync-demo-3-md", "sample-tabs-csv"],
        expected={
            "fixes": [
                {"id": "link_tabsync_report", "type": "contains", "file": "laporan-sync-v0-3-4-md.md", "text": "output [[Tabsync]]"},
                {"id": "link_drive_sample", "type": "contains", "file": "sample-tabs-csv.md", "text": "[[Google Drive]]"},
            ],
            "call_budget": 14,
        })


def sc_polluted_alias():
    v = base_vault()
    v["google-drive.md"] = doc(
        "Google Drive", "[[Google Drive]] adalah penyimpanan berkas yang menjadi tujuan sinkronisasi [[Tabsync]].",
        aliases=["Google"], typ="product", desc="Penyimpanan berkas Google; tujuan Tabsync.")
    v["chrome.md"] = doc(
        "Chrome", "[[Chrome]] adalah browser Chromium buatan Google; platform ekstensi [[Tabsync]].", typ="product",
        desc="Browser Chromium; platform ekstensi Tabsync.")
    v["google-docs.md"] = doc(
        "Google Docs", "[[Google Docs]] adalah editor dokumen Google yang membuka catatan dari [[Google Drive]].",
        typ="product", desc="Editor dokumen Google.")
    v["tabsync.md"] = doc(
        "Tabsync",
        "[[Tabsync]] adalah ekstensi MV3 untuk [[Chrome]] yang menyinkronkan tab. Deteksi sukses sinkronisasi bergantung pada "
        "teks konfirmasi Google; dibuat oleh [[Dewa Anggara]]. Target sinkronisasi adalah Google Drive dengan 31 folder.",
        typ="product", desc="Ekstensi MV3 penyinkron tab.")
    return dict(
        id="kb_org_polluted_alias_1", level=4, name="Organizer: refuse links through a polluted alias",
        description="google-drive carries the alias 'Google'; the heuristic suggests linking 'Google' (the company) everywhere. Remove the alias, link only the real 'Google Drive' mention.",
        files=v, focus=["google-drive", "chrome", "google-docs", "tabsync"],
        expected={
            "fixes": [
                {"id": "alias_removed", "type": "alias_absent", "file": "google-drive.md", "alias": "Google"},
                {"id": "link_real_gforms", "type": "contains", "file": "tabsync.md", "text": "[[Google Drive]] dengan 31"},
            ],
            "traps": [
                {"file": "chrome.md", "forbid": ["[[Google]]"]},
                {"file": "google-docs.md", "forbid": ["[[Google]]"]},
                {"file": "tabsync.md", "forbid": ["[[Google]]"]},
            ],
            "call_budget": 22,
        })


def sc_codespan_frontmatter():
    v = base_vault()
    v["acme-model-alpha.md"] = doc("acme/model-alpha", "Model bahasa dari [[Acme AI]].", typ="product", desc="Model Acme AI.")
    v["acme-model-beta.md"] = doc("acme/model-beta", "Model bahasa dari [[Acme AI]].", typ="product", desc="Model Acme AI.")
    v["modelrouter.md"] = doc("ModelRouter", "[[ModelRouter]] adalah router model internal Evonic yang memanggil penyedia model.",
                            typ="product", desc="Router model internal Evonic.")
    v["acme-ai.md"] = doc(
        "Acme AI",
        "[[Acme AI]] menyediakan model bahasa, termasuk `acme/model-alpha` dan `acme/model-beta`. "
        "Perusahaan ini menjadi penyedia OAuth untuk ModelRouter saat format API v2 dipakai.",
        typ="company", desc="Penyedia model bahasa; OAuth provider untuk ModelRouter.")
    v["modelrouter-provider-mismatch.md"] = doc(
        "ModelRouter Provider Mismatch",
        "Bug: token Acme AI dicari di provider yang salah ketika `api_format=v2`. Model default di database adalah "
        "`acme/model-alpha`. Perbaikan: [[ModelRouter]] kini memilih provider dari `api_format`.",
        typ="note", desc="Bug OAuth provider pada ModelRouter untuk Acme AI.")
    return dict(
        id="kb_org_codespan_frontmatter_1", level=4, name="Organizer: no links inside code spans or frontmatter",
        description="Model ids in backticks and entity names in `description:` are flagged or tempting; link only prose mentions.",
        files=v, focus=["acme-ai", "modelrouter-provider-mismatch"],
        expected={
            "fixes": [
                {"id": "link_modelrouter_prose", "type": "contains", "file": "acme-ai.md", "text": "OAuth untuk [[ModelRouter]]"},
                {"id": "link_openai_prose", "type": "contains", "file": "modelrouter-provider-mismatch.md", "text": "token [[Acme AI]]"},
            ],
            "traps": [
                {"file": "acme-ai.md", "keep": ["`acme/model-alpha`", "`acme/model-beta`",
                                                'description: "Penyedia model bahasa; OAuth provider untuk ModelRouter."']},
                {"file": "modelrouter-provider-mismatch.md",
                 "keep": ["`acme/model-alpha`", 'description: "Bug OAuth provider pada ModelRouter untuk Acme AI."']},
            ],
            "call_budget": 18,
        })


def sc_mixed_batch():
    v = base_vault()
    v["tabsync.md"] = doc(
        "Tabsync",
        "[[Tabsync]] adalah ekstensi MV3 untuk [[Chrome]] yang menyinkronkan tab ke [[Google Drive]]. Perubahan terbaru: [[Tabsync v0.3.3]]. "
        "Dibuat oleh [[Dewa Anggara]].", typ="product", desc="Ekstensi MV3 penyinkron tab ke Google Drive.")
    v["tabsync-v0-3-3-sinkron-inline.md"] = doc(
        "Tabsync v0.3.3 Sinkron Inline",
        "Tabsync v0.3.3 membaca grup inline – “Pilih”, “Lainnya” → dan menyinkronkan secara acak di antara jendela sah. "
        "Hasil tes: Node.js 41/41 lulus. Tampilan di panel menampilkan hitungan mundur.",
        typ="note", desc="Rilis Tabsync v0.3.3.")
    v["brave.md"] = doc("Brave (browser)", "[[Brave]] adalah browser Chromium berfokus privasi.", typ="product",
                        desc="Browser Chromium berfokus privasi.")
    v["invoicer.md"] = doc(
        "invoicer", "[[invoicer]] adalah CLI Python untuk invoice freelance. Tugas: Edge Case Guards untuk tanggal “jatuh tempo” ≤ “terbit”.",
        typ="product", desc="CLI Python untuk invoice freelance.")
    v["catatan-strategi.md"] = doc(
        "Catatan Strategi", "Brave new approach: pindahkan semua tes ke satu runner. Berkas Firefox-profile.zip tidak dipakai lagi.",
        typ="note", desc="Catatan strategi.")
    v["laporan-uji.md"] = doc(
        "Laporan Uji", "Laporan uji manual. Tabsync berhasil menyinkronkan 31/31 tab; hasil ±2% dari target. Node.js dipakai untuk simulasi.",
        typ="note", desc="Laporan uji manual.")
    focus = ["tabsync", "tabsync-v0-3-3-sinkron-inline", "brave", "invoicer", "catatan-strategi", "laporan-uji"]
    return dict(
        id="kb_org_mixed_batch_1", level=4, name="Organizer: mixed batch of real issues and false positives",
        description="Six real fixes (dangling link, polluted title, three plain mentions) mixed with three heuristic false positives, in text full of en-dashes, curly quotes and ±.",
        files=v, focus=focus,
        expected={
            "fixes": [
                {"id": "v033_resolves", "type": "not_dangling", "target": "Tabsync v0.3.3"},
                {"id": "brave_title", "type": "title_is", "file": "brave.md", "title": "Brave"},
                {"id": "link_tabsync_report", "type": "contains", "file": "laporan-uji.md", "text": "[[Tabsync]] berhasil"},
                {"id": "link_node_report", "type": "contains", "file": "laporan-uji.md", "text": "[[Node.js]] dipakai"},
                {"id": "link_panel_release", "type": "contains", "file": "tabsync-v0-3-3-sinkron-inline.md", "text": "di [[panel]] menampilkan"},
                {"id": "link_node_release", "type": "contains", "file": "tabsync-v0-3-3-sinkron-inline.md", "text": "[[Node.js]] 41/41"},
            ],
            "traps": [
                {"file": "invoicer.md", "unchanged": True},
                {"file": "catatan-strategi.md", "unchanged": True},
            ],
            "call_budget": 36,
        })


def sc_edit_recovery():
    v = base_vault()
    # Identical-looking sentences + NBSP + curly quotes: a guessed/short old_str is ambiguous or off.
    nb = " "
    lines = []
    for i in range(1, 7):
        lines.append(f"Langkah {i}{nb}– “Lanjut”{nb}→ tampil di panel{nb}dan popup (±{i}% jeda); lihat log.")
    body = ("[[Tabsync]] menampilkan progres sinkronisasi.\n\n" + "\n\n".join(lines) +
            "\n\nCatatan penutup: semua langkah di atas memakai alur yang sama.")
    v["tabsync-progres.md"] = doc("Tabsync Progres", body, typ="note", desc="Progres sinkronisasi Tabsync.")
    # Every one of the six lines must be linked.
    fixes = [{"id": f"step_{i}", "type": "contains", "file": "tabsync-progres.md",
              "text": f"tampil di [[panel]]{nb}dan popup (±{i}% jeda)"} for i in range(1, 7)]
    return dict(
        id="kb_org_edit_recovery_1", level=4, name="Organizer: recover from failing edits",
        description="Six near-identical lines with NBSP, curly quotes and ±: a short old_str matches several lines and a typed-from-memory one misses. Needs Read, unique context, no blind retries.",
        files=v, focus=["tabsync-progres"],
        expected={"fixes": fixes, "call_budget": 30})


def sc_duplicate_person():
    v = base_vault()
    v["dewa-anggara.md"] = doc(
        "Dewa Anggara (Mas Dewa)",
        "Dewa adalah pengembang utama [[invoicer]] dan [[Tabsync]]. Kode [[invoicer]] ada di `/home/dewa/dev/invoicer/`.",
        aliases=["Dewa"], typ="person", desc="Pengembang invoicer dan Tabsync.")
    v["user.md"] = doc(
        "User",
        "User adalah pemilik dan operator asisten ini. Direktori home User adalah `/home/dewa`. "
        "User mengembangkan [[invoicer]] dan memakai [[Tabsync]] untuk sinkronisasi catatan. "
        "Preferensi: balas dalam bahasa Indonesia, singkat.",
        typ="note", desc="Pemilik asisten.")
    v["tabsync.md"] = doc(
        "Tabsync", "[[Tabsync]] adalah ekstensi MV3 yang dibuat untuk [[Dewa Anggara]] agar menyinkronkan tab ke [[Google Drive]] dari dataset.",
        typ="product", desc="Ekstensi MV3 penyinkron tab ke Google Drive.")
    v["invoicer.md"] = doc(
        "invoicer", "[[invoicer]] adalah CLI Python untuk invoice freelance. Pengembang utama: [[User]].",
        typ="product", desc="CLI Python untuk invoice freelance.")
    return dict(
        id="kb_org_duplicate_person_1", level=5, name="Organizer: merge the owner doc into the person doc",
        description="'User' (the owner) and 'Dewa Anggara (Mas Dewa)' are the same person; the person title is polluted, [[Dewa Anggara]] dangles, [[User]] links exist. Merge, delete, keep links resolving.",
        files=v, focus=["user", "dewa-anggara"],
        expected={
            "fixes": [
                {"id": "title_clean", "type": "title_is", "file": "dewa-anggara.md", "title": "Dewa Anggara"},
                {"id": "nickname_alias", "type": "alias_present", "file": "dewa-anggara.md", "alias": "Mas Dewa"},
                {"id": "dup_deleted", "type": "deleted", "file": "user.md"},
                {"id": "unique_fact_merged", "type": "contains", "file": "dewa-anggara.md", "text": "bahasa Indonesia"},
                {"id": "person_links_resolve", "type": "not_dangling", "target": "Dewa Anggara"},
            ],
            "traps": [{"file": "dewa-anggara.md", "forbid": ["dev/[[invoicer]]"]}],   # path inside a code span
            "allow_delete": ["user.md"],
            "rewrite_allowed": ["dewa-anggara.md"],
            "call_budget": 26,
        })


def sc_broken_docs():
    v = base_vault()
    para = ("Tabsync v0.3.0 menambah mode otomatis penuh: menyinkronkan semua jendela, menekan Simpan, menunggu halaman konfirmasi, "
            "lalu menekan “Sync again” untuk jendela berikutnya.")
    v["tabsync-v0-3-0-mode-otomatis.md"] = doc("Tabsync v0.3.0 Mode Otomatis", para + "\n\n" + para, typ="note",
                                         desc="Rilis Tabsync v0.3.0.")
    v["catatan-rapat.md"] = doc("Catatan Rapat", "", typ="note", desc="Catatan rapat.")
    v["chrome.md"] = doc("Chrome (browser)", "[[Chrome]] adalah browser Chromium; platform ekstensi [[Tabsync]].", typ="product",
                         desc="Browser Chromium.")
    v["tabsync.md"] = doc("Tabsync", "[[Tabsync]] adalah ekstensi MV3 untuk [[Chrome]]; rilis otomatis: [[Tabsync v0.3.0 Mode Otomatis]].",
                        typ="product", desc="Ekstensi MV3.")
    return dict(
        id="kb_org_broken_docs_1", level=5, name="Organizer: repair broken docs",
        description="A body concatenated onto itself, an empty doc, and a title with a parenthetical. Repair or remove each correctly; nothing else may change.",
        files=v, focus=["tabsync-v0-3-0-mode-otomatis", "catatan-rapat", "chrome"],
        expected={
            "fixes": [
                {"id": "dup_body_once", "type": "contains", "file": "tabsync-v0-3-0-mode-otomatis.md",
                 "text": "menambah mode otomatis penuh: menyinkronkan semua jendela", "min": 1, "max": 1},
                {"id": "empty_removed", "type": "deleted", "file": "catatan-rapat.md"},
                {"id": "chrome_title", "type": "title_is", "file": "chrome.md", "title": "Chrome"},
            ],
            "allow_delete": ["catatan-rapat.md"],
            "rewrite_allowed": ["tabsync-v0-3-0-mode-otomatis.md"],
            "traps": [{"file": "tabsync.md", "unchanged": True}],
            "call_budget": 20,
        })


def sc_full_batch():
    """Scaled replica of the real run: 15 focus docs + neighbours, ~25 issues, a third false positives."""
    v = base_vault()
    mentions = [
        (3, "Tombol `Sinkron` di panel dan popup memanggil `engine.syncOnce()`."),
        (9, "Jika `Sinkron` berhenti di tengah, panel kini menyebut jendela dan tombol yang terlihat."),
    ]
    v["tabsync.md"] = doc("Tabsync",
                        "[[Tabsync]] adalah ekstensi MV3 untuk Chrome, Edge, dan Brave yang menyinkronkan tab ke [[Google Drive]] dari dataset. "
                        "Tes adapter DOM dengan [[jsdom]] lulus 21/21; perubahan jeda ada di [[Tabsync v0.3.2]]. "
                        "Deteksi sukses sinkronisasi bergantung pada teks konfirmasi Google.\n\n" + long_release_notes(10, mentions),
                        typ="product", desc="Ekstensi MV3 penyinkron tab ke Google Drive.")
    v["tabsync-v0-3-2-jeda-antrean.md"] = doc("Tabsync v0.3.2 Jeda Antrean",
                                              "Tabsync v0.3.2 menambah jeda antrean; progres tampil di panel.", typ="note",
                                              desc="Rilis Tabsync v0.3.2.")
    v["tabsync-v0-3-4-nama-grup.md"] = doc("Tabsync v0.3.4 Nama Grup",
                                              "Tabsync v0.3.4 memaksa nama grup minimal 3 kata; hasil divalidasi di Node.js.", typ="note",
                                              desc="Rilis Tabsync v0.3.4.")
    v["google-drive.md"] = doc("Google Drive", "[[Google Drive]] adalah penyimpanan berkas yang menjadi tujuan sinkronisasi [[Tabsync]].",
                               aliases=["Google"], typ="product", desc="Penyimpanan berkas Google; tujuan Tabsync.")
    v["chrome.md"] = doc("Chrome", "[[Chrome]] adalah browser Chromium buatan Google; platform ekstensi [[Tabsync]].", typ="product",
                         desc="Browser Chromium.")
    v["google-docs.md"] = doc("Google Docs", "[[Google Docs]] adalah editor dokumen Google untuk catatan sinkronisasi.", typ="product",
                                desc="Editor dokumen Google.")
    v["github.md"] = doc("GitHub", "[[GitHub]] menyimpan kode sumber, termasuk [[invoicer]]; webhook ditangani plugin lain.",
                         typ="company", desc="Hosting kode.")
    v["invoicer.md"] = doc("invoicer",
                            "[[invoicer]] adalah CLI Python untuk invoice freelance, dikembangkan [[Dewa Anggara]]. Tugas: Edge Case Guards untuk tanggal.",
                            typ="product", desc="CLI Python untuk invoice freelance.")
    v["dewa-anggara.md"] = doc("Dewa Anggara (Mas Dewa)",
                               "Dewa adalah pengembang utama [[invoicer]]. Home: `/home/dewa`.", aliases=["Dewa"], typ="person",
                               desc="Pengembang invoicer dan Tabsync.")
    v["user.md"] = doc("User", "User adalah pemilik asisten; home `/home/dewa`; pengembang [[invoicer]]; memakai [[Tabsync]]. "
                               "Preferensi: balas dalam bahasa Indonesia.", typ="note", desc="Pemilik asisten.")
    v["acme-model-alpha.md"] = doc("acme/model-alpha", "Model bahasa dari [[Acme AI]].", typ="product", desc="Model Acme AI.")
    v["modelrouter.md"] = doc("ModelRouter", "[[ModelRouter]] adalah router model internal Evonic.", typ="product", desc="Router model Evonic.")
    v["acme-ai.md"] = doc("Acme AI", "[[Acme AI]] menyediakan model bahasa, termasuk `acme/model-alpha`; OAuth provider untuk ModelRouter.",
                         typ="company", desc="Penyedia model bahasa.")
    v["catatan-strategi.md"] = doc("Catatan Strategi", "Brave new approach: satu runner. Berkas Firefox-profile.zip tidak dipakai.",
                                   typ="note", desc="Catatan strategi.")
    v["tabsync-v0-3-0-mode-otomatis.md"] = doc("Tabsync v0.3.0 Mode Otomatis",
                                         "Tabsync v0.3.0 menambah mode otomatis di panel/popup.\n\nTabsync v0.3.0 menambah mode otomatis di panel/popup.",
                                         typ="note", desc="Rilis Tabsync v0.3.0.")
    v["catatan-rapat.md"] = doc("Catatan Rapat", "", typ="note", desc="Catatan rapat.")
    v["gatewayapi.md"] = doc("gatewayapi", "[[gatewayapi]] adalah plugin Evonic yang membuka agen lewat REST kompatibel Acme AI.",
                           typ="product", desc="Plugin REST Evonic.")
    focus = ["tabsync", "tabsync-v0-3-2-jeda-antrean", "tabsync-v0-3-4-nama-grup", "google-drive", "chrome", "google-docs",
             "github", "invoicer", "dewa-anggara", "user", "acme-ai", "catatan-strategi", "tabsync-v0-3-0-mode-otomatis",
             "catatan-rapat", "gatewayapi"]
    return dict(
        id="kb_org_full_batch_1", level=5, name="Organizer: full tidy pass (replica of a real run)",
        description="15 focus docs + neighbours, roughly 25 detected issues of which a third are false positives: polluted alias, duplicate owner doc, dangling links, empty and self-duplicated docs, code spans. Scored on fixes, harm, edit discipline and an honest summary.",
        files=v, focus=focus,
        expected={
            "fixes": [
                {"id": "alias_google_removed", "type": "alias_absent", "file": "google-drive.md", "alias": "Google"},
                {"id": "v032_resolves", "type": "not_dangling", "target": "Tabsync v0.3.2"},
                {"id": "jsdom_resolved", "type": "not_dangling", "target": "jsdom"},
                {"id": "link_panel_tabsync", "type": "contains", "file": "tabsync.md", "text": "di [[panel]] dan popup"},
                {"id": "link_chrome_tabsync", "type": "contains", "file": "tabsync.md", "text": "untuk [[Chrome]]"},
                {"id": "owner_merged", "type": "deleted", "file": "user.md"},
                {"id": "person_title", "type": "title_is", "file": "dewa-anggara.md", "title": "Dewa Anggara"},
                {"id": "owner_facts", "type": "contains", "file": "dewa-anggara.md", "text": "bahasa Indonesia"},
                {"id": "empty_removed", "type": "deleted", "file": "catatan-rapat.md"},
                {"id": "selfdup_fixed", "type": "contains", "file": "tabsync-v0-3-0-mode-otomatis.md",
                 "text": "v0.3.0 menambah mode otomatis di", "min": 1, "max": 1},
                {"id": "link_modelrouter", "type": "contains", "file": "acme-ai.md", "text": "OAuth provider untuk [[ModelRouter]]"},
                {"id": "link_openai_gatewayapi", "type": "contains", "file": "gatewayapi.md", "text": "kompatibel [[Acme AI]]"},
            ],
            "traps": [
                {"file": "invoicer.md", "unchanged": True},
                {"file": "catatan-strategi.md", "unchanged": True},
                {"file": "acme-ai.md", "keep": ["`acme/model-alpha`"]},
                {"file": "chrome.md", "forbid": ["[[Google]]"]},
                {"file": "google-docs.md", "forbid": ["[[Google]]"]},
                {"file": "tabsync.md", "forbid": ["[[Google]]"]},
            ],
            "allow_delete": ["user.md", "catatan-rapat.md"],
            "rewrite_allowed": ["dewa-anggara.md", "tabsync-v0-3-0-mode-otomatis.md"],
            "call_budget": 70,
        })


SCENARIOS = [sc_unlinked_mention, sc_dangling_link, sc_false_positive, sc_slug_resolution,
             sc_polluted_alias, sc_codespan_frontmatter, sc_mixed_batch, sc_edit_recovery,
             sc_duplicate_person, sc_broken_docs, sc_full_batch]


# ─────────────────────────── rendering ───────────────────────────

def render_task(files, focus):
    """Run the production planner on *files* and build the task message _spawn_kb_tidy would send."""
    from backend.agent_runtime import kb_tidy_plan

    with tempfile.TemporaryDirectory() as tmp:
        base = 1_700_000_000
        for i, (path, text) in enumerate(sorted(files.items())):
            full = os.path.join(tmp, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(text)
            slug = path[:-3]
            # Focus docs are the newest ("new"); everything else is already audited.
            mtime = base + 10_000 + i if slug in focus else base + i
            os.utime(full, (mtime, mtime))
        vault = kb_tidy_plan.scan_vault(tmp)
        ledger = {s: {"m": d.mtime, "t": 1.0} for s, d in vault.items() if s not in focus}
        batch, neighbors, reasons = kb_tidy_plan.plan_batch(vault, ledger, cap=max(len(focus), 1))
        plan_slugs = batch + neighbors
        plan_text = kb_tidy_plan.render_plan(vault, batch, neighbors, reasons,
                                             kb_tidy_plan.find_issues(vault, plan_slugs))
    return plan_text + "\n\n" + _TASK_TAIL_WITH_PLAN


def build_test(sc):
    from backend.agent_runtime.memory_manager import _KB_TIDY_SYSTEM_PROMPT
    return {
        "id": sc["id"],
        "name": sc["name"],
        "description": sc["description"],
        "system_prompt": _KB_TIDY_SYSTEM_PROMPT,
        "system_prompt_mode": "overwrite",
        "prompt": render_task(sc["files"], sc["focus"]),
        "expected": sc["expected"],
        "vault": {"files": dict(sorted(sc["files"].items()))},
        # Same tool set _spawn_kb_tidy gives the organizer: direxplorer Read/Grep/Glob + the three edit tools.
        "tool_ids": ["skill:direxplorer:Read", "skill:direxplorer:Grep", "skill:direxplorer:Glob",
                     "str_replace", "write_file", "delete_file"],
        "max_tool_turns": 80 if sc["level"] == 5 else 50,
        "evaluator_id": "kb_organizer",
        "timeout_ms": 600000,
        "weight": 1.0,
        "enabled": True,
    }


def main():
    written = []
    for fn in SCENARIOS:
        sc = fn()
        level_dir = os.path.join(THIS_DIR, f"level_{sc['level']}")
        os.makedirs(level_dir, exist_ok=True)
        path = os.path.join(level_dir, f"{sc['id']}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(build_test(sc), f, ensure_ascii=False, indent=2)
            f.write("\n")
        written.append(os.path.relpath(path, THIS_DIR))
    print(f"Wrote {len(written)} files:")
    for p in written:
        print("  " + p)


if __name__ == "__main__":
    main()
