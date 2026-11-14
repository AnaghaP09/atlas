"""Stores share one SQLite connection across request threads and run threads without failing."""

import threading

from atlas.orchestrator import RunStore
from atlas.sqlite_util import connect


def test_many_threads_can_read_and_write_one_connection():
    db = connect(":memory:", isolation_level=None)
    db.execute("CREATE TABLE t (k INTEGER PRIMARY KEY, v TEXT)")
    errors: list[BaseException] = []

    def work(n: int) -> None:
        try:
            for i in range(200):
                db.execute("INSERT OR REPLACE INTO t VALUES (?, ?)", (n * 1000 + i, "x" * 50))
                rows = db.execute("SELECT k, v FROM t WHERE k >= ?", (n * 1000,)).fetchall()
                assert rows
                assert db.execute("SELECT COUNT(*) FROM t").fetchone()[0] > 0
        except BaseException as exc:  # noqa: BLE001 - the test reports any failure
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert db.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1600


def test_cursor_keeps_rows_and_row_counts():
    db = connect(":memory:", isolation_level=None)
    db.execute("CREATE TABLE t (k INTEGER PRIMARY KEY, v TEXT)")
    cur = db.cursor()
    cur.execute("INSERT INTO t (v) VALUES (?)", ("a",))
    assert cur.rowcount == 1 and cur.lastrowid == 1
    cur.execute("SELECT v FROM t")
    assert cur.fetchone() == ("a",) and cur.fetchone() is None
    assert [r for r in db.execute("SELECT v FROM t")] == [("a",)]


def test_run_store_reads_while_another_thread_writes(tmp_path):
    store = RunStore(str(tmp_path / "runs.sqlite"))
    errors: list[BaseException] = []
    stop = threading.Event()

    def reader() -> None:
        try:
            while not stop.is_set():
                store.authoritative("run-1")
                store.generation("run-1", 1)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    readers = [threading.Thread(target=reader) for _ in range(6)]
    for t in readers:
        t.start()
    for n in range(150):
        store.write_generation("run-1", {"report_type": "investigation", "n": n})
    stop.set()
    for t in readers:
        t.join()
    assert errors == []
    assert store.authoritative("run-1")[0] == 150
