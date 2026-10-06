"""账号的 R-18 / R-18G 可见性检测，以及下载时按结果分配作品"""
import random

from pixiv_dl import visibility
from pixiv_dl.database import Database
from fakes import FakeAPI, make_client, make_illust, make_processor
from test_download import fresh_routes, seed


class ProbeAPI:
    """只回答 illust_detail：按作品 ID 返回 可见 / 无权查看 / 不存在"""

    def __init__(self, answers, default="hidden"):
        self.answers, self.default, self.asked = answers, default, []

    def illust_detail(self, iid):
        self.asked.append(iid)
        kind = self.answers.get(iid, self.default)
        if kind == "gone":
            return {"error": {"user_message": "Page not found"}}
        return {"illust": {"id": iid, "visible": kind == "visible", "x_restrict": 1}}


def test_built_in_probe_sets_cover_several_artists():
    assert len(visibility.R18_PROBES) == 20 and len({a for a, _ in visibility.R18_PROBES}) == 10
    assert len({a for a, _ in visibility.R18G_PROBES}) >= visibility.HIDDEN_ARTISTS_NEEDED
    ids = [i for _, i in visibility.R18_PROBES + visibility.R18G_PROBES]
    assert len(ids) == len(set(ids))


def test_one_visible_work_is_enough():
    api = ProbeAPI({}, default="visible")
    assert visibility.check(api, 1, pause=0) is True and len(api.asked) == 1


def test_hidden_needs_several_different_artists():
    api = ProbeAPI({}, default="hidden")
    assert visibility.check(api, 1, pause=0, rng=random.Random(1)) is False
    # 问了 3 位不同画师的作品就下结论，不会把 20 个都问一遍
    assert len(api.asked) == visibility.HIDDEN_ARTISTS_NEEDED
    artists = {a for a, i in visibility.R18_PROBES if i in api.asked}
    assert len(artists) == visibility.HIDDEN_ARTISTS_NEEDED


def test_deleted_probes_are_skipped_not_counted_as_hidden():
    probes = [(1, 11), (2, 22), (3, 33), (4, 44)]
    # 前面几个被删了，后面有一个看得到：仍然是可见
    assert visibility.check(ProbeAPI({11: "gone", 22: "gone", 33: "gone", 44: "visible"}), 1, probes, pause=0) is True
    # 全都被删了：没法下结论
    assert visibility.check(ProbeAPI({}, default="gone"), 1, probes, pause=0) is None
    # 只有一位画师的作品看不到（可能是那位画师自己限制了范围），其余被删：不足以认定不可见
    assert visibility.check(ProbeAPI({11: "hidden"}, default="gone"), 1, probes, pause=0) is None


def test_r18g_is_not_asked_when_r18_is_hidden():
    api = ProbeAPI({}, default="hidden")
    assert visibility.check_all(api, pause=0) == {"r18": False, "r18g": False}
    assert not set(api.asked) & {i for _, i in visibility.R18G_PROBES}


def test_can_view_treats_untested_accounts_as_able():
    assert visibility.can_view({}, 1) and visibility.can_view(None, 2) and visibility.can_view({"r18": False}, 0)
    assert not visibility.can_view({"r18": False}, 1) and visibility.can_view({"r18": True, "r18g": False}, 1)
    assert not visibility.can_view({"r18": True, "r18g": False}, 2)


def _two(cfg, main, backup, routes, **flags):
    pro, _ = make_processor(main, routes)
    pro.clients = {'backup': make_client(backup, 'backup', 'tok-b'), 'main': make_client(main)}
    cfg.TOKENS = {'main': {'token': 'tok-main', 'is_valid': True}, 'backup': {'token': 'tok-b', 'is_valid': True, **flags}}
    return pro


def test_r18_works_go_only_to_accounts_that_can_see_them(cfg, no_sleep):
    main, backup = FakeAPI(), FakeAPI()
    routes = {}
    for iid in range(301, 309):
        main.details[iid] = make_illust(iid)
        backup.details[iid] = make_illust(iid, visible=False)
        routes.update(fresh_routes(iid, 1))
    pro = _two(cfg, main, backup, routes, r18=False, r18g=False)
    db = Database.local(cfg.DB_PATH)
    for iid in range(301, 309):
        seed(db, 1, iid)
    db.conn.execute("UPDATE illust_metadata SET x_restrict = 1 WHERE illust_id <= 306")     # 301–306 是 R-18，307–308 全年龄
    db.conn.commit()
    pro.download()
    assert db.conn.execute("SELECT status, COUNT(*) FROM illusts GROUP BY 1").fetchall() == [(1, 8)]
    asked_backup = {c[1] for c in backup.calls if c[0] == 'illust_detail'}
    assert not asked_backup & set(range(301, 307))        # 备用账号一次都没有去问 R-18 作品


def test_no_account_can_see_r18_fails_without_requests(cfg, no_sleep):
    main = FakeAPI()
    main.details[401] = make_illust(401)
    pro, _ = make_processor(main, fresh_routes(401, 1))
    cfg.TOKENS = {'main': {'token': 'tok-main', 'is_valid': True, 'r18': False, 'r18g': False}}
    db = Database.local(cfg.DB_PATH)
    seed(db, 1, 401)
    db.conn.execute("UPDATE illust_metadata SET x_restrict = 1")
    db.conn.commit()
    pro.download()
    kind, err = db.conn.execute("SELECT error_kind, last_error FROM illusts").fetchone()
    assert kind == 'restricted' and '没有账号能看 R-18' in err
    assert not [c for c in main.calls if c[0] == 'illust_detail']
