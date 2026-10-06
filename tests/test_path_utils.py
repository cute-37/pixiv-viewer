import os

from utils import path_utils as pu

UNC = "\\\\nas\\share\\a"


def test_unc_and_slash_paths_are_network():
    assert pu.is_network_path(UNC)
    assert pu.is_network_path("//nas/share/a")
    assert not pu.is_network_path("")
    assert not pu.is_network_path("relative/path")


def test_mapped_drive_detected_by_drive_type(monkeypatch):
    monkeypatch.setattr(pu, "_is_remote_drive", lambda letter: letter.upper() == "Z")
    assert pu.is_network_path("Z:\\PIXIV")
    assert pu.is_network_path("z:/PIXIV")
    assert not pu.is_network_path("C:\\Users")


def test_local_drive_is_not_remote():
    pu.forget_drive_cache()
    here = os.path.abspath(__file__)
    if len(here) > 1 and here[1] == ":":
        assert not pu.is_network_path(here)


def test_safe_exists_never_probes_network_paths(monkeypatch):
    def boom(_):
        raise AssertionError("网络路径不应在主线程被探测")

    monkeypatch.setattr(pu.os.path, "exists", boom)
    assert pu.safe_exists(UNC)  # 直接放行，不探测
    monkeypatch.undo()
    assert pu.safe_exists(os.path.abspath(__file__))
    assert not pu.safe_exists(os.path.abspath(__file__) + ".missing")
