from ai_scraper.dom import parse
from ai_scraper.pagination import find_next, page_number


def test_rel_next_wins():
    root = parse('<a href="/p3">3</a><link rel="next" href="/list?page=2">')
    assert find_next(root, "https://s.test/list") == ("https://s.test/list?page=2", "rel=next")


def test_next_link_text_and_arrows():
    assert find_next(parse('<a href="?p=2">Next &rsaquo;</a>'), "https://s.test/x")[0] == "https://s.test/x?p=2"
    assert find_next(parse('<a href="/2">More</a>'), "https://s.test/1")[0] == "https://s.test/2"


def test_numbered_pager_follows_current_page():
    html = '<div class="pagination"><a href="/l?page=1">1</a><span class="current">2</span><a href="/l?page=3">3</a></div>'
    url, rule = find_next(parse(html), "https://s.test/l?page=2")
    assert url == "https://s.test/l?page=3"


def test_never_leaves_the_site_or_follows_javascript():
    html = '<a href="https://other.test/page2">Next</a><a href="javascript:void(0)">next</a>'
    assert find_next(parse(html), "https://s.test/")[0] is None


def test_last_page_has_no_next():
    assert find_next(parse("<p>The end</p>"), "https://s.test/")[0] is None
    assert page_number("https://s.test/catalogue/page-7.html") == 7
