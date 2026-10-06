from pixiv_dl.extract import extract_metadata, extract_pages, is_visible, ugoira_info, ugoira_zip_candidates, url_ext
from fakes import make_illust, pj


def test_multi_page_pages_are_extracted():
    # 回归：meta_pages 分支曾因缩进错误成为死代码，多页作品得到空列表
    pages = extract_pages(pj(make_illust(1, pages=3)))
    assert [(i, u.rsplit('/', 1)[-1]) for i, u, _ in pages] == [(0, '1_p0.png'), (1, '1_p1.png'), (2, '1_p2.png')]
    assert {m for *_, m in pages} == {'image'}


def test_single_page_and_ugoira_and_empty():
    assert extract_pages(pj(make_illust(2)))[0][1].endswith('2_p0.png')
    assert extract_pages(pj(make_illust(3, type='ugoira'))) == [(0, 'ugoira://3', 'ugoira')]
    bad = make_illust(4)
    bad['meta_single_page'] = {}
    assert extract_pages(pj(bad)) == []


def test_invisible_placeholder():
    assert is_visible(pj(make_illust(1)))
    assert not is_visible(pj(make_illust(1, visible=False)))


def test_metadata_fields():
    m = extract_metadata(pj(make_illust(9, type='manga', pages=2)))
    assert m['illust_type'] == 1 and m['page_count'] == 2 and m['is_r18'] == 0
    assert m['tags'] == '["tag1"]' and m['tags_translated'] == '["T1"]' and m['is_bookmarked'] == 0 and m['ai_type'] == 1


def test_ugoira_hq_candidates():
    meta = pj({'zip_urls': {'medium': 'https://i.pximg.net/img-zip-ugoira/img/1/2/3_ugoira600x600.zip'},
               'frames': [{'file': '000000.jpg', 'delay': 60}]})
    c = ugoira_zip_candidates(meta)
    assert c[0].endswith('3_ugoira1920x1080.zip') and c[1].endswith('3_ugoira600x600.zip')
    assert ugoira_zip_candidates(meta, prefer_hq=False) == [c[1]]
    assert ugoira_info(meta)['frames'] == [{'file': '000000.jpg', 'delay': 60}]


def test_url_ext():
    assert url_ext('https://a/b/1_p0.PNG?x=1') == 'png'
    assert url_ext('https://a/b/noext') == 'jpg'
    assert url_ext(None, 'zip') == 'zip'
