"""Unit tests for the local-path link guard (backend/tools/local_path_link_guard.py).

Verifies that markdown links pointing at local filesystem paths / non-web URI
schemes are detected (and never emitted as final answers), while legitimate
web links, anchors, same-origin /api/ routes and bare prose paths pass.
"""

from backend.tools.local_path_link_guard import (
    build_corrective_injection,
    detect_local_path_links,
    is_safe_href,
)


# ---------------------------------------------------------------------------
# is_safe_href
# ---------------------------------------------------------------------------

def test_safe_hrefs():
    safe = [
        "https://example.com/doc.md",
        "http://example.com/doc.md",
        "mailto:owner@example.com",
        "#section",
        "/api/agents/linus/artifacts/report.md",
        "/api/attachments/123/view",
        "//example.com/doc.md",  # protocol-relative web URL
        "",
    ]
    for href in safe:
        assert is_safe_href(href), f"expected safe: {href!r}"


def test_unsafe_hrefs():
    unsafe = [
        "/home/evonic/evonic/plugins/muktamar_api/docs/api-integration-guide.md",
        "/workspace/shared/agents/linus/artifacts/guide.md",
        "/tmp/report.pdf",
        "sandbox:/home/evonic/evonic/shared/agents/linus/artifacts/notes.md",
        "file:///etc/passwd",
        "file:/home/evonic/secret.txt",
        "C:\\Users\\owner\\Documents\\guide.md",
        "C:/Users/owner/Documents/guide.md",
        "docs/api-integration-guide.md",
        "./docs/guide.md",
        "../secrets/key.pem",
        "ftp://files.example.com/guide.md",
    ]
    for href in unsafe:
        assert not is_safe_href(href), f"expected unsafe: {href!r}"


# ---------------------------------------------------------------------------
# detect_local_path_links
# ---------------------------------------------------------------------------

def test_detect_empty_and_plain_text():
    assert detect_local_path_links("") == []
    assert detect_local_path_links("Just some text without links.") == []


def test_detect_ignores_web_links_and_anchors():
    text = (
        "See [docs](https://example.com/guide.md) and [anchor](#intro) and "
        "[artifact](/api/agents/linus/artifacts/guide.md)."
    )
    assert detect_local_path_links(text) == []


def test_detect_absolute_posix_path_link():
    text = "Dokumen tersedia di:\n\n[docs/api-integration-guide.md](/home/evonic/evonic/plugins/muktamar_api/docs/api-integration-guide.md)"
    links = detect_local_path_links(text)
    assert links == [
        "/home/evonic/evonic/plugins/muktamar_api/docs/api-integration-guide.md"
    ]


def test_detect_sandbox_and_file_uris():
    text = (
        "[Unduh catatan](sandbox:/home/evonic/evonic/shared/agents/linus/artifacts/notes.md) "
        "atau [file](file:///etc/hosts)."
    )
    links = detect_local_path_links(text)
    assert "sandbox:/home/evonic/evonic/shared/agents/linus/artifacts/notes.md" in links
    assert "file:///etc/hosts" in links


def test_detect_windows_and_relative_links():
    text = "[g](C:\\Users\\owner\\guide.md) dan [r](docs/guide.md)"
    links = detect_local_path_links(text)
    assert "C:\\Users\\owner\\guide.md" in links
    assert "docs/guide.md" in links


def test_detect_image_links_with_local_src():
    text = "![foto](/home/evonic/photo.png)"
    assert detect_local_path_links(text) == ["/home/evonic/photo.png"]


def test_detect_deduplicates_and_caps():
    text = (
        "[a](/home/evonic/a.md) [b](/home/evonic/b.md) "
        "[a-dup](/home/evonic/a.md) [c](/home/evonic/c.md) "
        "[d](/home/evonic/d.md) [e](/home/evonic/e.md) "
        "[f](/home/evonic/f.md) [g](/home/evonic/g.md)"
    )
    links = detect_local_path_links(text)
    # Duplicates collapse and the list is capped at _MAX_REPORTED_LINKS (5).
    assert links == [f"/home/evonic/{ch}.md" for ch in "abcde"]
    assert len(links) == 5


def test_detect_ignores_bare_prose_paths():
    # Developers legitimately mention filesystem locations as plain text;
    # only markdown link destinations are flagged.
    text = (
        "Repository ditemukan di `/home/www/muktamar-nu`. "
        "Lokasi `/home/www/muktamar-monitor` belum ada."
    )
    assert detect_local_path_links(text) == []


def test_detect_mixed_links_only_flags_unsafe():
    text = (
        "Lihat [guide](/home/evonic/guide.md) atau [web](https://example.com) "
        "atau [api](/api/agents/linus/artifacts/x.md)."
    )
    assert detect_local_path_links(text) == ["/home/evonic/guide.md"]


# ---------------------------------------------------------------------------
# build_corrective_injection
# ---------------------------------------------------------------------------

def test_injection_empty_for_no_links():
    assert build_corrective_injection([]) == ""


def test_injection_mentions_send_file_and_offending_links():
    links = ["/home/evonic/evonic/docs/api-integration-guide.md"]
    msg = build_corrective_injection(links)
    assert "send_file" in msg
    assert "save_artifact" in msg
    assert "/home/evonic/evonic/docs/api-integration-guide.md" in msg
    assert "[SYSTEM]" in msg
