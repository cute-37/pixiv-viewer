"""账号的 R-18 / R-18G 可见性检测。

Pixiv 账号要在网页设置里打开「R-18 作品」「R-18G 作品」的显示才看得到对应的作品；没打开的账号请求这些作品时，
接口返回的是 visible=False 的占位项（动图接口直接返回 Page not found）。下载时把看不到的作品分给这样的账号，
只会白白浪费请求，旧版本还会把它误判成“作品已删除”。

所以验证账号时顺带测一下：拿下面这些已知的 R-18 / R-18G 作品去问，看得到就是可见。
测试作品是程序自带的（不依赖用户的库）：每档来自多位画师、投稿都超过一年、收藏数高，尽量不会失效；
即使个别被删也没关系——被删的会被跳过，只要有一个看得到就算可见，看不到则要凑够多位不同画师才下结论。
"""
import base64
import hashlib
import json
import logging
import random
import time
import zlib

from pixiv_dl.extract import _g

logger = logging.getLogger("PixivDownloader")

# 测试作品的清单：一组 (画师 ID, 作品 ID)，2026-10 时全部确认存在且分级正确（scripts/check_probes.py 可以复查）。
# 这里特意不用明文写：不想让人打开这个文件、或者在网上搜索时，一眼就看到一串成人作品的编号。
# 这只是遮一下，不是保密——解开的办法就在下面，任何人都能照着做。
# 要换一批时：python -c "from pixiv_dl.visibility import pack; print(pack([(画师, 作品), ...]))"，把输出贴到这里。
def _keystream(length):
    out, block = b"", 0
    while len(out) < length:
        out += hashlib.sha256(b"pixiv_dl.visibility/" + str(block).encode()).digest()
        block += 1
    return out[:length]


def pack(pairs):
    """[(画师 ID, 作品 ID), ...] -> 可以贴进源码的一串字符"""
    raw = zlib.compress(json.dumps([list(pair) for pair in pairs], separators=(",", ":")).encode(), 9)
    return base64.b85encode(bytes(a ^ b for a, b in zip(raw, _keystream(len(raw))))).decode()


def _unpack(text):
    data = base64.b85decode(text)
    raw = bytes(a ^ b for a, b in zip(data, _keystream(len(data))))
    return [tuple(pair) for pair in json.loads(zlib.decompress(raw))]


R18_PROBES = _unpack(
    "H;iyGGrbU+KA^!l(l=y62Co^wZg<p-YB0JDw6k$D5OPd^mNHV}?bu-O!Q^m6ImS&Mw!_$0LsZChF~+G=v{Je`1_>wI@)_$qy"
    "cP@D-ctug6g)uaU5q$5PJHamY@YsEeBhNF^-4#6vlKIMo{S{-a@*b=$*~5<sSlFLT~HY!ef0|2kH>Kibk_HWW62P$K_n;>CV"
    "fTo4eR`C(D#97)kbkiZSGYwI(%|;QU"
)
R18G_PROBES = _unpack(
    "H;iB~LERVXKH$JPC?`sGip!=z7?{om>mV{jRzg2Sp@;4#`x%kGV->N?uep2un6WrG8vOX>Vx!;Fg5=Q25oNc_Pdo=1NPUa&O"
    "qC)r+_|>Z=c*4k3Y)@#3@(4yoU9lxLh0hcwsV|Uh|xznkVb5OjHvC-gf~>!WI*F{-t(#SpbULP$=L"
)
PROBES = {1: R18_PROBES, 2: R18G_PROBES}       # 键是作品的 x_restrict：1 = R-18，2 = R-18G
HIDDEN_ARTISTS_NEEDED = 3                      # 至少这么多位不同画师的作品都看不到，才认定“不可见”
MAX_REQUESTS = 8                               # 一次检测最多问这么多个作品
FIELD = {1: "r18", 2: "r18g"}                  # 结果保存在账号信息里的字段名


def check(api, level, probes=None, pause=0.6, rng=random):
    """这个账号看不看得到 level 这一档的作品。返回 True（可见）/ False（不可见）/ None（没测出来）。

    - 任何一个测试作品看得到 → True（通常只需要一次请求）
    - 来自不少于 HIDDEN_ARTISTS_NEEDED 位不同画师的作品都返回“无权查看” → False
    - 作品不存在 / 请求出错的跳过；最后也凑不够依据就返回 None
    """
    items = list(PROBES[level] if probes is None else probes)
    rng.shuffle(items)
    hidden_artists, asked = set(), 0
    for artist, illust in items:
        if asked >= MAX_REQUESTS:
            break
        if artist in hidden_artists:
            continue                              # 同一位画师已经确认看不到，换别的画师更有说服力
        asked += 1
        try:
            res = api.illust_detail(illust)
        except Exception as e:
            logger.debug(f"可见性检测请求失败 {illust}: {e}")
            continue
        ill = _g(res, "illust")
        if ill:
            if _g(ill, "visible", True) is not False:
                return True
            hidden_artists.add(artist)
            if len(hidden_artists) >= HIDDEN_ARTISTS_NEEDED:
                return False
        if pause:
            time.sleep(pause)
    return None


def check_all(api, pause=0.6):
    """两档都测，返回 {"r18": ..., "r18g": ...}。R-18 都看不到的账号不会再去测 R-18G（一定也看不到）。"""
    r18 = check(api, 1, pause=pause)
    r18g = False if r18 is False else check(api, 2, pause=pause)
    return {"r18": r18, "r18g": r18g}


def can_view(account_info, level):
    """按上次检测的结果判断这个账号能不能下载 level 这一档的作品。没测过的当作可以（下载时发现看不到会换账号）。"""
    if not level:
        return True
    return (account_info or {}).get(FIELD.get(int(level), "r18")) is not False
