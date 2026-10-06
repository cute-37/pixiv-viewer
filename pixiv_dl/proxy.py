"""访问 Pixiv 用的网络代理。

三种方式（Config.PROXY_MODE）：
- system  跟随系统：用 Windows 的代理设置 / HTTP(S)_PROXY 环境变量（没设就是直连）。这是默认值。
- none    不使用代理：即使系统设了代理也直连。
- custom  自定义：用 Config.PROXY_URL，例如 http://127.0.0.1:7890 或 socks5://127.0.0.1:1080。

程序里各处发请求时用的是 Config.PROXIES（requests 的 proxies 字典），它由上面两项算出来：
system -> {}，none -> {"http": "", "https": ""}（空字符串 = 这个协议不走代理，也不看系统设置），
custom -> {"http": 地址, "https": 地址}。改了方式或地址之后要调用 Config.apply_proxy()。
长期使用的 requests.Session 不要直接赋值 session.proxies，用 configure_session()（原因见那里）。
"""
import time
from urllib.parse import urlsplit

MODES = ("system", "none", "custom")
SCHEMES = ("http", "https", "socks5", "socks5h", "socks4", "socks4a")
# SOCKS 代理一律让代理去解析域名。写 socks5:// 时域名是在本机解析的，而需要代理的网络环境里本机往往解析不出
# 正确的地址（实测：同一个端口 socks5:// 连不上 Pixiv，socks5h:// 正常），所以这里替用户换成带 h 的写法。
REMOTE_DNS = {"socks5": "socks5h", "socks4": "socks4a"}
# 测试连通性时访问的地址：能收到任何回应（哪怕是 403 / 404）就说明连得上
TEST_TARGETS = (("Pixiv 接口", "https://app-api.pixiv.net/"), ("图片服务器", "https://i.pximg.net/"))


def normalize(url):
    """整理用户填的代理地址；不合法时抛 ValueError（消息可以直接给用户看）。

    只写了“主机:端口”的会补上 http://。空字符串原样返回（表示没填）。
    """
    text = str(url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in SCHEMES:
        raise ValueError("代理地址要以 http://、https:// 或 socks5:// 开头")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("代理地址里的端口不对") from None
    if not parts.hostname:
        raise ValueError("代理地址里没有主机名，例如 http://127.0.0.1:7890")
    if port is None:
        raise ValueError("代理地址里要写端口，例如 http://127.0.0.1:7890")
    scheme = REMOTE_DNS.get(scheme, scheme)
    if scheme.startswith("socks"):
        try:
            import socks  # noqa: F401
        except ImportError:
            raise ValueError("这个程序没有带 SOCKS 支持，请改用 http:// 开头的代理端口") from None
    auth = ""
    if parts.username:
        auth = parts.username + (":" + parts.password if parts.password else "") + "@"
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{scheme}://{auth}{host}:{port}"


def build(mode, url):
    """(方式, 地址) -> requests 用的 proxies 字典"""
    if mode == "none":
        return {"http": "", "https": ""}
    if mode == "custom" and url:
        return {"http": url, "https": url}
    return {}


def configure_session(session, proxies=None):
    """让一个 requests.Session 按当前的代理设置工作。

    单次请求时传 proxies={"https": ""} 就能直连；但放在 session.proxies 里的空字符串会被系统代理盖掉
    （requests 先把系统代理填进这次请求，再和会话的设置合并，前者优先）。所以“不使用代理”时，
    还要让这个会话不去读系统的代理设置。
    """
    if proxies is None:
        from pixiv_dl.config import Config
        proxies = Config.PROXIES or {}
    session.proxies = dict(proxies)
    session.trust_env = not (proxies and not any(proxies.values()))
    return session


def describe(mode, url):
    """给日志 / 界面看的一句话（不带用户名和密码）"""
    if mode == "none":
        return "不使用代理"
    if mode == "custom" and url:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.hostname}:{parts.port}"
    return "跟随系统设置"


def effective_system_proxy():
    """系统现在给 https 配的代理地址（没有就是空字符串），只用于在界面上告诉用户“跟随系统”实际是什么"""
    try:
        from urllib.request import getproxies
        found = getproxies()
        return str(found.get("https") or found.get("all") or found.get("http") or "")
    except Exception:
        return ""


def test(mode, url, timeout=8):
    """按给定的代理设置试着连 Pixiv。返回 {"ok", "message", "steps": [{"name", "ok", "detail"}]}。只读，不登录。"""
    import requests
    proxies = build(mode, url)
    steps, ok = [], True
    for name, target in TEST_TARGETS:
        started = time.monotonic()
        try:
            requests.get(target, proxies=proxies or None, timeout=timeout, allow_redirects=False,
                         headers={"User-Agent": "PixivViewer/proxy-test"})
            steps.append({"name": name, "ok": True, "detail": f"连得上，{int((time.monotonic() - started) * 1000)} 毫秒"})
        except requests.exceptions.ProxyError:
            ok = False
            steps.append({"name": name, "ok": False, "detail": "代理连不上或拒绝了请求：检查地址、端口，以及代理软件是否在运行"})
        except requests.exceptions.SSLError:
            ok = False
            steps.append({"name": name, "ok": False, "detail": "加密连接建立失败：多半是被干扰了，需要换一个能用的代理"})
        except requests.exceptions.Timeout:
            ok = False
            steps.append({"name": name, "ok": False, "detail": f"{timeout} 秒内没有回应"})
        except requests.exceptions.RequestException as error:
            ok = False
            steps.append({"name": name, "ok": False, "detail": f"连不上（{type(error).__name__}）"})
    return {"ok": ok, "message": "可以连上 Pixiv" if ok else "连不上 Pixiv", "steps": steps, "using": describe(mode, url)}
