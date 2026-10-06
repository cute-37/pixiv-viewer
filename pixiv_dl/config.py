import os
import json
import logging
import tempfile
import threading

logger = logging.getLogger("PixivDownloader")

# 数据目录（settings.json、db/、avatars/、logs/ 等都在这里）。
# 作为查看器的内置功能运行时由 PIXIV_DL_HOME 指定（见 webapp/downloader.py）；单独运行时就是项目根目录。
BASE_DIR = os.environ.get("PIXIV_DL_HOME") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    from utils.constants import APP_VERSION as VERSION      # 作为查看器的一部分：版本号跟随整个软件
except ImportError:                                         # 单独拿出去用时
    VERSION = "0.0.0"

# 允许写入/读取 settings.json 的字段（"current" 段）
PERSIST_KEYS = [
    'REFRESH_TOKEN', 'TOKENS', 'MAIN_ACCOUNT',
    'STORAGE_MODE', 'LOCAL_SAVE_PATH',
    'NAS_IP', 'NAS_USER', 'NAS_PASS', 'NAS_SHARE', 'NAS_BASE_PATH', 'NAS_REMOTE_NAME',
    'DOWNLOAD_THREADS', 'MAIN_ACCOUNT_SYNC_THREADS', 'BACKUP_ACCOUNT_SYNC_THREADS',
    'MAIN_ACCOUNT_DOWNLOAD_THREADS', 'BACKUP_ACCOUNT_DOWNLOAD_THREADS',
    'METADATA_REFRESH_LIMIT', 'FAILURE_RATE_THRESHOLD', 'RATE_LIMIT_ENABLED', 'REVIEW_THRESHOLD',
    'SYNC_TYPES', 'SYNC_NOVELS', 'DELAY_SYNC', 'DELAY_DOWNLOAD', 'MAX_RETRIES',
    'UGOIRA_PREFER_HQ', 'UGOIRA_WEBP_LOSSLESS', 'PROXIES', 'PROXY_MODE', 'PROXY_URL', 'HOST_CONCURRENCY', 'AUTO_CLEAN_TEMP_AFTER_DOWNLOAD',
    'WEB_HOST', 'WEB_PORT', 'DB_AUTO_BACKUP_DAYS', 'DB_BACKUP_KEEP', 'DB_JOURNAL',
    'WEBDAV_URL', 'WEBDAV_USER', 'WEBDAV_PASS', 'WEBDAV_VERIFY_TLS',
    'FTP_URL', 'FTP_USER', 'FTP_PASS', 'SFTP_URL', 'SFTP_USER', 'SFTP_PASS', 'SFTP_KEY_FILE',
    'S3_ENDPOINT', 'S3_REGION', 'S3_BUCKET', 'S3_PREFIX', 'S3_ACCESS_KEY', 'S3_SECRET_KEY', 'S3_PATH_STYLE', 'S3_VERIFY_TLS',
]

# 预设只保存存储相关的字段
PRESET_KEYS = [
    'STORAGE_MODE', 'LOCAL_SAVE_PATH',
    'NAS_IP', 'NAS_USER', 'NAS_PASS', 'NAS_SHARE', 'NAS_BASE_PATH', 'NAS_REMOTE_NAME',
    'WEBDAV_URL', 'WEBDAV_USER', 'WEBDAV_PASS', 'WEBDAV_VERIFY_TLS',
    'FTP_URL', 'FTP_USER', 'FTP_PASS', 'SFTP_URL', 'SFTP_USER', 'SFTP_PASS', 'SFTP_KEY_FILE',
    'S3_ENDPOINT', 'S3_REGION', 'S3_BUCKET', 'S3_PREFIX', 'S3_ACCESS_KEY', 'S3_SECRET_KEY', 'S3_PATH_STYLE', 'S3_VERIFY_TLS',
]

# 可通过环境变量覆盖（PIXIV_<KEY>），适合放密码，避免写进文件
ENV_KEYS = ['STORAGE_MODE', 'LOCAL_SAVE_PATH', 'NAS_IP', 'NAS_USER', 'NAS_PASS',
            'NAS_SHARE', 'NAS_BASE_PATH', 'NAS_REMOTE_NAME',
            'WEBDAV_URL', 'WEBDAV_USER', 'WEBDAV_PASS', 'FTP_URL', 'FTP_USER', 'FTP_PASS',
            'SFTP_URL', 'SFTP_USER', 'SFTP_PASS', 'SFTP_KEY_FILE',
            'S3_ENDPOINT', 'S3_REGION', 'S3_BUCKET', 'S3_PREFIX', 'S3_ACCESS_KEY', 'S3_SECRET_KEY']

PASSWORD_KEYS = ('NAS_PASS', 'WEBDAV_PASS', 'FTP_PASS', 'SFTP_PASS', 'S3_SECRET_KEY')   # 各协议的密码：只写不读，页面和日志都看不到
SECRET_KEYS = PASSWORD_KEYS + ('REFRESH_TOKEN', 'TOKENS')

_settings_lock = threading.RLock()


def _strip_userinfo(url):
    """去掉地址里误带的 用户名:密码@，避免在界面/日志里泄露。"""
    from urllib.parse import urlparse, urlunparse
    try:
        u = urlparse(url or '')
        if u.username or u.password:
            return urlunparse(u._replace(netloc=u.hostname + (f":{u.port}" if u.port else '')))
    except ValueError:
        pass
    return url or ''


def mask_token(token):
    token = token or ""
    if len(token) <= 8:
        return "*" * len(token)
    return token[:4] + "…" + token[-2:]


class Config:
    # --- 基础认证 ---
    REFRESH_TOKEN = ""  # 向后兼容的单 token；实际使用 TOKENS

    # --- 多 Token 管理 ---
    # 格式: {"账号名": {"token": "...", "username": "...", "user_id": "...",
    #                   "last_tested": "...", "is_valid": True, "remark": ""}}
    TOKENS = {}
    MAIN_ACCOUNT = ""  # 主账号名称，用于获取关注列表

    # --- 存储模式: "local" 或 "smb" ---
    STORAGE_MODE = "local"

    # --- 本地存储配置 ---
    LOCAL_SAVE_PATH = os.path.join(BASE_DIR, "downloads")
    LOCAL_TEMP_PATH = os.path.join(BASE_DIR, "temp")      # SMB 模式下的本地缓存
    AVATARS_PATH = os.path.join(BASE_DIR, "avatars")      # 画师头像目录
    CACHE_PATH = os.path.join(BASE_DIR, "cache")          # 前端缩略图缓存

    # --- NAS (SMB) 配置（密码请放 settings.json 或环境变量 PIXIV_NAS_PASS，不要写进源码）---
    NAS_IP = ""
    NAS_USER = ""
    NAS_PASS = ""
    NAS_SHARE = ""
    NAS_BASE_PATH = "PIXIV"
    NAS_REMOTE_NAME = "NAS"

    # --- 其他存储方式（STORAGE_MODE 选 webdav / ftp / sftp 时使用）---
    WEBDAV_URL = ""          # 例如 https://nas.local:5006/dav/PIXIV
    WEBDAV_USER = ""
    WEBDAV_PASS = ""
    WEBDAV_VERIFY_TLS = True  # 自签名证书时可关闭
    FTP_URL = ""             # 例如 ftp://192.168.1.100:21/PIXIV ；ftps:// 表示显式 TLS 加密
    FTP_USER = ""
    FTP_PASS = ""
    SFTP_URL = ""            # 例如 sftp://192.168.1.100:22/volume1/PIXIV ；sftp://主机/~/PIXIV 表示相对登录用户的主目录
    SFTP_USER = ""
    SFTP_PASS = ""      # 使用密钥时这里填密钥口令（没有口令就留空）
    SFTP_KEY_FILE = ""  # 私钥文件路径（可选）

    # S3 兼容对象存储（STORAGE_MODE 选 s3）：AWS S3 / MinIO / Cloudflare R2 / 阿里云 OSS / 腾讯云 COS / Backblaze B2 …
    S3_ENDPOINT = ""         # 留空 = AWS；其他服务填 https://...
    S3_REGION = ""
    S3_BUCKET = ""
    S3_PREFIX = "PIXIV"      # 保存文件夹（对象键前缀）
    S3_ACCESS_KEY = ""
    S3_SECRET_KEY = ""
    S3_PATH_STYLE = False     # MinIO 等自建服务通常需要勾选
    S3_VERIFY_TLS = True

    # --- 数据库与日志 ---
    DB_PATH = os.path.join(BASE_DIR, "db", "pixiv_manager.db")
    LOG_DIR = os.path.join(BASE_DIR, "logs")

    # --- 命名规范 ---
    FOLDER_FORMAT = "[{author_id}] {author_name}"
    FILENAME_FORMAT = "{illust_id}_p{index}.{ext}"

    # --- 网络 ---
    # 访问 Pixiv 用的代理，见 pixiv_dl/proxy.py。界面上改的是 PROXY_MODE / PROXY_URL；
    # PROXIES 是由它们算出来、各处发请求时实际使用的字典（apply_proxy 负责更新）。
    PROXY_MODE = ""   # system 跟随系统 / none 不使用 / custom 用 PROXY_URL；空 = 还没设置过（按 system 处理）
    PROXY_URL = ""    # 例如 http://127.0.0.1:7890
    PROXIES = {}
    USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36")

    # --- 并发 ---
    DOWNLOAD_THREADS = 2  # 仅在没有按账号配置时作为兜底
    MAIN_ACCOUNT_SYNC_THREADS = 1
    BACKUP_ACCOUNT_SYNC_THREADS = 2
    MAIN_ACCOUNT_DOWNLOAD_THREADS = 1
    BACKUP_ACCOUNT_DOWNLOAD_THREADS = 2

    MAX_RETRIES = 3
    DELAY_SYNC = (1.5, 3.0)      # 同步翻页间隔（秒）
    DELAY_DOWNLOAD = (0.8, 2.0)  # 每处理完一个作品后的间隔（秒）

    # --- 同步范围 ---
    SYNC_TYPES = ['illust', 'manga']  # 动图包含在 illust 中
    SYNC_NOVELS = True

    # --- 风控休息策略 (已下载数量为 threshold 倍数时暂停 N 秒，优先较大阈值) ---
    RATE_LIMIT_ENABLED = True
    RATE_LIMIT_RULES = {1000: 10, 100: 10}

    # --- 增量同步 ---
    METADATA_REFRESH_LIMIT = 20  # 遇到已同步作品后继续扫描的数量
    # “检查更新并下载”时，新发现的文件超过这个数就先停下来让用户看一眼再决定下不下（0 = 从不询问）
    REVIEW_THRESHOLD = 2000

    # --- 任务回收 ---
    IN_PROGRESS_TIMEOUT_HOURS = 6
    AUTO_CLEAN_TEMP_AFTER_DOWNLOAD = False
    TEMP_CLEAN_DAYS = 7
    MAX_ATTEMPTS = 3  # 达到该失败次数视为永久失败

    # 动图：优先下载 1920x1080 的 zip（失败回退 600x600）
    UGOIRA_PREFER_HQ = True
    UGOIRA_WEBP_LOSSLESS = True   # 预览用的 WebP 动画不再压缩（和原始帧一致；体积大、转换慢）。False=有损、体积小

    # HTTP 重试（requests Retry）
    HTTP_MAX_RETRIES = 3
    HTTP_BACKOFF_FACTOR = 0.5
    HTTP_STATUS_FORCELIST = [429, 500, 502, 503, 504]

    # 自动降速：滑动窗口失败率超过阈值时全局暂停
    AUTO_THROTTLE_ENABLED = True
    FAILURE_RATE_THRESHOLD = 0.5
    FAILURE_RATE_WINDOW = 20
    FAILURE_PAUSE_SECONDS = 10

    # 主机并发
    HOST_CONCURRENCY = {'i.pximg.net': 4}
    DEFAULT_MAX_PER_HOST = 3

    # --- 数据库维护 ---
    DB_AUTO_BACKUP_DAYS = 7   # 同步/下载前，距上次备份超过 N 天就自动备份（0 = 关闭）
    DB_BACKUP_KEEP = 5        # 自动备份最多保留几份（手动备份、迁移前备份不会被自动删除）
    DB_JOURNAL = 'delete'     # SQLite 日志模式：delete（默认）或 wal（读写并发更好）

    # --- 日志 ---
    LOG_JSON = False

    # --- 前端 ---
    WEB_HOST = "127.0.0.1"
    WEB_PORT = 8765

    # --- 运行时标志 ---
    TESTING = False
    PROMETHEUS_ENABLED = False
    PROMETHEUS_PORT = 8000

    SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")

    # ------------------------------------------------------------------ 持久化
    @classmethod
    def _read_settings_file(cls):
        if not os.path.exists(cls.SETTINGS_FILE):
            return {}
        with open(cls.SETTINGS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)

    @classmethod
    def _write_settings_file(cls, data):
        """原子写入，避免写到一半进程被杀导致 settings.json 损坏。"""
        directory = os.path.dirname(cls.SETTINGS_FILE) or "."
        fd, tmp = tempfile.mkstemp(prefix=".settings_", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            os.replace(tmp, cls.SETTINGS_FILE)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @classmethod
    def _coerce(cls, key, value):
        """JSON 里没有 tuple / int 键，读回时还原类型。"""
        if key in ('DELAY_SYNC', 'DELAY_DOWNLOAD') and isinstance(value, (list, tuple)) and len(value) == 2:
            return (float(value[0]), float(value[1]))
        if key == 'RATE_LIMIT_RULES' and isinstance(value, dict):
            return {int(k): int(v) for k, v in value.items()}
        return value

    @classmethod
    def load_settings(cls):
        """从 settings.json 加载（仅白名单字段），再应用环境变量覆盖。"""
        with _settings_lock:
            try:
                data = cls._read_settings_file()
                for k, v in data.get('current', {}).items():
                    if k in PERSIST_KEYS:
                        setattr(cls, k, cls._coerce(k, v))
            except Exception as e:
                logger.warning(f"加载配置文件失败: {e}")
            cls._apply_env()
            cls.apply_proxy()

    @classmethod
    def apply_proxy(cls):
        """按 PROXY_MODE / PROXY_URL 算出 PROXIES。改了代理设置之后调用。

        以前的版本没有这两项，只能手工在 settings.json 里写 PROXIES：那样的配置在这里转成“自定义”。
        """
        from pixiv_dl import proxy
        if cls.PROXY_MODE not in proxy.MODES:
            legacy = cls.PROXIES if isinstance(cls.PROXIES, dict) else {}
            url = str(legacy.get('https') or legacy.get('http') or '')
            cls.PROXY_MODE, cls.PROXY_URL = ('custom', url) if url else ('system', '')
        cls.PROXIES = proxy.build(cls.PROXY_MODE, cls.PROXY_URL)

    @classmethod
    def _apply_env(cls):
        for k in ENV_KEYS:
            v = os.environ.get(f"PIXIV_{k}")
            if v not in (None, ""):
                setattr(cls, k, v)

    @classmethod
    def save_settings(cls):
        with _settings_lock:
            try:
                data = {}
                try:
                    data = cls._read_settings_file()
                except Exception:
                    pass  # 文件损坏时以当前内存配置为准重新写出
                data['current'] = {k: getattr(cls, k) for k in PERSIST_KEYS if hasattr(cls, k)}
                cls._write_settings_file(data)
                return True
            except Exception as e:
                logger.error(f"保存配置文件失败: {e}")
                return False

    # ------------------------------------------------------------------ 预设
    @classmethod
    def save_preset(cls, name):
        with _settings_lock:
            try:
                data = cls._read_settings_file()
            except Exception:
                data = {}
            presets = data.get('presets', {})
            presets[name] = {k: getattr(cls, k) for k in PRESET_KEYS if hasattr(cls, k)}
            data['presets'] = presets
            try:
                cls._write_settings_file(data)
                return True
            except Exception as e:
                logger.error(f"保存预设失败: {e}")
                return False

    @classmethod
    def load_preset(cls, name):
        try:
            preset = cls._read_settings_file().get('presets', {}).get(name)
        except Exception as e:
            logger.error(f"加载预设失败: {e}")
            return False
        if not preset:
            return False
        for k, v in preset.items():
            if k in PRESET_KEYS:
                setattr(cls, k, v)
        return True

    @classmethod
    def list_presets(cls):
        try:
            return list(cls._read_settings_file().get('presets', {}).keys())
        except Exception:
            return []

    # ------------------------------------------------------------------ 账号
    @classmethod
    def add_token(cls, name, token, username="", user_id="", is_valid=True, remark=""):
        if not cls.TOKENS:
            cls.TOKENS = {}
        cls.TOKENS[name] = {
            "token": token, "username": username, "user_id": user_id,
            "last_tested": "", "is_valid": is_valid, "remark": remark,
        }
        if not cls.MAIN_ACCOUNT:
            cls.MAIN_ACCOUNT = name
        cls.save_settings()

    @classmethod
    def remove_token(cls, name):
        if name in cls.TOKENS:
            removed = cls.TOKENS.pop(name)
            if cls.REFRESH_TOKEN and cls.REFRESH_TOKEN == removed.get("token"):
                cls.REFRESH_TOKEN = ""
            if cls.MAIN_ACCOUNT == name:
                cls.MAIN_ACCOUNT = ""
            cls.save_settings()

    @classmethod
    def set_main_account(cls, name):
        if name in cls.TOKENS:
            cls.MAIN_ACCOUNT = name
            cls.save_settings()

    @classmethod
    def get_accounts(cls):
        """返回 {账号名: token信息}。没有多账号配置时回退到旧的单 REFRESH_TOKEN。"""
        if cls.TOKENS:
            return cls.TOKENS
        if cls.REFRESH_TOKEN:
            return {"default": {"token": cls.REFRESH_TOKEN, "is_valid": True}}
        return {}

    @classmethod
    def get_main_token(cls):
        accounts = cls.get_accounts()
        if cls.MAIN_ACCOUNT and cls.MAIN_ACCOUNT in accounts:
            return accounts[cls.MAIN_ACCOUNT]["token"]
        for info in accounts.values():
            if info.get("is_valid", True):
                return info["token"]
        return ""

    @classmethod
    def get_backup_tokens(cls):
        return [info["token"] for name, info in cls.get_accounts().items()
                if name != cls.MAIN_ACCOUNT and info.get("is_valid", True)]

    @classmethod
    def get_all_valid_tokens(cls):
        return [info["token"] for info in cls.get_accounts().values() if info.get("is_valid", True)]

    @classmethod
    def storage_target(cls):
        """给界面/日志显示的保存位置描述（不含密码）。"""
        m = cls.STORAGE_MODE
        if m == 'smb':
            return f"\\\\{cls.NAS_IP}\\{cls.NAS_SHARE}\\{cls.NAS_BASE_PATH}"
        if m == 's3':
            host = f" @ {cls.S3_ENDPOINT}" if cls.S3_ENDPOINT else ''
            return f"s3://{cls.S3_BUCKET}/{cls.S3_PREFIX}{host}".rstrip('/')
        if m in ('webdav', 'ftp', 'sftp'):
            return _strip_userinfo({'webdav': cls.WEBDAV_URL, 'ftp': cls.FTP_URL, 'sftp': cls.SFTP_URL}[m] or '')
        return cls.LOCAL_SAVE_PATH

    @classmethod
    def public_view(cls):
        """给 UI / 日志用的配置视图：不含任何密钥。"""
        view = {k: getattr(cls, k) for k in PERSIST_KEYS if hasattr(cls, k) and k not in SECRET_KEYS}
        view.pop('PROXIES', None)             # 界面只用 PROXY_MODE / PROXY_URL
        for k in ('WEBDAV_URL', 'FTP_URL', 'SFTP_URL'):
            view[k] = _strip_userinfo(view.get(k))
        for k in PASSWORD_KEYS:
            view[k + '_SET'] = bool(getattr(cls, k, ''))
        view['DELAY_SYNC'] = list(cls.DELAY_SYNC)
        view['DELAY_DOWNLOAD'] = list(cls.DELAY_DOWNLOAD)
        return view

    # ------------------------------------------------------------------ 连通性测试
    @staticmethod
    def validate_connection(mode, local_path=None, nas_info=None):
        """验证存储配置，返回 (ok, message)。"""
        if mode == 'local':
            if not local_path:
                return False, "路径不能为空"
            try:
                os.makedirs(local_path, exist_ok=True)
                test_file = os.path.join(local_path, '.test_write')
                with open(test_file, 'w') as f:
                    f.write('ok')
                os.remove(test_file)
                return True, "本地路径有效"
            except Exception as e:
                return False, f"本地路径无效: {e}"
        if mode == 'smb':
            if not nas_info:
                return False, "NAS 信息缺失"
            from pixiv_dl.storage import smbtools
            res = smbtools.diagnose(nas_info)
            return res['ok'], res['message']
        return False, "未知模式"


# 程序启动时自动加载配置
Config.load_settings()
