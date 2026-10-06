import threading
import time


def test_reads_are_not_blocked_by_a_long_running_write(library):
    library.set_rating("a.jpg", 3)                    # 先建好表结构、写入一条数据
    assert library.get_rating("a.jpg") == 3

    release = threading.Event()
    holding = threading.Event()

    def long_write():
        with library._lock:                           # 模拟批量写入整段持有写锁
            holding.set()
            release.wait(3)

    writer = threading.Thread(target=long_write, daemon=True)
    writer.start()
    assert holding.wait(2)
    try:
        t0 = time.monotonic()
        rating = library.get_rating("a.jpg")          # 读取不应该排队等写锁
        tags = library.get_tags("a.jpg")
        elapsed = time.monotonic() - t0
        assert rating == 3 and tags == []
        assert elapsed < 0.3, f"读取被写锁阻塞了 {elapsed:.2f}s"
    finally:
        release.set()
        writer.join(2)


def test_first_use_still_initialises_schema_exactly_once(tmp_path):
    from utils.database import DatabaseManager

    manager = DatabaseManager(tmp_path / "fresh.db")
    try:
        results = []
        threads = [threading.Thread(target=lambda: results.append(manager.get_rating("x.jpg")))
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        assert results == [0] * 8
        manager.set_rating("x.jpg", 5)
        assert manager.get_rating("x.jpg") == 5
    finally:
        manager.close_thread_connection()
