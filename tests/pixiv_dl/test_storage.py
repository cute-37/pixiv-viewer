import pytest

from pixiv_dl.storage.adapter import StorageAdapter


def test_put_bytes_atomic_and_min_size(cfg):
    sa = StorageAdapter()
    n = sa.put_bytes('author/file.bin', b'x' * 200)
    assert n == 200 and sa.get_file_size('author/file.bin') == 200 and sa.exists('author/file.bin')
    with pytest.raises(ValueError):
        sa.put_bytes('author/tiny.bin', b'x' * 10)
    assert not sa.exists('author/tiny.bin') and not sa.exists('author/tiny.bin.tmp')
    sa.put_bytes('author/t.txt', b'hi', min_size=1)
    assert sa.read('author/t.txt') == b'hi'


def test_put_file_and_listing(cfg, tmp_path):
    src = tmp_path / 'src.webp'
    src.write_bytes(b'w' * 300)
    sa = StorageAdapter()
    sa.put_file('[1] A/1_p0.webp', str(src))
    listing = sa.list_dir('[1] A')
    assert listing == {'1_p0.webp': (False, 300)}
    assert sa.list_dir('nope') is None
    assert sa.list_dir('')['[1] A'][0] is True


def test_artist_folder_rename_policy(cfg):
    sa = StorageAdapter()
    assert sa.get_artist_folder(1, 'Old') == '[1] Old'
    sa.makedirs('[1] Old')
    sa.put_bytes('[1] Old/f.bin', b'x' * 200)
    sa._folders.clear()
    sa._base_dirs = None
    # 下载/核查阶段：沿用已有目录，绝不重命名（即使传入的名字不同）
    assert sa.get_artist_folder(1, 'Unknown', rename=False) == '[1] Old'
    assert sa.exists('[1] Old/f.bin')
    # 同步阶段：名字变了才重命名，内容保留
    sa._folders.clear()
    sa._base_dirs = None
    assert sa.get_artist_folder(1, 'New') == '[1] New'
    assert sa.exists('[1] New/f.bin') and not sa.exists('[1] Old/f.bin')
    assert sa.find_artist_folder(1) == '[1] New'
    assert sa.find_artist_folder(999) is None


def test_rename_conflict_keeps_old_folder(cfg):
    sa = StorageAdapter()
    sa.makedirs('[2] A')
    sa.makedirs('[2] B')  # 目标已存在
    sa._folders.clear()
    sa._base_dirs = None
    # 存在多个同 ID 前缀目录时不应崩溃，且返回一个已存在的目录
    assert sa.get_artist_folder(2, 'B') in ('[2] A', '[2] B')
