"""test_results is filtered and counted by run_id on every history query."""
import sqlite3
import threading

from models.db import db


def test_test_results_run_id_is_indexed():
    db._tls = threading.local()
    db._init_tables()
    db._init_tables()  # idempotent
    con = sqlite3.connect(db.db_path)
    try:
        cols = [r[2] for r in con.execute("PRAGMA index_info('idx_test_results_run')")]
    finally:
        con.close()
    assert cols == ["run_id"]
