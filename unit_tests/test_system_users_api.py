"""/system#users admin API: search, counts, enrichment, approve, edit, delete."""


def _client():
    from app import app

    client = app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
    return client


def _seed():
    from models.db import db

    db.create_user("u1", "Alice Wong")
    db.create_user("u2", "Bob Stone", notes="vip customer")
    db.create_user("u3", "Carol")
    db.add_contact("u1", "whatsapp", "628111", value="+62 811 1")
    db.block_user("u3", reason="spam")
    db.add_tag("u1", "staff")


def test_list_returns_counts_and_enrichment():
    _seed()
    data = _client().get("/api/settings/users").get_json()
    assert data["counts"] == {"all": 3, "approved": 0, "pending": 2, "blocked": 1}
    alice = next(u for u in data["users"] if u["id"] == "u1")
    assert alice["contacts"][0]["channel_type"] == "whatsapp"
    assert alice["tags"] == ["staff"]
    assert alice["agents"] == []


def test_search_matches_name_notes_and_contact_and_counts_follow_search():
    _seed()
    c = _client()
    assert [u["id"] for u in c.get("/api/settings/users?q=bob").get_json()["users"]] == ["u2"]
    assert [u["id"] for u in c.get("/api/settings/users?q=vip").get_json()["users"]] == ["u2"]
    by_phone = c.get("/api/settings/users?q=628111").get_json()
    assert [u["id"] for u in by_phone["users"]] == ["u1"]
    assert by_phone["counts"]["all"] == 1
    blocked = c.get("/api/settings/users?filter=blocked").get_json()
    assert [u["id"] for u in blocked["users"]] == ["u3"]
    assert blocked["counts"]["all"] == 3  # chips ignore the status filter


def test_sort_by_name():
    _seed()
    names = [u["name"] for u in _client().get("/api/settings/users?sort=name").get_json()["users"]]
    assert names == sorted(names, key=str.lower)


def test_approve_edit_delete():
    _seed()
    c = _client()
    assert c.post("/api/settings/users/u2/approve").get_json()["success"] is True
    assert c.get("/api/settings/users?filter=approved").get_json()["counts"]["approved"] == 1
    # approving a blocked user unblocks them
    assert c.post("/api/settings/users/u3/approve").get_json()["success"] is True
    from models.db import db
    assert db.get_user("u3")["is_approved"] == 1 and not db.get_user("u3")["blocked_at"]

    assert c.put("/api/settings/users/u2", json={"name": "  "}).status_code == 400
    assert c.put("/api/settings/users/u2", json={"name": "Robert", "notes": "n"}).get_json()["success"] is True
    assert db.get_user("u2")["name"] == "Robert"

    assert c.delete("/api/settings/users/u1").get_json()["success"] is True
    assert c.get("/api/settings/users").get_json()["counts"]["all"] == 2
    assert c.delete("/api/settings/users/nope").status_code == 404
    assert c.post("/api/settings/users/nope/approve").status_code == 404
