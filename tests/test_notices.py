"""重要更新提醒：只对受影响的版本提示；读不到、内容不对都不影响使用；可以关掉"""
import json

from webapp.notices import CHECK_EVERY, Notices, applicable

FILE = {"notices": [
    {"id": "api-2026", "below": "1.2.0", "level": "important", "title": {"zh": "下载已失效", "en": "Downloads are broken"},
     "text": {"zh": "请更新", "en": "Please update"}},
    {"id": "old-only", "below": "1.0.5", "level": "download", "title": "只影响很早的版本"},
    {"id": "range", "below": "2.0.0", "from": "1.5.0", "level": "download", "title": "只影响 1.5 到 2.0"},
    {"id": "bad-level", "below": "9.0.0", "level": "run-this", "title": "x"},
    {"id": "no-below", "level": "important", "title": "x"},
    "not even an object",
    {"id": "no-title", "below": "9.0.0", "level": "important"},
]}


def test_only_notices_for_this_version_apply():
    assert [n["id"] for n in applicable(FILE, "1.1.9")] == ["api-2026"]
    assert [n["id"] for n in applicable(FILE, "1.0.2")] == ["api-2026", "old-only"]
    assert [n["id"] for n in applicable(FILE, "1.6.0")] == ["range"]
    assert applicable(FILE, "2.0.0") == [] and applicable(FILE, "1.2.0") == []       # 已经更新到了：不再提醒
    one = applicable(FILE, "1.0.2")[1]
    assert one["title"] == {"zh": "只影响很早的版本", "en": "只影响很早的版本"}        # 只写了一种文字时两种语言都用它
    assert set(applicable(FILE, "1.1.9")[0]) == {"id", "level", "below", "title", "text"}   # 别的字段一概不带出去
    for junk in (None, [], "x", {"notices": "nope"}, {"notices": [{"id": 1, "below": "zzz", "level": "important", "title": "t"}]}):
        assert applicable(junk, "1.1.9") == []


def make(tmp_path, fetch, enabled=lambda: True, version="1.1.9"):
    return Notices(version, "https://example.invalid/notices.json", tmp_path / "notices.json", fetch, enabled)


def test_fetches_once_a_day_and_remembers_the_result(tmp_path):
    calls = []

    def fetch(url):
        calls.append(url)
        return json.dumps(FILE)

    box = make(tmp_path, fetch)
    assert box.current() == [] and box.due()
    assert box.refresh() is True and [n["id"] for n in box.current()] == ["api-2026"]
    assert box.refresh() is False and len(calls) == 1                     # 一天之内不重复读
    again = make(tmp_path, lambda url: (_ for _ in ()).throw(OSError("offline")))
    assert [n["id"] for n in again.current()] == ["api-2026"]            # 重启后、连不上网时：用上次读到的
    saved = json.loads((tmp_path / "notices.json").read_text(encoding="utf-8"))
    saved["checked"] -= CHECK_EVERY + 5
    (tmp_path / "notices.json").write_text(json.dumps(saved), encoding="utf-8")
    assert box.due() and box.refresh() is True and len(calls) == 2       # 过了一天：再读一次
    assert make(tmp_path, fetch, version="1.2.0").current() == []        # 更新之后同一份通知不再适用


def test_failures_and_garbage_are_harmless(tmp_path):
    for fetch in (lambda url: "<html>not json</html>", lambda url: "[1, 2]", lambda url: (_ for _ in ()).throw(RuntimeError("boom"))):
        box = make(tmp_path, fetch)
        assert box.refresh(force=True) is False and box.current() == []
    assert not (tmp_path / "notices.json").exists()


def test_can_be_turned_off(tmp_path):
    calls = []
    on = {"v": True}
    box = make(tmp_path, lambda url: calls.append(url) or json.dumps(FILE), enabled=lambda: on["v"])
    box.refresh()
    on["v"] = False
    assert box.current() == [] and box.refresh(force=True) is False and len(calls) == 1      # 关掉后既不显示也不联网


def test_repo_notice_file_is_valid():
    """仓库里那份通知文件必须一直是能读的格式（写坏了所有用户都收不到提醒）"""
    from pathlib import Path
    data = json.loads((Path(__file__).resolve().parent.parent / "notices.json").read_text(encoding="utf-8"))
    assert isinstance(data.get("notices"), list)
    for it in data["notices"]:
        assert applicable({"notices": [it]}, "0.0.1"), f"这条通知写得不对，软件会忽略它: {it}"
