from pixiv_dl import interrupt
from fakes import FakeAPI, PJ, err, make_client
from pixiv_dl.pixiv_client import AUTH, NOT_FOUND, OTHER, RATE_LIMIT, classify_error


def test_classify():
    assert classify_error(err('Rate Limit')).kind == RATE_LIMIT
    assert classify_error(err('Error occurred at the OAuth process. Please check your Access Token')).kind == AUTH
    assert classify_error(err('', '該当作品は削除されたか、存在しない作品IDです。')).kind == NOT_FOUND
    assert classify_error(err('The creator has limited who can view this content')).kind == NOT_FOUND
    assert classify_error(err('Not Found')).kind == NOT_FOUND
    assert classify_error(err('something weird for illust 14049402')).kind == OTHER  # 数字里的 404 不能误判
    assert classify_error(PJ({'error': 'boom'})).kind == OTHER


def test_call_does_not_retry_not_found(cfg, no_sleep):
    c = make_client(FakeAPI())
    n = []
    res, e = c.call(lambda: n.append(1) or err('Not Found'))
    assert res is None and e.kind == NOT_FOUND and len(n) == 1


def test_call_reauths_on_auth_error_then_succeeds(cfg, no_sleep):
    api = FakeAPI()
    c = make_client(api)
    auths = []
    api.auth = lambda refresh_token=None: auths.append(1) or PJ({'ok': 1})
    state = {'n': 0}

    def f():
        state['n'] += 1
        return err('invalid OAuth access token') if state['n'] == 1 else PJ({'ok': 1})
    res, e = c.call(f)
    assert e is None and res and len(auths) == 1


def test_call_retries_exceptions_then_gives_up(cfg, no_sleep):
    c = make_client(FakeAPI())
    n = []

    def boom():
        n.append(1)
        raise ConnectionError('net')
    res, e = c.call(boom)
    assert res is None and e.kind == OTHER and len(n) == cfg.MAX_RETRIES


def test_call_respects_interrupt(cfg):
    c = make_client(FakeAPI())
    interrupt.set()
    try:
        res, e = c.call(lambda: PJ({'ok': 1}))
        assert res is None and e.kind == 'interrupted'
    finally:
        interrupt.clear()
