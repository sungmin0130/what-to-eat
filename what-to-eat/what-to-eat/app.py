import json
import math
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

# override=True: .env를 고치면 예전 값 대신 새 값을 쓴다
load_dotenv(override=True)

app = Flask(__name__)
# nginx를 거쳐 들어오므로, 진짜 사용자 IP는 X-Forwarded-For에 있다 (요청 수 제한에 필요)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

# 요청 수 제한 (README 7번: 1분에 10번)
limiter = Limiter(get_remote_address, app=app, storage_uri="memory://")

DB_PATH = os.environ.get("APP_DB", os.path.join(os.path.dirname(__file__), "app.db"))

KAKAO_CATEGORY_URL = "https://dapi.kakao.com/v2/local/search/category.json"
KAKAO_KEYWORD_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
KAKAO_REGION_URL = "https://dapi.kakao.com/v2/local/geo/coord2regioncode.json"

KAKAO_TIMEOUT = 5        # 카카오가 5초 안에 답하지 않으면 실패로 본다
CACHE_SECONDS = 10 * 60  # 같은 위치·거리 검색은 10분 동안 캐시
MIN_RADIUS, MAX_RADIUS, RADIUS_STEP = 100, 3000, 100
MAX_PLACES = 300         # 화면에 쓰는 음식점은 최대 300곳
SINGLE_SEARCH_RADIUS = 300  # 이 거리까지는 한 번에 찾고, 더 넓으면 구역을 나눠 찾는다
GRID_CELL = 500          # 구역 한 칸의 대략 크기(m)
GRID_MAX = 4             # 한 줄에 최대 4칸 (최대 16구역)

# ---------------------------------------------------------------------------
# 대표 메뉴 표 (README 6번) — 음식점 종류의 "마지막 칸" → 대표 메뉴
# 새 종류를 넣고 싶으면 여기에 한 줄 추가하면 된다.
# ---------------------------------------------------------------------------
MENU_TABLE = {
    "국밥": ["국밥"],
    "해장국": ["해장국"],
    "설렁탕": ["설렁탕"],
    "감자탕": ["감자탕"],
    "삼겹살": ["삼겹살"],
    "육류,고기": ["고기구이"],
    "갈비": ["갈비"],
    "곱창,막창": ["곱창"],
    "족발,보쌈": ["족발"],
    "찌개,전골": ["김치찌개"],
    "냉면": ["냉면"],
    "칼국수": ["칼국수"],
    "국수": ["국수"],
    "쌈밥": ["쌈밥"],
    "죽": ["죽"],
    "도시락": ["도시락"],
    "한식": ["백반"],
    "중화요리": ["짜장면", "짬뽕"],
    "중국요리": ["짜장면", "짬뽕"],
    "마라탕": ["마라탕"],
    "면식당": ["라멘"],
    "라멘": ["라멘"],
    "샐러드": ["샐러드"],
    "중식": ["짜장면", "짬뽕"],
    "초밥,롤": ["초밥"],
    "돈까스,우동": ["돈까스"],
    "일식": ["초밥"],
    "분식": ["떡볶이", "김밥"],
    "피자": ["피자"],
    "햄버거": ["햄버거"],
    "패스트푸드": ["햄버거"],
    "양식": ["파스타"],
    "이탈리안": ["파스타"],
    "치킨": ["치킨"],
    "닭강정": ["닭강정"],
    "베트남음식": ["쌀국수"],
    "태국음식": ["팟타이"],
    "인도음식": ["커리"],
    "샤브샤브": ["샤브샤브"],
    "해물,생선": ["해물요리"],
    "회": ["회"],
    "제과,베이커리": ["빵"],
}

# 너무 넓은 종류 — 더 자세한 칸이 있으면 그쪽을 쓴다
BROAD_KINDS = {"한식", "중식", "일식", "양식", "아시아음식"}

# 종류로 거르기 (F11) — 음식점 종류의 첫 칸 → 화면에 보일 종류 이름
KIND_TABLE = {
    "한식": "한식",
    "중식": "중식",
    "일식": "일식",
    "양식": "양식",
    "분식": "분식",
    "패스트푸드": "패스트푸드",
    "치킨": "치킨",
    "아시아음식": "아시아",
    "간식": "간식",
}

# 학생용 앱이라 술집은 뺀다
SKIP_KINDS = {"술집"}


def category_parts(category_name):
    """'음식점 > 한식 > 국밥' → ['한식', '국밥']"""
    parts = [p.strip() for p in (category_name or "").split(">")]
    return [p for p in parts if p and p != "음식점"]


def pick_menus(category_name):
    """음식점 종류로 대표 메뉴를 고른다 (README 6번)."""
    parts = category_parts(category_name)
    if not parts:
        return ["기타"]
    # 카카오 종류는 보통 [큰 종류, 음식 종류, 가게 브랜드] 순서다.
    # 1) 뒤 칸부터 표에서 찾는다 ('한식'처럼 너무 넓은 종류는 건너뛴다)
    for part in reversed(parts):
        if part in MENU_TABLE and part not in BROAD_KINDS:
            return list(MENU_TABLE[part])
    # 2) 표에 없으면 음식 종류 칸을 그대로 쓴다. 가게 브랜드 칸은 쓰지 않는다.
    #    '한식 > 순대 > ○○순대' → 순대 / '샐러드 > ○○샐러드' → 샐러드
    if parts[0] not in BROAD_KINDS:
        return [parts[0].split(",")[0].strip() or "기타"]
    if len(parts) >= 2:
        return [parts[1].split(",")[0].strip() or "기타"]
    # 3) 큰 종류 하나뿐이면 (예: '한식') 표의 기본 메뉴
    if parts[0] in MENU_TABLE:
        return list(MENU_TABLE[parts[0]])
    last = parts[-1]
    # 표에 없으면 마지막 칸을 그대로 쓴다. '육류,고기'처럼 쉼표가 있으면 앞의 것만.
    return [last.split(",")[0].strip() or "기타"]


def pick_kind(category_name):
    parts = category_parts(category_name)
    if not parts:
        return "기타"
    return KIND_TABLE.get(parts[0], "기타")


def normalize_radius(value):
    """100~3000m, 100m 단위로 맞춘다. 숫자가 아니면 None."""
    try:
        r = float(value)
    except (TypeError, ValueError):
        return None
    if r != r:  # NaN
        return None
    r = int(r / RADIUS_STEP + 0.5) * RADIUS_STEP  # 가장 가까운 100m 단위
    return max(MIN_RADIUS, min(MAX_RADIUS, r))


def parse_coord(value, low, high):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not (low <= v <= high):
        return None
    return v


# ---------------------------------------------------------------------------
# 데이터베이스 (검색 캐시)
# ---------------------------------------------------------------------------
def db_connect():
    return sqlite3.connect(DB_PATH)


def init_db():
    with db_connect() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS place_cache (
                lat_key TEXT NOT NULL,
                lng_key TEXT NOT NULL,
                radius INTEGER NOT NULL,
                data TEXT NOT NULL,
                saved_at REAL NOT NULL,
                PRIMARY KEY (lat_key, lng_key, radius)
            )
            """
        )


init_db()


def cache_get(lat_key, lng_key, radius):
    with db_connect() as db:
        # 10분 지난 캐시는 지운다
        db.execute("DELETE FROM place_cache WHERE saved_at < ?", (time.time() - CACHE_SECONDS,))
        row = db.execute(
            "SELECT data FROM place_cache WHERE lat_key = ? AND lng_key = ? AND radius = ?",
            (lat_key, lng_key, radius),
        ).fetchone()
    return json.loads(row[0]) if row else None


def cache_put(lat_key, lng_key, radius, data):
    with db_connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO place_cache (lat_key, lng_key, radius, data, saved_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (lat_key, lng_key, radius, json.dumps(data, ensure_ascii=False), time.time()),
        )


# ---------------------------------------------------------------------------
# 카카오 API 부르기 (키는 서버에서만 쓴다)
# ---------------------------------------------------------------------------
class KakaoError(Exception):
    pass


class NoKeyError(Exception):
    pass


def kakao_get(url, params, timeout=KAKAO_TIMEOUT):
    key = os.environ.get("KAKAO_REST_API_KEY", "").strip()
    if not key:
        raise NoKeyError()
    try:
        resp = requests.get(
            url,
            params=params,
            headers={"Authorization": f"KakaoAK {key}"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise KakaoError(str(exc)) from exc
    if resp.status_code != 200:
        app.logger.warning("Kakao API %s → %s %s", url, resp.status_code, resp.text[:200])
        raise KakaoError(f"status {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise KakaoError("bad json") from exc


def distance_m(lat1, lng1, lat2, lng2):
    """두 좌표 사이 거리(미터)."""
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def search_areas(lat, lng, radius):
    """찾을 구역 목록 [(위도, 경도, 반경, 쪽 수)].

    카카오는 한 번에 '가까운 45곳'만 준다. 그래서 거리가 넓으면 가게가 많은 곳에서는
    바로 옆 가게만 나온다. 넓은 거리는 원을 바둑판처럼 나눠 구역마다 따로 찾는다.
    """
    areas = [(lat, lng, min(radius, SINGLE_SEARCH_RADIUS), 3)]  # 가까운 곳은 꼼꼼히
    if radius <= SINGLE_SEARCH_RADIUS:
        return areas
    n = min(GRID_MAX, math.ceil(2 * radius / GRID_CELL))   # 한 줄에 몇 칸
    cell = 2 * radius / n                                   # 한 칸 크기(m)
    m_per_lat = 111320
    m_per_lng = 111320 * math.cos(math.radians(lat))
    for i in range(n):
        for j in range(n):
            # 칸 가운데 (내 위치 기준 동서·남북 거리)
            dx = -radius + cell * (i + 0.5)
            dy = -radius + cell * (j + 0.5)
            # 원과 겹치지 않는 구석 칸은 건너뛴다
            nx = max(abs(dx) - cell / 2, 0)
            ny = max(abs(dy) - cell / 2, 0)
            if nx * nx + ny * ny > radius * radius:
                continue
            areas.append((lat + dy / m_per_lat, lng + dx / m_per_lng, math.ceil(cell * 0.71), 2))
    return areas


def fetch_area(area, deadline):
    """구역 하나를 찾는다. (문서 목록, 다 못 가져왔는지, 실패했는지)"""
    a_lat, a_lng, a_radius, pages = area
    docs, more = [], False
    for page in range(1, pages + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return docs, True, page == 1
        try:
            data = kakao_get(
                KAKAO_CATEGORY_URL,
                {
                    "category_group_code": "FD6",
                    "x": f"{a_lng:.6f}",
                    "y": f"{a_lat:.6f}",
                    "radius": a_radius,
                    "sort": "distance",
                    "size": 15,
                    "page": page,
                },
                timeout=remaining,
            )
        except KakaoError:
            return docs, True, page == 1
        docs.extend(data.get("documents") or [])
        meta = data.get("meta") or {}
        if meta.get("is_end", True):
            # 카카오는 45곳까지만 준다. 45곳을 꽉 채웠으면 더 있다는 뜻이다.
            return docs, len(docs) >= 45, False
        more = True
    return docs, more, False


def search_places(lat, lng, radius):
    """정한 거리 안 음식점을 골고루 찾아 거리순으로 준다 (F3)."""
    lat, lng = float(lat), float(lng)
    areas = search_areas(lat, lng, radius)
    deadline = time.monotonic() + KAKAO_TIMEOUT
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda a: fetch_area(a, deadline), areas))

    if all(failed for _, _, failed in results):
        raise KakaoError("all failed")

    places, seen = [], set()
    limited = any(more for _, more, _ in results)
    for docs, _, _ in results:
        for doc in docs:
            pid = str(doc.get("id") or "")
            if not pid.isdigit() or pid in seen:
                continue
            try:
                distance = round(distance_m(lat, lng, float(doc["y"]), float(doc["x"])))
            except (KeyError, TypeError, ValueError):
                continue
            if distance > radius:  # 정한 거리 밖은 버린다 (완료 기준)
                continue
            category = doc.get("category_name") or ""
            if category_parts(category) and category_parts(category)[0] in SKIP_KINDS:
                continue
            seen.add(pid)
            places.append(
                {
                    "id": pid,
                    "name": doc.get("place_name") or "",
                    "category": " > ".join(category_parts(category)),
                    "kind": pick_kind(category),
                    "menus": pick_menus(category),
                    "distance": distance,
                    "address": doc.get("road_address_name") or doc.get("address_name") or "",
                    # 지도 링크는 장소 번호로 직접 만든다 (이상한 주소가 섞이지 않게)
                    "url": f"https://place.map.kakao.com/{pid}",
                }
            )

    places.sort(key=lambda p: p["distance"])
    if len(places) > MAX_PLACES:
        # 가까운 곳만 남기지 않고, 거리순 목록에서 고르게 뽑아 먼 곳도 남긴다
        step = len(places) / MAX_PLACES
        places = [places[int(i * step)] for i in range(MAX_PLACES)]
        limited = True
    return places, limited


def region_name(lat, lng):
    """좌표 → 동네 이름 (실패해도 괜찮다)."""
    try:
        data = kakao_get(KAKAO_REGION_URL, {"x": lng, "y": lat}, timeout=2)
    except (KakaoError, NoKeyError):
        return ""
    docs = data.get("documents") or []
    # H(행정동)를 먼저, 없으면 첫 번째
    doc = next((d for d in docs if d.get("region_type") == "H"), docs[0] if docs else None)
    if not doc:
        return ""
    return doc.get("region_3depth_name") or doc.get("address_name") or ""


def build_menus(places):
    """같은 메뉴는 하나로 합치고, 메뉴마다 파는 음식점 수를 센다 (F6)."""
    menus = {}
    for p in places:
        for name in p["menus"]:
            m = menus.setdefault(name, {"name": name, "count": 0, "kinds": [], "place_ids": []})
            m["count"] += 1
            m["place_ids"].append(p["id"])
            if p["kind"] not in m["kinds"]:
                m["kinds"].append(p["kind"])
    return sorted(menus.values(), key=lambda m: (-m["count"], m["name"]))


# ---------------------------------------------------------------------------
# 화면과 API
# ---------------------------------------------------------------------------
def error(message, status):
    return jsonify(error=message), status


@app.errorhandler(429)
def too_many(exc):
    return error("너무 자주 찾았어요. 1분 뒤에 다시 해 주세요.", 429)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/places")
@limiter.limit("10 per minute")
def api_places():
    lat = parse_coord(request.args.get("lat"), -90, 90)
    lng = parse_coord(request.args.get("lng"), -180, 180)
    radius = normalize_radius(request.args.get("radius", 500))
    if lat is None or lng is None:
        return error("위치 값이 올바르지 않아요.", 400)
    if radius is None:
        return error("거리 값이 올바르지 않아요.", 400)

    # 같은 위치(소수점 3자리)·거리는 캐시를 쓴다. 검색도 이 좌표로 해서 캐시와 결과가 같게 한다.
    lat_key, lng_key = f"{lat:.3f}", f"{lng:.3f}"
    cached = cache_get(lat_key, lng_key, radius)
    if cached is not None:
        cached["cached"] = True
        return cached

    try:
        places, limited = search_places(lat_key, lng_key, radius)
    except NoKeyError:
        return error("서버에 카카오 API 키가 아직 없어요. 관리자에게 알려 주세요.", 503)
    except KakaoError:
        return error("잠시 후 다시 시도해 주세요.", 502)

    result = {
        "radius": radius,
        "region": region_name(lat_key, lng_key),
        "places": places,
        "menus": build_menus(places),
        "limited": limited,
    }
    cache_put(lat_key, lng_key, radius, result)
    result["cached"] = False
    return result


@app.get("/api/geocode")
@limiter.limit("20 per minute")
def api_geocode():
    query = (request.args.get("query") or "").strip()
    if not (1 <= len(query) <= 50):
        return error("동네 이름이나 주소를 1~50자로 써 주세요.", 400)
    try:
        data = kakao_get(KAKAO_KEYWORD_URL, {"query": query, "size": 1})
    except NoKeyError:
        return error("서버에 카카오 API 키가 아직 없어요. 관리자에게 알려 주세요.", 503)
    except KakaoError:
        return error("잠시 후 다시 시도해 주세요.", 502)
    docs = data.get("documents") or []
    if not docs:
        return error("그런 곳을 찾지 못했어요. 다른 이름으로 써 보세요.", 404)
    doc = docs[0]
    try:
        lat, lng = float(doc["y"]), float(doc["x"])
    except (KeyError, TypeError, ValueError):
        return error("잠시 후 다시 시도해 주세요.", 502)
    return {"lat": lat, "lng": lng, "name": doc.get("place_name") or query}
