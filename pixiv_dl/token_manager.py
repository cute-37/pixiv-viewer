import base64
import hashlib
import json
import logging
import secrets
from datetime import datetime
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from pixiv_dl.config import Config, mask_token

logger = logging.getLogger("PixivDownloader")

# Pixiv 安卓 App 公开的 OAuth 客户端凭据（并非用户机密）
_CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
_CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"


class PixivTokenManager:
    """封装获取 / 刷新 Pixiv Token 的 OAuth (PKCE) 逻辑。"""

    def __init__(self):
        self.client_id = _CLIENT_ID
        self.client_secret = _CLIENT_SECRET
        self.redirect_uri = 'https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback'
        self.user_agent = 'PixivAndroidApp/5.0.234 (Android 11; Pixel 5)'

    def generate_pkce_challenge(self) -> Tuple[str, str]:
        code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('utf-8').rstrip('=')
        code_sha = hashlib.sha256(code_verifier.encode('utf-8')).digest()
        code_challenge = base64.urlsafe_b64encode(code_sha).decode('utf-8').rstrip('=')
        return code_verifier, code_challenge

    def get_auth_url(self, code_challenge: str) -> str:
        params = {
            'code_challenge': code_challenge,
            'code_challenge_method': 'S256',
            'client': 'pixiv-android',
        }
        return f"https://app-api.pixiv.net/web/v1/login?{urlencode(params)}"

    def _post_token_request(self, data: Dict) -> Dict:
        headers = {'User-Agent': self.user_agent}
        response = None
        try:
            response = requests.post(
                'https://oauth.secure.pixiv.net/auth/token',
                data=data, headers=headers, timeout=15,
                proxies=Config.PROXIES or None,
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            error_message = str(e)
            try:
                if response is not None:
                    details = response.json()
                    error_message = f"API Error: {details.get('error', {}).get('message', str(details))}"
            except (ValueError, AttributeError):
                pass
            return {'error': 'NetworkError', 'message': error_message}

    def exchange_code_for_token(self, code: str, code_verifier: str) -> dict:
        return self._post_token_request({
            'client_id': self.client_id, 'client_secret': self.client_secret,
            'code': code, 'code_verifier': code_verifier,
            'grant_type': 'authorization_code', 'include_policy': 'true',
            'redirect_uri': self.redirect_uri,
        })

    def refresh_existing_token(self, refresh_token: str) -> dict:
        return self._post_token_request({
            'client_id': self.client_id, 'client_secret': self.client_secret,
            'grant_type': 'refresh_token', 'include_policy': 'true',
            'refresh_token': refresh_token,
        })


def extract_code(callback_url_or_code: str) -> Optional[str]:
    """从回调 URL（pixiv://account/login?code=... 或 https 回调）里取出 code；也接受直接粘贴的 code。"""
    text = (callback_url_or_code or "").strip()
    if not text:
        return None
    if "code=" in text or "://" in text or "?" in text:
        try:
            return parse_qs(urlparse(text).query).get('code', [None])[0]
        except Exception:
            return None
    return text


def test_token_validity(token, visibility=False):
    """测试 token 是否有效。返回 (ok, {"username","user_id"} | 错误信息 | None)。

    visibility=True 时，有效的账号还会检测 R-18 / R-18G 作品的可见性，结果放在返回的字典里（键 r18、r18g，
    值 True / False / None，见 pixiv_dl.visibility）。
    """
    try:
        from pixivpy3 import AppPixivAPI
        api = AppPixivAPI(proxies=Config.PROXIES or {})
        result = api.auth(refresh_token=token)
        if result and "error" not in result:
            user_info = api.user_detail(api.user_id)
            if user_info and "user" in user_info:
                user = user_info["user"]
                info = {"username": user.get("name", ""), "user_id": str(user.get("id", ""))}
            else:
                info = {"username": "", "user_id": str(getattr(api, 'user_id', ''))}
            if visibility:
                from pixiv_dl import visibility as vis
                try:
                    info.update(vis.check_all(api))
                except Exception as e:
                    logger.warning(f"可见性检测失败: {e}")
            return True, info
        return False, None
    except Exception as e:
        return False, str(e)


def apply_visibility(info, result):
    """把检测结果记到账号信息里（没测出来的保留原来的记录）"""
    if not isinstance(result, dict):
        return
    for key in ("r18", "r18g"):
        if result.get(key) is not None:
            info[key] = bool(result[key])
    if any(result.get(k) is not None for k in ("r18", "r18g")):
        info["visibility_tested"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def test_all_tokens(verbose=True):
    """测试所有账号并保存结果；返回 [{name, ok, username, user_id}]。"""
    results = []
    if not Config.TOKENS:
        if verbose:
            print("没有保存的账号")
        return results
    for name, info in Config.TOKENS.items():
        ok, user_info = test_token_validity(info["token"], visibility=True)
        info["is_valid"] = bool(ok)
        if ok:
            apply_visibility(info, user_info)
        info["last_tested"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        item = {"name": name, "ok": bool(ok), "username": info.get("username", ""),
                "user_id": info.get("user_id", ""), "r18": info.get("r18"), "r18g": info.get("r18g")}
        if ok and isinstance(user_info, dict):
            if user_info.get("username"):
                info["username"] = item["username"] = user_info["username"]
            if user_info.get("user_id"):
                info["user_id"] = item["user_id"] = user_info["user_id"]
        results.append(item)
        if verbose:
            print(f"  {'✔' if ok else '✘'} {name} {item['username']}")
    Config.save_settings()
    return results


def finish_oauth(callback_url_or_code: str, code_verifier: str, account_name: str = "", remark: str = ""):
    """用授权码换 token 并保存账号。返回 (ok, message, account_name)。"""
    code = extract_code(callback_url_or_code)
    if not code:
        return False, "未能在内容中找到 code 参数", ""
    resp = PixivTokenManager().exchange_code_for_token(code, code_verifier)
    refresh_token = resp.get('refresh_token')
    if not refresh_token:
        return False, f"换取 Token 失败: {resp.get('message') or resp.get('error') or resp}", ""
    ok, info = test_token_validity(refresh_token, visibility=True)
    username = info.get("username", "") if ok and isinstance(info, dict) else ""
    user_id = info.get("user_id", "") if ok and isinstance(info, dict) else ""
    name = (account_name or "").strip() or (f"{username}({remark})" if username and remark else username) or "account"
    Config.add_token(name, refresh_token, username, user_id, bool(ok), remark)
    if ok:
        apply_visibility(Config.TOKENS[name], info)
        Config.save_settings()
    return True, f"已保存账号 '{name}'", name


# ----------------------------------------------------------------------- CLI 流程
def get_new_token_flow(account_name=None) -> bool:
    manager = PixivTokenManager()
    for attempt in range(1, 4):
        code_verifier, code_challenge = manager.generate_pkce_challenge()
        auth_url = manager.get_auth_url(code_challenge)

        print(f"\n{'=' * 20} 尝试 #{attempt}/3 {'=' * 20}")
        print("1. 浏览器按 F12 打开开发者工具，切换到「网络(Network)」并勾选「保留日志」。")
        print("2. 在打开的页面中完成 Pixiv 登录。")
        print("3. 登录后在网络面板里找到 `callback?...` 请求，复制完整 URL（含 code=）。")
        input("准备好后按回车打开浏览器...")
        try:
            import webbrowser
            webbrowser.open(auth_url)
        except Exception:
            pass
        print(f"如果浏览器没有自动打开，请手动访问:\n   {auth_url}")

        callback = input("\n粘贴 callback URL（输入 q 退出）: ").strip()
        if callback.lower() == 'q':
            print("已取消。")
            return False

        remark = input("账号备注（可选，回车跳过）: ").strip()
        name = account_name or input("账号名称（回车使用用户名）: ").strip()
        if name and name in Config.TOKENS:
            if input(f"账号 '{name}' 已存在，覆盖? (y/n): ").strip().lower() != 'y':
                print("操作已取消")
                return False
        ok, msg, saved = finish_oauth(callback, code_verifier, name, remark)
        print(("✔ " if ok else "✘ ") + msg)
        if ok:
            return True
        print("失败原因通常是授权码已过期（操作太慢）、已被使用或网络问题。")
    print("已达到最大尝试次数。")
    return False


def refresh_token_flow(account_name=None):
    """刷新已有账号的 token。"""
    if account_name is None:
        if not Config.TOKENS:
            print("当前没有保存的账号，请先添加账号。")
            return
        names = list(Config.TOKENS.keys())
        for i, n in enumerate(names, 1):
            mark = " (主要)" if n == Config.MAIN_ACCOUNT else ""
            print(f"  {i}. {n}{mark} {'✔' if Config.TOKENS[n].get('is_valid', True) else '✘'}")
        choice = input("\n选择要刷新的账号序号 (q 退出): ").strip()
        if choice.lower() == 'q':
            return
        try:
            account_name = names[int(choice) - 1]
        except (ValueError, IndexError):
            print("无效选择")
            return
    if account_name not in Config.TOKENS:
        print(f"找不到账号 '{account_name}'")
        return

    print(f"正在刷新 '{account_name}' 的 Token...")
    resp = PixivTokenManager().refresh_existing_token(Config.TOKENS[account_name]["token"])
    new_token = resp.get('refresh_token')
    if new_token:
        Config.TOKENS[account_name]["token"] = new_token
        Config.TOKENS[account_name]["is_valid"] = True
        Config.TOKENS[account_name]["last_tested"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        Config.save_settings()
        print(f"✔ 已更新（{mask_token(new_token)}）")
    else:
        print("✘ 刷新失败:", json.dumps({k: v for k, v in resp.items() if k != 'refresh_token'},
                                       ensure_ascii=False))
