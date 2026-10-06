import os
import time

from utils import thumbnail_cache as tc


def test_cache_key_changes_with_file_identity_and_size(tmp_path):
    base = tc.cache_path("a.jpg", 100.0, 10, 225, tmp_path)
    assert base == tc.cache_path("a.jpg", 100.0, 10, 225, tmp_path)
    assert base != tc.cache_path("a.jpg", 101.0, 10, 225, tmp_path)   # 文件被修改
    assert base != tc.cache_path("a.jpg", 100.0, 11, 225, tmp_path)   # 大小变化
    assert base != tc.cache_path("a.jpg", 100.0, 10, 300, tmp_path)   # 缩略图尺寸变化
    assert base != tc.cache_path("b.jpg", 100.0, 10, 225, tmp_path)
    assert base.suffix == ".jpg"


def _make(path, size, age_days):
    path.write_bytes(b"x" * size)
    t = time.time() - age_days * 86400
    os.utime(path, (t, t))


def test_prune_removes_expired_files(tmp_path):
    _make(tmp_path / "old.jpg", 100, 200)
    _make(tmp_path / "new.jpg", 100, 1)
    stats = tc.prune(max_bytes=10_000, max_age_days=90, cache_dir=tmp_path)
    assert stats["removed"] == 1
    assert not (tmp_path / "old.jpg").exists() and (tmp_path / "new.jpg").exists()


def test_prune_evicts_least_recently_used_when_over_capacity(tmp_path):
    for i, age in enumerate((30, 20, 10, 1)):
        _make(tmp_path / f"f{i}.jpg", 100, age)
    stats = tc.prune(max_bytes=250, max_age_days=90, cache_dir=tmp_path)
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == ["f2.jpg", "f3.jpg"], left          # 最旧的两个被淘汰
    assert stats["kept_bytes"] == 200


def test_touch_refreshes_recency(tmp_path):
    p = tmp_path / "a.jpg"
    _make(p, 10, 50)
    before = p.stat().st_mtime
    tc.touch(p)
    assert p.stat().st_mtime > before


def test_clear_removes_everything(tmp_path):
    _make(tmp_path / "a.jpg", 100, 0)
    _make(tmp_path / "b.jpg", 300, 30)
    assert tc.clear(cache_dir=tmp_path) == {"removed": 2, "freed": 400}
    assert list(tmp_path.iterdir()) == []
    assert tc.clear(cache_dir=tmp_path) == {"removed": 0, "freed": 0}
    assert tc.clear(cache_dir=tmp_path / "missing") == {"removed": 0, "freed": 0}
