"""KB Janitor planning: ledger-driven priority batch, neighbours, heuristic issues."""
import os

from backend.agent_runtime import kb_tidy_plan as P


def _write(kb, slug, title, body, aliases=None, mtime=None):
    fm = ["---", f'title: "{title}"', "type: note"]
    if aliases:
        fm.append("aliases:")
        fm += [f'  - "{a}"' for a in aliases]
    fm += ["---", body]
    path = os.path.join(kb, f"{slug}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(fm))
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _audit_all(docs, t=1000.0):
    return {s: {"m": d.mtime, "t": t + i} for i, (s, d) in enumerate(docs.items())}


def test_scan_parses_block_and_flow_aliases(tmp_path):
    kb = str(tmp_path)
    _write(kb, "a", "Alpha", "x", aliases=["Al", "A1"])
    with open(os.path.join(kb, "b.md"), "w") as f:
        f.write('---\ntitle: "Beta"\naliases: ["Be", "B1"]\n---\nbody [[Alpha|the first]]')
    docs = P.scan_vault(kb)
    assert docs["a"].aliases == ("Al", "A1")
    assert docs["b"].aliases == ("Be", "B1")
    assert docs["b"].links == ("Alpha",)


def test_new_docs_come_first_then_changed_then_stale(tmp_path):
    kb = str(tmp_path)
    for i in range(20):
        _write(kb, f"old{i:02d}", f"Old {i}", "x", mtime=100.0)
    docs = P.scan_vault(kb)
    ledger = _audit_all(docs)
    _write(kb, "fresh", "Fresh", "x", mtime=5000.0)
    _write(kb, "old05", "Old 5", "edited", mtime=4000.0)
    docs = P.scan_vault(kb)
    batch, _, reasons = P.plan_batch(docs, ledger)
    assert batch[0] == "fresh" and reasons["fresh"] == "new"
    assert batch[1] == "old05" and reasons["old05"] == "changed"
    assert len(batch) == P.BATCH_CAP
    # the rest is filled by the longest-unaudited docs (lowest audit time)
    assert batch[2] == "old00"
    assert all(reasons[s] == "stale" for s in batch[2:])


def test_stale_rotation_covers_every_doc(tmp_path):
    kb = str(tmp_path)
    for i in range(40):
        _write(kb, f"d{i:02d}", f"Doc {i}", "x", mtime=100.0)
    ledger, seen, now = {}, set(), 1000.0
    for _ in range(3):
        docs = P.scan_vault(kb)
        batch, nb, _ = P.plan_batch(docs, ledger)
        seen.update(batch)
        now += 10
        ledger.update(P.audit_entries(docs, batch + nb, now))
    assert seen == set(P.scan_vault(kb))


def test_neighbours_of_new_docs_are_added_outside_the_cap(tmp_path):
    kb = str(tmp_path)
    _write(kb, "zz-linker", "Linker", "met [[Newbie]] once", mtime=100.0)
    _write(kb, "target", "Target", "x", mtime=100.0)
    for i in range(20):
        _write(kb, f"pad{i:02d}", f"Pad {i}", "x", mtime=100.0)
    docs = P.scan_vault(kb)
    ledger = _audit_all(docs)
    _write(kb, "newbie", "Newbie", "works with [[Target]]", mtime=9000.0)
    docs = P.scan_vault(kb)
    batch, neighbors, _ = P.plan_batch(docs, ledger)
    assert "newbie" in batch
    assert {"zz-linker", "target"} <= set(neighbors)
    assert not set(neighbors) & set(batch)
    assert len(batch) == P.BATCH_CAP


def test_stale_batch_docs_do_not_pull_in_neighbours(tmp_path):
    kb = str(tmp_path)
    _write(kb, "a", "A", "see [[B]]", mtime=100.0)
    _write(kb, "b", "B", "x", mtime=100.0)
    docs = P.scan_vault(kb)
    batch, neighbors, _ = P.plan_batch(docs, _audit_all(docs))
    assert neighbors == []


def test_find_issues_dangling_unlinked_duplicate_empty_paren(tmp_path):
    kb = str(tmp_path)
    _write(kb, "budi", "Budi Santoso", "Ketemu di Surabaya, link [[Ghost Person]].")
    _write(kb, "surabaya", "Surabaya", "Kota.")
    _write(kb, "budi-2", "Budi Santoso", "dup")
    _write(kb, "empty", "Kosong", "")
    _write(kb, "gusdur", "Abdurrahman Wahid (Gus Dur)", "x")
    _write(kb, "elsewhere", "Elsewhere", "Budi Santoso tinggal di sana.")
    docs = P.scan_vault(kb)
    text = "\n".join(P.find_issues(docs, ["budi", "empty", "gusdur"]))
    assert "DANGLING" in text and "[[Ghost Person]] -> no close doc" in text
    assert "UNLINKED mention of 'Surabaya'" in text
    assert "DUPLICATE?" in text and "[budi-2]" in text
    assert "[empty] EMPTY body" in text
    assert "WRONG TITLE" in text
    # retro-link direction: an OLD doc mentioning the focus doc's name
    assert "[elsewhere] UNLINKED mention of 'Budi Santoso'" in text


def test_existing_links_are_not_reported_as_unlinked(tmp_path):
    kb = str(tmp_path)
    _write(kb, "a", "Alpha", "see [[Surabaya]] and [[Surabaya|sby]]")
    _write(kb, "surabaya", "Surabaya", "Kota.")
    docs = P.scan_vault(kb)
    assert not [i for i in P.find_issues(docs, ["a"]) if "UNLINKED" in i]


def test_mentions_inside_code_are_not_reported_as_unlinked(tmp_path):
    kb = str(tmp_path)
    _write(kb, "a", "Alpha", "token `Bearer` dirotasi\n```\nBearer=1\n```\n")
    _write(kb, "bearer", "Bearer", "Token.")
    docs = P.scan_vault(kb)
    assert not [i for i in P.find_issues(docs, ["a"]) if "UNLINKED" in i]
    _write(kb, "b", "Beta", "token Bearer dirotasi")
    docs = P.scan_vault(kb)
    assert [i for i in P.find_issues(docs, ["b"]) if "UNLINKED" in i]


def test_render_plan_lists_batch_neighbours_and_issues(tmp_path):
    kb = str(tmp_path)
    _write(kb, "a", "Alpha", "x [[Missing]]")
    docs = P.scan_vault(kb)
    batch, nb, reasons = P.plan_batch(docs, {})
    out = P.render_plan(docs, batch, nb, reasons, P.find_issues(docs, batch))
    assert "FOCUS BATCH (1 of 1 docs" in out and "[a] Alpha (new)" in out
    assert "DETECTED ISSUES" in out and "Missing" in out


def test_dangling_links_are_grouped_per_doc_with_closest_candidates(tmp_path):
    kb = str(tmp_path)
    _write(kb, "blue-heron-project", "Blue Heron Project", "x")
    _write(kb, "series", "Blue Series",
           "[[blue-heron]] and [[blue-heron]] and [[Totally Unknown Thing]]")
    docs = P.scan_vault(kb)
    issues = [i for i in P.find_issues(docs, ["series"]) if "DANGLING" in i]
    assert len(issues) == 1                       # one line per doc, not per link
    assert "[[blue-heron]] -> closest: [blue-heron-project] Blue Heron Project" in issues[0]
    assert "[[Totally Unknown Thing]] -> no close doc" in issues[0]


def test_dangling_list_is_capped_per_doc(tmp_path):
    kb = str(tmp_path)
    body = " ".join(f"[[Ghost{i:02d}xyz]]" for i in range(12))
    _write(kb, "a", "Alpha", body)
    docs = P.scan_vault(kb)
    line = [i for i in P.find_issues(docs, ["a"]) if "DANGLING" in i][0]
    assert f"(+{12 - P.MAX_DANGLING_PER_DOC} more dangling)" in line
