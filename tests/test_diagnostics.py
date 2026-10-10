"""导出诊断信息：有日志和摘要，没有任何凭证"""
import json
import zipfile

from webapp import diagnostics

TOKEN = "rt-SECRETSECRETSECRET-0123456789"


def make(tmp_path):
    logs, home = tmp_path / "logs", tmp_path / "pixiv"
    (home / "logs").mkdir(parents=True)
    logs.mkdir()
    (home / "settings.json").write_text(json.dumps({"current": {
        "STORAGE_MODE": "smb", "NAS_IP": "10.0.0.2", "NAS_USER": "me", "NAS_PASS": "hunter2-pass",
        "WEBDAV_URL": "https://me:webdavpw123@dav.example/x", "S3_SECRET_KEY": "s3-secret-key-value", "REFRESH_TOKEN": TOKEN,
        "TOKENS": {"main": {"token": TOKEN, "is_valid": True, "username": "alice"}},
        "PROXY_MODE": "custom", "PROXY_URL": "http://u:proxypw99@127.0.0.1:7890", "REST_EVERY": 150}}), encoding="utf-8")
    (logs / "pixiv_viewer_20261007.log").write_text("启动\n读取 token " + TOKEN + " 完成\n", encoding="utf-8")
    (logs / "update.log").write_text("updated\n", encoding="utf-8")
    for i in range(12):
        (home / "logs" / f"202610{i:02d}_000000.log").write_text(f"job {i} pw=hunter2-pass\n", encoding="utf-8")
    (home / "logs" / "viewer_worker.log").write_text("", encoding="utf-8")       # 空的不放
    return logs, home


def test_bundle_has_logs_and_summary_but_no_secrets(tmp_path):
    logs, home = make(tmp_path)
    out = diagnostics.build(tmp_path / "d.zip", logs, home, {"library": {"artists": 3}})
    with zipfile.ZipFile(out["path"]) as z:
        names = z.namelist()
        blob = b"\n".join(z.read(n) for n in names).decode("utf-8")
        summary = json.loads(z.read("summary.txt"))
    assert "summary.txt" in names and "viewer/pixiv_viewer_20261007.log" in names and "viewer/update.log" in names
    assert len([n for n in names if n.startswith("downloader/")]) == diagnostics.DL_LOGS       # 只放最近的几个
    assert "downloader/20261011_000000.log" in names and "downloader/20261000_000000.log" not in names
    assert out["files"] == len(names) - 1
    for secret in (TOKEN, "hunter2-pass", "webdavpw123", "s3-secret-key-value", "proxypw99"):
        assert secret not in blob
    assert "读取 token *** 完成" in blob                                   # 日志里万一出现的凭证被抹掉
    s = summary["download_settings"]
    assert s["STORAGE_MODE"] == "smb" and s["REST_EVERY"] == 150 and "NAS_PASS" not in s and "TOKENS" not in s
    assert s["ACCOUNTS"] == {"main": {"is_valid": True, "username": "alice"}}
    assert s["WEBDAV_URL"] == "https://***@dav.example/x" and summary["library"] == {"artists": 3}


def test_bundle_works_without_any_downloader_data(tmp_path):
    (tmp_path / "logs").mkdir()
    out = diagnostics.build(tmp_path / "d.zip", tmp_path / "logs", tmp_path / "nowhere", {})
    with zipfile.ZipFile(out["path"]) as z:
        assert z.namelist() == ["summary.txt"]
