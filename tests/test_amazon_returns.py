import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import amazon_returns as ar  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "orders_page.html"
TODAY = date(2026, 11, 5)


# ---- date parsing --------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("December 5, 2026", date(2026, 12, 5)),
    ("Dec. 5, 2026", date(2026, 12, 5)),
    ("Nov 7 2026", date(2026, 11, 7)),
    ("Dec 5", date(2026, 12, 5)),        # no year -> this year
    ("Jan 10", date(2027, 1, 10)),       # no year, just after new year -> next year
])
def test_parse_return_date(raw, expected):
    assert ar.parse_return_date(raw, TODAY) == expected


def test_parse_return_date_garbage():
    assert ar.parse_return_date("not a date", TODAY) is None


# ---- item building -------------------------------------------------------

def test_build_items_dedupes_and_sorts():
    hits = [
        {"date_text": "Dec 5, 2026", "title": "B", "asin": "B000000002", "order_id": "111-1111111-1111111"},
        {"date_text": "Dec 5, 2026", "title": "B", "asin": "B000000002", "order_id": "111-1111111-1111111"},
        {"date_text": "Nov 6, 2026", "title": "A", "asin": "B000000001", "order_id": "222-2222222-2222222"},
        # same title, different order -> kept
        {"date_text": "Dec 5, 2026", "title": "B", "asin": "B000000002", "order_id": "333-3333333-3333333"},
    ]
    items = ar.build_items(hits, TODAY)
    assert [i["title"] for i in items] == ["A", "B", "B"]
    assert items[0]["days_left"] == 1
    assert items[0]["order_url"].endswith("orderID=222-2222222-2222222")


def test_build_output_failure_keeps_previous_items_as_stale():
    prev = {"last_success": "2026-11-01T09:00:00",
            "all_items": [{"title": "X", "return_by": "2099-01-01", "days_left": 1, "order_id": ""},
                          {"title": "Expired", "return_by": "2000-01-01", "days_left": 1, "order_id": ""}]}
    out = ar.build_output("login_required", [], 2, error="expired", previous=prev)
    assert out["status"] == "login_required"
    assert out["stale"] is True
    assert out["last_success"] == "2026-11-01T09:00:00"
    assert [i["title"] for i in out["all_items"]] == ["X"]   # expired dropped
    assert out["all_items"][0]["days_left"] > 1000           # recomputed


def test_build_output_ok():
    items = [{"title": "A", "return_by": "2026-11-06", "days_left": 1},
             {"title": "B", "return_by": "2026-12-01", "days_left": 26}]
    out = ar.build_output("ok", items, 2, pages=2)
    assert out["urgent_count"] == 1 and out["total_returnable"] == 2
    assert out["next_deadline"] == "2026-11-06"
    assert out["error"] is None and out["stale"] is False


def test_write_json_atomic(tmp_path):
    p = tmp_path / "sub" / "returns.json"
    ar.write_json_atomic(p, {"a": 1})
    assert json.loads(p.read_text()) == {"a": 1}
    assert list(p.parent.iterdir()) == [p]   # no temp files left behind


# ---- in-browser extraction against a fake orders page ---------------------

@pytest.fixture(scope="module")
def extracted():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(executable_path=ar.find_brave(), headless=True)
        except Exception as e:
            pytest.skip(f"Brave not available: {e}")
        page = browser.new_page()
        page.set_content(FIXTURE.read_text())
        result = page.evaluate(ar.EXTRACTION_JS)
        browser.close()
    return result


def test_extraction_finds_every_returnable_item(extracted):
    got = {(h["title"], h["date_text"], h["order_id"]) for h in extracted["hits"]}
    assert got == {
        ("Anker USB-C Charger 65W", "November 7, 2026", "112-1234567-1234567"),
        ("Logitech MX Master 3S Mouse", "December 1, 2026", "113-7654321-7654321"),
        ("Hydro Flask 32 oz Water Bottle", "December 3, 2026", "113-7654321-7654321"),
        ("Kindle Paperwhite Case", "Nov 20, 2026", "D01-1111111-2222222"),
    }


def test_extraction_gets_asin(extracted):
    by_title = {h["title"]: h for h in extracted["hits"]}
    assert by_title["Anker USB-C Charger 65W"]["asin"] == "B0AAAAAAA1"


def test_extraction_reports_orders_and_pagination(extracted):
    assert set(extracted["orderIds"]) == {
        "112-1234567-1234567", "113-7654321-7654321", "D01-1111111-2222222", "114-0000000-0000001"}
    assert extracted["hasNext"] is True


def test_find_brave_explicit_missing():
    with pytest.raises(FileNotFoundError):
        ar.find_brave("/nope/brave")


def test_profile_lock_blocks_second_run(tmp_path):
    import threading
    order = []
    with ar.profile_lock(tmp_path):
        def second():
            with ar.profile_lock(tmp_path, wait_s=10):
                order.append("second")
        t = threading.Thread(target=second)
        t.start()
        t.join(0.5)
        order.append("first-done")   # second must still be waiting here
    t.join(10)
    assert order == ["first-done", "second"]


def test_profile_lock_gives_up(tmp_path):
    import threading
    hold, release = threading.Event(), threading.Event()

    def holder():
        with ar.profile_lock(tmp_path):
            hold.set()
            release.wait(10)
    t = threading.Thread(target=holder)
    t.start()
    hold.wait(5)
    with pytest.raises(RuntimeError, match="Brave profile"):
        with ar.profile_lock(tmp_path, wait_s=0):
            pass
    release.set()
    t.join(5)


def test_build_items_normalizes_fancy_unicode_titles():
    hits = [{"title": "𝟐𝟎𝟐𝟔 𝐔𝐩𝐠𝐫𝐚𝐝𝐞𝐝 Charger", "date_text": "November 10, 2026", "order_id": "1"}]
    items = ar.build_items(hits, today=date(2026, 11, 5))
    assert items[0]["title"] == "2026 Upgraded Charger"
