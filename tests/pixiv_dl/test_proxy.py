"""代理：地址整理、三种方式各自的实际效果（用本机的假代理和假网站验证，不访问外网）"""
import http.server
import os
import threading

import pytest
import requests

from pixiv_dl import proxy


class _Site(http.server.BaseHTTPRequestHandler):
    """假网站 / 假代理：记下收到的请求行，然后回一个 200"""
    seen = None

    def do_GET(self):
        type(self).seen.append(self.path)
        body = b"hello"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _serve():
    seen = []
    handler = type("H", (_Site,), {"seen": seen})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", seen


def _env_proxies():
    return {k[:-6].lower(): v for k, v in os.environ.items() if k.lower().endswith("_proxy") and k.lower() != "no_proxy"}


class Net:
    pass


@pytest.fixture
def net(monkeypatch):
    """一个假网站、一个假代理；“系统代理”只由测试里设的环境变量决定"""
    site, site_url, site_seen = _serve()
    prox, proxy_url, proxy_seen = _serve()
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(key, raising=False)
    # Windows 上 requests 还会去读注册表里的系统代理；测试里不能受这台电脑的设置影响
    monkeypatch.setattr(requests.utils, "getproxies", _env_proxies)
    monkeypatch.setattr(requests.utils, "proxy_bypass", lambda host: False)
    n = Net()
    n.site, n.proxy, n.site_seen, n.proxy_seen = site_url, proxy_url, site_seen, proxy_seen
    n.set_system = lambda url: monkeypatch.setenv("HTTP_PROXY", url)
    yield n
    site.shutdown()
    prox.shutdown()


DEAD = "http://127.0.0.1:9"          # 没有东西在听的端口：走到这里的请求一定失败


# ---------------------------------------------------------------- 地址整理
@pytest.mark.parametrize("given, expect", [
    ("", ""), ("  ", ""),
    ("127.0.0.1:7890", "http://127.0.0.1:7890"),
    ("HTTP://127.0.0.1:7890/", "http://127.0.0.1:7890"),
    ("https://proxy.example:8443", "https://proxy.example:8443"),
    ("socks5://127.0.0.1:1080", "socks5h://127.0.0.1:1080"),       # 域名交给代理解析
    ("socks5h://127.0.0.1:1080", "socks5h://127.0.0.1:1080"),
    ("socks4://127.0.0.1:1080", "socks4a://127.0.0.1:1080"),
    ("user:pa55@10.0.0.2:8080", "http://user:pa55@10.0.0.2:8080"),
    ("http://[::1]:7890", "http://[::1]:7890"),
])
def test_normalize(given, expect):
    assert proxy.normalize(given) == expect


@pytest.mark.parametrize("given", ["ftp://127.0.0.1:21", "http://127.0.0.1", "http://:7890", "127.0.0.1:99999", "http://"])
def test_normalize_rejects(given):
    with pytest.raises(ValueError):
        proxy.normalize(given)


def test_describe_hides_credentials():
    assert proxy.describe("custom", "http://user:pa55@10.0.0.2:8080") == "http://10.0.0.2:8080"
    assert proxy.describe("none", "") == "不使用代理" and proxy.describe("system", "") == "跟随系统设置"
    assert proxy.build("custom", "") == {}                       # 选了自定义但没填地址：当作跟随系统


# ---------------------------------------------------------------- 三种方式实际走哪条路
def test_custom_goes_through_the_given_proxy(net):
    net.set_system(DEAD)                                         # 系统代理是坏的也不影响
    r = requests.get(net.site + "/a", proxies=proxy.build("custom", net.proxy), timeout=5)
    assert r.status_code == 200
    assert net.proxy_seen == [net.site + "/a"] and net.site_seen == []     # 代理收到的是完整地址


def test_none_ignores_system_proxy(net):
    net.set_system(DEAD)
    r = requests.get(net.site + "/b", proxies=proxy.build("none", ""), timeout=5)
    assert r.status_code == 200 and net.site_seen == ["/b"] and net.proxy_seen == []
    # 图片下载用的是带 session.proxies 的会话，同样要直连
    session = proxy.configure_session(requests.Session(), proxy.build("none", ""))
    assert session.get(net.site + "/c", timeout=5).status_code == 200
    assert net.site_seen == ["/b", "/c"]


def test_plain_session_proxies_would_not_be_enough(net):
    # 记录下为什么需要 configure_session：只设 session.proxies 的话，系统代理仍然会被用上
    net.set_system(net.proxy)
    session = requests.Session()
    session.proxies = proxy.build("none", "")
    session.get(net.site + "/z", timeout=5)
    assert net.proxy_seen == [net.site + "/z"]


def test_system_follows_system_proxy(net):
    net.set_system(net.proxy)
    for kwargs in ({"proxies": proxy.build("system", "") or None}, {"proxies": proxy.build("system", "")}):
        assert requests.get(net.site + "/d", timeout=5, **kwargs).status_code == 200
    session = proxy.configure_session(requests.Session(), proxy.build("system", ""))
    assert session.get(net.site + "/e", timeout=5).status_code == 200
    custom = proxy.configure_session(requests.Session(), proxy.build("custom", net.proxy))
    assert custom.trust_env and custom.proxies["http"] == net.proxy
    assert net.proxy_seen == [net.site + "/d", net.site + "/d", net.site + "/e"] and net.site_seen == []


def test_system_without_any_proxy_is_direct(net):
    assert requests.get(net.site + "/f", proxies=proxy.build("system", "") or None, timeout=5).status_code == 200
    assert net.site_seen == ["/f"]


# ---------------------------------------------------------------- 连通性测试
def test_connectivity_test_reports_each_target(net, monkeypatch):
    monkeypatch.setattr(proxy, "TEST_TARGETS", (("甲", net.site + "/x"), ("乙", net.site + "/y")))
    result = proxy.test("custom", net.proxy)
    assert result["ok"] and [s["name"] for s in result["steps"]] == ["甲", "乙"]
    assert net.proxy_seen == [net.site + "/x", net.site + "/y"]
    bad = proxy.test("custom", DEAD, timeout=3)
    assert not bad["ok"] and all(not s["ok"] for s in bad["steps"]) and "代理" in bad["steps"][0]["detail"]


# ---------------------------------------------------------------- 旧配置
def test_legacy_proxies_setting_becomes_custom(cfg):
    cfg.PROXIES = {"https": "http://127.0.0.1:7890"}
    cfg.PROXY_MODE, cfg.PROXY_URL = "", ""
    cfg.apply_proxy()
    assert (cfg.PROXY_MODE, cfg.PROXY_URL) == ("custom", "http://127.0.0.1:7890")
    assert cfg.PROXIES == {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}


def test_fresh_config_defaults_to_system(cfg):
    cfg.apply_proxy()
    assert cfg.PROXY_MODE == "system" and cfg.PROXIES == {}
