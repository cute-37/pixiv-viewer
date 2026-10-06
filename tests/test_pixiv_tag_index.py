import json
import sqlite3
import threading
import time
from contextlib import closing

from utils import pixiv_tag_index as pti
from utils.pixiv_metadata_reader import PixivMetadataReader


def _make_db(path, rows):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE illust_metadata (illust_id INTEGER PRIMARY KEY, tags TEXT, "
                     "ai_type INTEGER, x_restrict INTEGER)")
        conn.executemany("INSERT INTO illust_metadata VALUES (?, ?, 0, 0)",
                         [(i, json.dumps(tags, ensure_ascii=(i % 2 == 0))) for i, tags in rows])
        conn.commit()


ROWS = [
    (1, ["猫", "Cat", "風景"]),
    (2, ["cat", "犬"]),
    (3, ["Dog"]),
    (4, []),
    (5, ["猫"]),
]


def _reader(tmp_path, rows=ROWS, name="pixiv.db"):
    db = tmp_path / name
    _make_db(db, rows)
    return PixivMetadataReader(str(db), tag_index_dir=tmp_path / "idx"), db


def test_lookup_is_case_insensitive_and_unicode_safe(tmp_path):
    reader, _ = _reader(tmp_path)
    assert reader.find_illust_ids_by_tag("cat") == {"1", "2"}
    assert reader.find_illust_ids_by_tag("CAT") == {"1", "2"}
    assert reader.find_illust_ids_by_tag("猫") == {"1", "5"}       # ensure_ascii 转义存储的行也能匹配
    assert reader.find_illust_ids_by_tag("nothing") == set()


def test_all_tags_matches_full_scan_semantics(tmp_path):
    reader, _ = _reader(tmp_path)
    indexed = reader.get_all_tags()
    reader._fresh_tag_index = lambda db: None            # 强制走全表扫描
    assert indexed == reader.get_all_tags() == sorted({"猫", "Cat", "風景", "cat", "犬", "Dog"})


def test_index_is_built_once_and_rebuilt_when_source_changes(tmp_path, monkeypatch):
    reader, db = _reader(tmp_path)
    builds = []
    real_build = pti.PixivTagIndex._build
    monkeypatch.setattr(pti.PixivTagIndex, "_build",
                        lambda self, key, rows: (builds.append(key), real_build(self, key, rows))[1])

    reader.find_illust_ids_by_tag("cat")
    reader.find_illust_ids_by_tag("dog")
    reader.get_all_tags()
    assert len(builds) == 1                              # 之后的查询都复用索引

    time.sleep(0.02)
    with closing(sqlite3.connect(db)) as conn:           # 下载器又写入了新作品
        conn.execute("INSERT INTO illust_metadata VALUES (6, ?, 0, 0)", (json.dumps(["cat"]),))
        conn.commit()
    assert reader.find_illust_ids_by_tag("cat") == {"1", "2", "6"}
    assert len(builds) == 2                              # 源库变化 -> 重建一次


def test_concurrent_first_use_builds_only_once(tmp_path, monkeypatch):
    reader, _ = _reader(tmp_path)
    builds = []
    real_build = pti.PixivTagIndex._build

    def slow_build(self, key, rows):
        builds.append(key)
        time.sleep(0.2)
        return real_build(self, key, rows)

    monkeypatch.setattr(pti.PixivTagIndex, "_build", slow_build)
    results = []
    threads = [threading.Thread(target=lambda: results.append(reader.find_illust_ids_by_tag("cat")))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(builds) == 1
    assert results == [{"1", "2"}] * 4


def test_falls_back_to_full_scan_when_index_unusable(tmp_path):
    blocker = tmp_path / "idx"
    blocker.write_text("not a directory")                # 索引目录建不出来
    db = tmp_path / "pixiv.db"
    _make_db(db, ROWS)
    reader = PixivMetadataReader(str(db), tag_index_dir=blocker)
    assert reader.find_illust_ids_by_tag("cat") == {"1", "2"}
    assert "Dog" in reader.get_all_tags()


def test_lookup_on_large_library_is_fast_after_first_build(tmp_path):
    rows = [(i, [f"tag{i % 500}", "common", f"artist{i % 40}"]) for i in range(1, 30001)]
    reader, _ = _reader(tmp_path, rows, "big.db")

    t0 = time.perf_counter()
    first = reader.find_illust_ids_by_tag("tag7")        # 含建索引
    build_time = time.perf_counter() - t0

    t0 = time.perf_counter()
    second = reader.find_illust_ids_by_tag("tag8")
    lookup_time = time.perf_counter() - t0

    assert len(first) == 60 and len(second) == 60
    assert lookup_time < 0.1, f"带索引的查询仍用了 {lookup_time:.3f}s"
    assert lookup_time < build_time
