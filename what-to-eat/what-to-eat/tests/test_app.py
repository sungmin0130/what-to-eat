"""서버 테스트 — 진짜 카카오 대신 가짜 응답을 쓴다.

실행: cd ~/app && python -m pytest -q tests
"""
import os
import math
import sys
import tempfile

import pytest
import requests

# 테스트용 DB를 따로 쓴다 (진짜 app.db를 건드리지 않게)
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False, dir=os.path.dirname(__file__))
os.environ["APP_DB"] = _tmp.name
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app as app_module  # noqa: E402


def doc(pid, name, category, distance):
    # 기준점(37.5, 127.0)에서 북쪽으로 distance 미터 떨어진 곳
    return {
        "id": str(pid),
        "place_name": name,
        "category_name": category,
        "distance": str(distance),
        "road_address_name": "서울 어딘가 1",
        "address_name": "서울 어딘가 1",
        "place_url": "http://place.map.kakao.com/" + str(pid),
        "x": "127.0",
        "y": str(37.5 + distance / 111195),
    }


SAMPLE = [
    doc(1, "든든국밥", "음식점 > 한식 > 국밥", 120),
    doc(2, "홍콩반점", "음식점 > 중식 > 중화요리", 250),
    doc(3, "<b>분식왕</b>", "음식점 > 분식", 300),
    doc(4, "냉면집", "음식점 > 한식 > 냉면", 410),
    doc(5, "멀리국밥", "음식점 > 한식 > 국밥", 900),   # 반경 밖이라 빠져야 함
    doc(6, "호프", "음식점 > 술집 > 호프,요리주점", 100),  # 술집은 뺀다
]


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = ""

    def json(self):
        return self._payload


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("KAKAO_REST_API_KEY", "test-key")
    app_module.app.config["TESTING"] = True
    app_module.limiter.reset()
    with app_module.db_connect() as db:
        db.execute("DELETE FROM place_cache")
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params, "headers": headers})
        if "category" in url:
            return FakeResp({"meta": {"total_count": len(SAMPLE), "is_end": True}, "documents": SAMPLE})
        if "coord2regioncode" in url:
            return FakeResp({"documents": [{"region_type": "H", "region_3depth_name": "역삼1동"}]})
        if "keyword" in url:
            if params["query"] == "없는곳":
                return FakeResp({"documents": []})
            return FakeResp({"documents": [{"x": "127.03", "y": "37.50", "place_name": "역삼역"}]})
        raise AssertionError(url)

    monkeypatch.setattr(app_module.requests, "get", fake_get)
    with app_module.app.test_client() as c:
        c.calls = calls
        yield c


def test_places_menus_and_distance(client):
    res = client.get("/api/places?lat=37.5&lng=127.0&radius=500")
    assert res.status_code == 200
    body = res.get_json()
    assert body["radius"] == 500
    assert body["region"] == "역삼1동"
    # 모든 음식점이 정한 거리 이하, 술집은 빠짐
    assert all(p["distance"] <= 500 for p in body["places"])
    names = [p["name"] for p in body["places"]]
    assert "멀리국밥" not in names and "호프" not in names
    # 거리순
    assert [p["distance"] for p in body["places"]] == sorted(p["distance"] for p in body["places"])
    menus = {m["name"]: m for m in body["menus"]}
    assert set(menus) == {"국밥", "짜장면", "짬뽕", "떡볶이", "김밥", "냉면"}
    assert menus["국밥"]["count"] == 1
    # 지도 링크는 장소 번호로 만든 https 링크
    assert body["places"][0]["url"] == "https://place.map.kakao.com/1"


def test_api_key_sent_only_to_kakao(client):
    client.get("/api/places?lat=37.5&lng=127.0&radius=500")
    assert client.calls[0]["headers"]["Authorization"] == "KakaoAK test-key"
    page = client.get("/").get_data(as_text=True)
    assert "test-key" not in page


def test_radius_is_clamped(client):
    assert client.get("/api/places?lat=37.5&lng=127.0&radius=50").get_json()["radius"] == 100
    assert client.get("/api/places?lat=37.5&lng=127.0&radius=99999").get_json()["radius"] == 3000
    assert client.get("/api/places?lat=37.5&lng=127.0&radius=740").get_json()["radius"] == 700
    assert client.calls[0]["params"]["radius"] == 100


def test_bad_input(client):
    assert client.get("/api/places?lat=abc&lng=127&radius=500").status_code == 400
    assert client.get("/api/places?lat=95&lng=127&radius=500").status_code == 400
    assert client.get("/api/places?lat=37.5&lng=127&radius=nan").status_code == 400


def test_cache(client):
    client.get("/api/places?lat=37.5001&lng=127.0001&radius=500")
    n = len(client.calls)
    second = client.get("/api/places?lat=37.5004&lng=127.0003&radius=500").get_json()
    assert second["cached"] is True
    assert len(client.calls) == n  # 카카오를 다시 부르지 않음
    # 거리가 다르면 새로 찾는다
    client.get("/api/places?lat=37.5&lng=127.0&radius=1000")
    assert len(client.calls) > n


def test_rate_limit(client):
    for i in range(10):
        # 캐시를 피하려고 위치를 조금씩 바꾼다
        assert client.get(f"/api/places?lat=37.{500 + i}&lng=127.0&radius=500").status_code == 200
    res = client.get("/api/places?lat=37.6&lng=127.0&radius=500")
    assert res.status_code == 429
    assert "error" in res.get_json()


def test_kakao_failure(client, monkeypatch):
    def boom(*a, **k):
        raise requests.Timeout("slow")

    monkeypatch.setattr(app_module.requests, "get", boom)
    res = client.get("/api/places?lat=37.5&lng=127.0&radius=500")
    assert res.status_code == 502
    assert res.get_json()["error"] == "잠시 후 다시 시도해 주세요."


def test_no_key(client, monkeypatch):
    monkeypatch.setenv("KAKAO_REST_API_KEY", "")
    res = client.get("/api/places?lat=37.5&lng=127.0&radius=500")
    assert res.status_code == 503


def test_empty_result(client, monkeypatch):
    monkeypatch.setattr(
        app_module.requests, "get",
        lambda *a, **k: FakeResp({"meta": {"total_count": 0, "is_end": True}, "documents": []}),
    )
    body = client.get("/api/places?lat=37.5&lng=127.0&radius=300").get_json()
    assert body["places"] == [] and body["menus"] == []


def test_geocode(client):
    body = client.get("/api/geocode?query=역삼역").get_json()
    assert body == {"lat": 37.5, "lng": 127.03, "name": "역삼역"}
    assert client.get("/api/geocode?query=없는곳").status_code == 404
    assert client.get("/api/geocode?query=").status_code == 400


def test_wide_radius_searches_whole_area(client, monkeypatch):
    """넓은 거리는 구역을 나눠 찾아서, 멀리 있는 가게도 나온다."""
    far = [doc(100 + i, f"먼가게{i}", "음식점 > 양식 > 피자", 1200 + i * 100) for i in range(5)]
    near = [doc(i, f"가까운가게{i}", "음식점 > 분식", 10 + i) for i in range(45)]

    def fake_get(url, params=None, headers=None, timeout=None):
        if "coord2regioncode" in url:
            return FakeResp({"documents": []})
        # 내 위치(37.5)에서 찾으면 가까운 45곳만, 북쪽 구역에서 찾으면 먼 가게가 나온다
        if float(params["y"]) > 37.5 + 900 / 111195:
            return FakeResp({"meta": {"is_end": True}, "documents": far})
        return FakeResp({"meta": {"is_end": params["page"] >= 3}, "documents": near[(params["page"] - 1) * 15:params["page"] * 15]})

    monkeypatch.setattr(app_module.requests, "get", fake_get)
    body = client.get("/api/places?lat=37.5&lng=127.0&radius=2000").get_json()
    names = {p["name"] for p in body["places"]}
    assert {f"먼가게{i}" for i in range(5)} <= names
    assert len(body["places"]) == 50  # 같은 가게는 한 번만
    assert all(p["distance"] <= 2000 for p in body["places"])
    assert {m["name"] for m in body["menus"]} == {"떡볶이", "김밥", "피자"}


def test_search_areas_cover_circle():
    assert len(app_module.search_areas(37.5, 127.0, 300)) == 1
    areas = app_module.search_areas(37.5, 127.0, 2000)
    assert 2 <= len(areas) <= 17
    # 원 안의 아무 점이나 어느 구역엔가 들어간다
    import random
    for _ in range(500):
        ang, d = random.random() * 6.283, 2000 * random.random() ** 0.5
        plat = 37.5 + d * math.sin(ang) / 111320
        plng = 127.0 + d * math.cos(ang) / (111320 * math.cos(math.radians(37.5)))
        assert any(app_module.distance_m(a, b, plat, plng) <= r for a, b, r, _ in areas)


def test_pick_menus_fallback():
    assert app_module.pick_menus("음식점 > 한식 > 냉면") == ["냉면"]
    assert app_module.pick_menus("음식점 > 한식 > 처음보는요리") == ["처음보는요리"]
    assert app_module.pick_menus("음식점 > 양식 > 햄버거") == ["햄버거"]
    assert app_module.pick_menus("") == ["기타"]
    # 마지막 칸이 브랜드 이름이면 앞 칸에서 찾는다
    assert app_module.pick_menus("음식점 > 치킨 > BHC치킨") == ["치킨"]
    assert app_module.pick_menus("음식점 > 한식 > 해장국 > 더진국") == ["해장국"]
    assert app_module.pick_menus("음식점 > 한식 > 순대 > 신의주찹쌀순대") == ["순대"]
    assert app_module.pick_menus("음식점 > 샐러드 > 포케올데이") == ["샐러드"]
    assert app_module.pick_menus("음식점 > 한식") == ["백반"]
    assert app_module.pick_menus("음식점 > 구내식당") == ["구내식당"]


def teardown_module(module):
    try:
        os.unlink(_tmp.name)
    except OSError:
        pass
