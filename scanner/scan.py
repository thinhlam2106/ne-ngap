"""
Né Ngập – AI xem camera giao thông TP.HCM để phát hiện ngập.

Mỗi lần chạy (GitHub Actions gọi 10 phút/lần):
  1. Xem trời có mưa hoặc triều cao không (Open-Meteo). Đang mưa thì xem tất cả camera
     mỗi lần chạy; trong 2 giờ sau mưa thì 20 phút một lần; triều cao mà không mưa thì
     xem camera gần các điểm hay ngập do triều; trời khô thì chỉ xem camera gần các điểm
     hay ngập, 3 giờ một lần.
  2. Tải ảnh chụp mới của từng camera (hệ thống camera TP.HCM do Notis vận hành).
  3. Gửi ảnh theo lô cho Claude Haiku, nhận lại mức ngập theo bánh xe máy.
  4. Chỉ công bố một chỗ ngập khi AI chắc chắn, hoặc thấy ngập ở 2 lần quét liên tiếp.
  5. Người đi đường bấm "Báo ngập" trên bản đồ: báo cáo vào hàng chờ (ntfy.sh). Bộ quét lấy
     camera trong bán kính 400 m quanh chỗ báo ra xem. Camera thấy nước thì báo cáo được xác nhận
     và hiện cho mọi người; camera thấy đường khô 2 lượt liền thì báo cáo bị loại.
     Chỗ không có camera: cần 3 máy khác nhau cùng báo trong bán kính 250 m, lúc trời vừa mưa.
  6. Ghi kết quả ra data/flood-ai.json để bản đồ đọc.

Biến môi trường:
  ANTHROPIC_API_KEY   khoá API (bắt buộc, trừ khi chạy giả lập)
  SCAN_ALL=true       quét tất cả camera ngay, bỏ qua kiểm tra mưa
  DAILY_BUDGET_USD    trần chi phí mỗi ngày, mặc định 1.5
  DRY_SCAN_HOURS      trời khô thì bao nhiêu giờ xem camera điểm hay ngập một lần, mặc định 3
  REPORT_TOPIC        tên hàng chờ báo cáo trên ntfy.sh, phải trùng với trang bản đồ
  MOCK=1              chạy thử không cần mạng và khoá API (ảnh, mô hình giả lập)
"""

import base64
import concurrent.futures as cf
import datetime as dt
import hashlib
import io
import json
import math
import os
import sys
import time
from pathlib import Path

import requests
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT_FILE = DATA / "flood-ai.json"
STATE_FILE = DATA / "state.json"
CAMS_FILE = DATA / "cams.json"
SPOTS_FILE = DATA / "spots.json"

MODEL = "claude-haiku-5-5"
# Giá Claude Haiku 5.5 (prompt dưới 100.000 token), USD cho mỗi triệu token
PRICE_IN, PRICE_OUT = 0.10, 0.50

VN = dt.timezone(dt.timedelta(hours=7))
START = time.time()
TIME_LIMIT = 8 * 60          # dừng trước khi GitHub Actions cắt (9 phút)
BATCH = 8                    # số ảnh mỗi lần gọi AI
MAX_WIDTH = 640              # thu nhỏ ảnh còn 640 px ngang (~300 token mỗi ảnh)
HOT_RADIUS_M = 500           # camera trong bán kính này quanh điểm hay ngập
CONFIRM_WINDOW = 25 * 60     # hai lần thấy ngập cách nhau tối đa 25 phút thì coi là xác nhận
EXPIRE_AFTER = 35 * 60       # quá thời gian này không quét lại thì gỡ khỏi bản đồ
MOCK = os.environ.get("MOCK") == "1"

REPORT_TOPIC = os.environ.get("REPORT_TOPIC") or "ne-ngap-bao-ngap-hcm-k7q2"
VERIFY_RADIUS_M = 400        # camera trong bán kính này quanh chỗ báo được dùng để kiểm chứng
VERIFY_MAX_CAMS = 3          # tối đa 3 camera gần nhất cho mỗi báo cáo
CHECK_FOR = 45 * 60          # thời gian chờ kiểm chứng tối đa
REPORT_KEEP = 45 * 60        # báo cáo đã xác nhận giữ trên bản đồ tới 45 phút sau lần camera thấy nước gần nhất
CROWD_RADIUS_M = 250
CROWD_MIN_DEVICES = 3
MAX_NEW_REPORTS = 80         # chống spam: mỗi lượt chỉ nhận tối đa 80 báo cáo mới

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 NeNgap/1.0"
HEADERS = {"User-Agent": UA, "Referer": "https://giaothong.hochiminhcity.gov.vn/"}

PROMPT = """You are checking snapshots from Ho Chi Minh City traffic cameras for street flooding.
Each image is preceded by its camera number. For EVERY camera, report:
- usable: false if the image is black, frozen, a "no signal"/placeholder graphic, or too dark/blurry to see the road surface. Otherwise true.
- level (integer 0-4), judged by water depth on the roadway, using vehicles, wheels, curbs and people's legs as a ruler:
  0 = no standing water. A wet, shiny or reflective road is still 0, even if it has streaks, patches or "pools" of reflected light. At night, reflections of headlights and street lights on wet asphalt are NOT water depth and must be rated 0. Tire tracks, splash marks and small puddles at the edge or in a pothole are 0.
  1 = a continuous sheet of standing water covering most of the width of at least one travel lane, with visible depth: water rising around shoes or tire treads, wakes or spray behind moving vehicles, or curbs/lane markings disappearing under water. Below ankle height, under about 10 cm.
  2 = water around half a motorbike wheel, about 15-25 cm.
  3 = water up to a motorbike's exhaust or engine, about 30-40 cm.
  4 = water at a motorbike seat or higher, over 50 cm.
  Only give level 1 or more if you can actually see a water surface with depth on the road (wakes or spray from moving vehicles, submerged curbs, lane markings or wheels). If the only evidence is shine or reflection, the level is 0. When unsure between two levels, choose the lower one, and set confidence below 0.6 whenever you are not sure.
- confidence: 0 to 1, how sure you are of the level.
- raining: true only if rain is visibly falling right now: rain streaks (especially under street lights), droplets on the lens, splashes or rings on puddles, or most riders in raincoats or under umbrellas on a wet road. A dry road, or a wet road with no sign of falling rain, is false.
- note: at most 15 words in Vietnamese describing what you see (e.g. "Nước ngập nửa bánh xe máy, xe đi chậm").
Call the report_cameras tool once with one entry per camera number."""

TOOL = {
    "name": "report_cameras",
    "description": "Report flood assessment for each camera image.",
    "input_schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "camera": {"type": "integer"},
                        "usable": {"type": "boolean"},
                        "level": {"type": "integer", "minimum": 0, "maximum": 4},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "raining": {"type": "boolean"},
                        "note": {"type": "string"},
                    },
                    "required": ["camera", "usable", "level", "confidence", "raining", "note"],
                },
            }
        },
        "required": ["results"],
    },
}


def log(*a):
    print(f"[{time.time() - START:6.1f}s]", *a, flush=True)


def now_ts():
    return int(time.time())


def iso(ts):
    return dt.datetime.fromtimestamp(ts, VN).isoformat(timespec="seconds")


def dist_m(la1, lo1, la2, lo2):
    x = math.radians(lo2 - lo1) * math.cos(math.radians((la1 + la2) / 2))
    y = math.radians(la2 - la1)
    return math.hypot(x, y) * 6371000


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- camera
def load_cameras():
    """Danh mục camera: thử lấy mới từ Notis, lỗi thì dùng bản lưu sẵn."""
    cams = []
    if not MOCK:
        try:
            r = requests.get("https://api.notis.vn/v4/cameras/bybbox?lat1=11.20&lng1=106.90&lat2=10.30&lng2=106.30",
                             headers=HEADERS, timeout=20)
            r.raise_for_status()
            for x in r.json():
                lo, la = x["loc"]["coordinates"]
                cams.append({"id": x["_id"], "name": x.get("name", "").strip(), "lat": la, "lng": lo, "notis": True})
            log(f"Danh mục camera mới từ Notis: {len(cams)}")
        except Exception as e:
            log("Không lấy được danh mục mới, dùng bản lưu sẵn:", repr(e)[:120])
            cams = []
    if not cams:
        for x in load_json(CAMS_FILE, []):
            cams.append({"id": x[0], "name": x[1], "lat": x[2], "lng": x[3], "notis": bool(x[4])})
    else:
        # giữ cả camera chỉ có trong danh mục lưu sẵn (lấy ảnh qua cổng thành phố)
        known = {c["id"] for c in cams}
        for x in load_json(CAMS_FILE, []):
            if x[0] not in known:
                cams.append({"id": x[0], "name": x[1], "lat": x[2], "lng": x[3], "notis": False})
    return cams


def snapshot_urls(cam):
    urls = []
    if cam["notis"]:
        urls.append(f"https://api.notis.vn/v4/cameras/{cam['id']}/snapshot")
    urls.append(f"https://giaothong.hochiminhcity.gov.vn:8007/Render/CameraHandler.ashx?id={cam['id']}")
    return urls


def fetch_image(cam):
    """Tải ảnh, thu nhỏ còn MAX_WIDTH, trả về (jpeg_bytes, sha1) hoặc (None, lỗi)."""
    if MOCK:
        return mock_image(cam)
    last = "không rõ"
    for url in snapshot_urls(cam):
        try:
            r = requests.get(url, params={"t": int(time.time() * 1000)}, headers=HEADERS, timeout=12)
            if r.status_code != 200 or len(r.content) < 2000:
                last = f"HTTP {r.status_code}, {len(r.content)} byte"
                continue
            im = Image.open(io.BytesIO(r.content)).convert("RGB")
            if im.width > MAX_WIDTH:
                im = im.resize((MAX_WIDTH, round(im.height * MAX_WIDTH / im.width)), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=72)
            data = buf.getvalue()
            return data, hashlib.sha1(r.content).hexdigest()
        except Exception as e:
            last = repr(e)[:80]
    return None, last


# ---------------------------------------------------------------- thời tiết
def weather():
    """Lượng mưa lớn nhất trong 1 giờ qua trên lưới TP.HCM và mực triều hiện tại."""
    if MOCK:
        return {"rain_1h": float(os.environ.get("MOCK_RAIN", "12")), "tide": float(os.environ.get("MOCK_TIDE", "1.2"))}
    lats, lons = [], []
    la = 10.65
    while la <= 10.951:
        lo = 106.55
        while lo <= 106.851:
            lats.append(f"{la:.2f}"); lons.append(f"{lo:.2f}")
            lo += 0.05
        la += 0.05
    out = {"rain_1h": None, "tide": None}
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", timeout=20, params={
            "latitude": ",".join(lats), "longitude": ",".join(lons),
            "minutely_15": "precipitation", "past_minutely_15": 5, "forecast_minutely_15": 1, "timezone": "GMT"})
        r.raise_for_status()
        js = r.json()
        js = js if isinstance(js, list) else [js]
        now = time.time()
        best = 0.0
        for o in js:
            m = o.get("minutely_15") or {}
            tot = 0.0
            for t, p in zip(m.get("time", []), m.get("precipitation", [])):
                end = dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc).timestamp()
                if 0 <= now - end <= 3600 + 450 and p:
                    tot += p
            best = max(best, tot)
        out["rain_1h"] = round(best, 1)
    except Exception as e:
        log("Không lấy được số liệu mưa:", repr(e)[:120])
    try:
        r = requests.get("https://marine-api.open-meteo.com/v1/marine", timeout=20, params={
            "latitude": 10.30, "longitude": 107.10, "hourly": "sea_level_height_msl", "past_days": 1, "forecast_days": 1, "timezone": "GMT"})
        r.raise_for_status()
        h = r.json().get("hourly", {})
        now = time.time()
        pts = [(dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc).timestamp(), v)
               for t, v in zip(h.get("time", []), h.get("sea_level_height_msl", [])) if v is not None]
        for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
            if t0 <= now <= t1:
                out["tide"] = round(v0 + (v1 - v0) * (now - t0) / (t1 - t0), 2)
    except Exception as e:
        log("Không lấy được số liệu triều:", repr(e)[:120])
    return out


# ---------------------------------------------------------------- radar mưa (RainViewer)
# Bảng màu "Universal Blue" của RainViewer (dBZ -> RGB), từ 15 dBZ trở lên (mưa thật, không tính nhiễu).
UB = {15: "88ddee", 16: "6cd1eb", 17: "51c5e8", 18: "36bae5", 19: "1baee2", 20: "00a3e0", 21: "009ad5", 22: "0091ca",
      23: "0088bf", 24: "007fb4", 25: "0077aa", 26: "0070a3", 27: "00699c", 28: "006295", 29: "005b8e", 30: "005588",
      31: "005180", 32: "004e78", 33: "004a70", 34: "004768", 35: "ffee00", 36: "ffe000", 37: "ffd200", 38: "ffc500",
      39: "ffb700", 40: "ffaa00", 41: "ff9f00", 42: "ff9500", 43: "ff8b00", 44: "ff8100", 45: "ff4400", 46: "f23600",
      47: "e62800", 48: "d91b00", 49: "cd0d00", 50: "c10000", 51: "a80000", 52: "8f0000", 53: "760000", 54: "5d0000",
      55: "ffaaff", 56: "ff9fff", 57: "ff95ff", 58: "ff8bff", 59: "ff81ff", 60: "ff77ff", 61: "ff6cff", 62: "ff62ff",
      63: "ff58ff", 64: "ff4eff", 65: "ffffff"}
UB_RGB = [(d, tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))) for d, h in UB.items()]
RADAR_BBOX = (10.35, 11.20, 106.35, 107.05)     # lat_min, lat_max, lng_min, lng_max (TP.HCM mới)
RADAR_Z = 7                                     # RainViewer miễn phí chỉ tới zoom 7 (mỗi điểm ảnh ~1,2 km)


def dbz_to_mmh(dbz):
    """Marshall–Palmer: Z = 200·R^1.6."""
    return round((10 ** (dbz / 10) / 200) ** (1 / 1.6), 1)


def _dbz_of(rgba):
    r, g, b, a = rgba
    if a < 250:
        return None                              # dưới 15 dBZ: bán trong suốt, coi như không mưa
    best, bd = None, 1e9
    for d, (R, G, B) in UB_RGB:
        e = (r - R) ** 2 + (g - G) ** 2 + (b - B) ** 2
        if e < bd:
            best, bd = d, e
    return best if bd < 900 else None


def radar():
    """Ảnh radar mới nhất của RainViewer trên TP.HCM. Trả về dict hoặc None nếu lỗi."""
    if MOCK:
        mm = float(os.environ.get("MOCK_RADAR_DBZ", "0"))
        cells = [[10.80, 106.71, dbz_to_mmh(mm)]] * 6 if mm >= 15 else []
        return {"time": now_ts() - 300, "max_dbz": mm if cells else 0, "max_mmh": dbz_to_mmh(mm) if cells else 0,
                "heavy_px": 6 if mm >= 30 else 0, "rain_px": len(cells), "cells": cells}
    try:
        j = requests.get("https://api.rainviewer.com/public/weather-maps.json", headers={"User-Agent": UA}, timeout=15).json()
        fr = j["radar"]["past"][-1]
        host = j["host"]
        n = 2 ** RADAR_Z

        def tx(lng): return (lng + 180) / 360 * n
        def ty(lat):
            la = math.radians(lat)
            return (1 - math.asinh(math.tan(la)) / math.pi) / 2 * n
        la0, la1, lo0, lo1 = RADAR_BBOX
        xs = range(int(tx(lo0)), int(tx(lo1)) + 1)
        ys = range(int(ty(la1)), int(ty(la0)) + 1)
        cells, max_dbz, heavy, rainy = [], 0, 0, 0
        for x in xs:
            for y in ys:
                url = f"{host}{fr['path']}/256/{RADAR_Z}/{x}/{y}/2/0_0.png"
                r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
                r.raise_for_status()
                im = Image.open(io.BytesIO(r.content)).convert("RGBA")
                px = im.load()
                for j2 in range(im.height):
                    gy = (y * 256 + j2 + 0.5) / 256 / n
                    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * gy))))
                    if not (la0 <= lat <= la1):
                        continue
                    for i2 in range(im.width):
                        lng = (x * 256 + i2 + 0.5) / 256 / n * 360 - 180
                        if not (lo0 <= lng <= lo1):
                            continue
                        d = _dbz_of(px[i2, j2])
                        if d is None:
                            continue
                        rainy += 1
                        max_dbz = max(max_dbz, d)
                        if d >= 30:
                            heavy += 1
                        cells.append([round(lat, 3), round(lng, 3), dbz_to_mmh(d)])
        cells.sort(key=lambda c: -c[2])
        return {"time": int(fr["time"]), "max_dbz": max_dbz, "max_mmh": dbz_to_mmh(max_dbz) if max_dbz else 0,
                "heavy_px": heavy, "rain_px": rainy, "cells": cells[:400]}
    except Exception as e:
        log("Không đọc được radar:", repr(e)[:120])
        return None

# ---------------------------------------------------------------- radar Đài KTTV khu vực Nam Bộ (trạm Nhà Bè)
# Ảnh PNG vuông, tâm là trạm ra đa Nhà Bè, bao trọn vòng quét bán kính 300 km.
# Căn theo bản đồ nghiệp vụ của Đài (kttvnb.info): tâm ~10,684°B 106,735°Đ.
KTTV_LIST = "https://kttvnb.info/kttvnb-admin/map/mapajax/radar/list/REAL/2"
KTTV_FILE = "https://kttvnb.info/kttvnb-admin/public/products/"
KTTV_REF = "https://kttvnb.info/kttvnb-admin/index.php?view=radar&city=NAM_BO"
KTTV_CENTER = (10.684, 106.735)
KTTV_RANGE_KM = 300.0
RADAR_PNG = DATA / "radar.png"


def kttv_bounds():
    la, lo = KTTV_CENTER
    dlat = KTTV_RANGE_KM / 111.32
    dlon = KTTV_RANGE_KM / (111.32 * math.cos(math.radians(la)))
    return (la - dlat, la + dlat, lo - dlon, lo + dlon)          # nam, bắc, tây, đông


def kttv_dbz(r, g, b):
    """Quy màu trên ảnh của Đài ra dBZ (thang xanh xám → xanh dương → xanh lá → vàng → cam → đỏ, tối đa ~60)."""
    mx, mn = max(r, g, b), min(r, g, b)
    sat = mx - mn
    if sat < 60:
        return None
    if mx == r: h = (60 * (g - b) / sat) % 360
    elif mx == g: h = 60 * (b - r) / sat + 120
    else: h = 60 * (r - g) / sat + 240
    if h >= 290: return 60                       # tím, hồng: rất mạnh
    if h >= 195: return 18 if sat < 120 else 24  # xanh xám / xanh dương: mưa nhỏ
    if h >= 150: return 30                       # xanh ngọc
    if h >= 95: return 36                        # xanh lá
    if h >= 66: return 41                        # vàng chanh
    if h >= 48: return 45                        # vàng
    if h >= 22: return 49                        # cam
    return 54                                    # đỏ


def kttv_radar():
    """Ảnh radar mới nhất của Đài: lọc nhiễu, lưu data/radar.png (nền trong suốt), trả về số liệu mưa trên TP.HCM."""
    from PIL import ImageFilter
    if MOCK:
        return None
    try:
        h = {"User-Agent": UA, "Referer": KTTV_REF, "X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/javascript, */*"}
        j = requests.get(KTTV_LIST, headers=h, timeout=20).json()
        files = [f for f in (j.get("files") or []) if isinstance(f, str) and f.endswith(".png")]
        if not files:
            raise ValueError("danh sách ảnh trống")
        f = files[-1]
        y, mth, d, hm = f.split("/")[-4:]
        hh, mm = hm[:-4].split("_")
        t = int(dt.datetime(int(y), int(mth), int(d), int(hh), int(mm), tzinfo=dt.timezone.utc).timestamp())
        r = requests.get(KTTV_FILE + f, headers={"User-Agent": UA, "Referer": KTTV_REF}, timeout=25)
        r.raise_for_status()
        im = Image.open(io.BytesIO(r.content)).convert("RGB")
        W, H = im.size
        px = im.load()
        # mặt nạ điểm có màu (bỏ nền xám và vệt xám nhạt)
        mask = Image.new("L", (W, H), 0); mp = mask.load()
        for yy in range(H):
            for xx in range(W):
                rr, gg, bb = px[xx, yy]
                if max(rr, gg, bb) - min(rr, gg, bb) >= 60:
                    mp[xx, yy] = 255
        k = max(3, round(W / 130) | 1)               # ~5 điểm ảnh với ảnh 660
        opened = mask.filter(ImageFilter.MinFilter(k)).filter(ImageFilter.MaxFilter(k + 2))
        op = opened.load()
        out = Image.new("RGBA", (W, H), (0, 0, 0, 0)); o = out.load()
        S, N, Wl, E = kttv_bounds()
        la0, la1, lo0, lo1 = RADAR_BBOX
        cx, cy, rc = W / 2, H / 2, 0.045 * W         # bỏ vòng nhiễu sát trạm
        cells, max_dbz, heavy, rainy = [], 0, 0, 0
        for yy in range(H):
            lat = N - (yy + 0.5) / H * (N - S)
            for xx in range(W):
                if not (mp[xx, yy] and op[xx, yy]):
                    continue
                if (xx - cx) ** 2 + (yy - cy) ** 2 < rc * rc:
                    continue
                rr, gg, bb = px[xx, yy]
                o[xx, yy] = (rr, gg, bb, 230)
                lng = Wl + (xx + 0.5) / W * (E - Wl)
                if la0 <= lat <= la1 and lo0 <= lng <= lo1:
                    dbz = kttv_dbz(rr, gg, bb)
                    if dbz is None:
                        continue
                    rainy += 1; max_dbz = max(max_dbz, dbz)
                    if dbz >= 30: heavy += 1
                    if (xx + yy) % 2 == 0:
                        cells.append([round(lat, 3), round(lng, 3), dbz_to_mmh(dbz)])
        out.resize((W // 2, H // 2), Image.LANCZOS).save(RADAR_PNG, optimize=True)   # nửa kích thước cho kho gọn (~1,8 km/điểm ảnh)
        cells.sort(key=lambda c: -c[2])
        # mỗi điểm ảnh ~0,9 km; quy về ô ~1,2 km cho cùng ngưỡng với RainViewer
        scale = (2 * KTTV_RANGE_KM / W) ** 2 / 1.44
        return {"src": "kttvnb", "time": t, "file": f, "img": "radar.png", "bounds": [[round(Wl, 4), round(N, 4)], [round(E, 4), round(N, 4)], [round(E, 4), round(S, 4)], [round(Wl, 4), round(S, 4)]],
                "max_dbz": max_dbz, "max_mmh": dbz_to_mmh(max_dbz) if max_dbz else 0,
                "heavy_px": round(heavy * scale), "rain_px": round(rainy * scale), "cells": cells[:400]}
    except Exception as e:
        log("Không lấy được radar của Đài KTTV Nam Bộ:", repr(e)[:140])
        return None


# ---------------------------------------------------------------- báo cáo của người đi đường
def fetch_reports(state, ts):
    """Lấy báo cáo mới từ hàng chờ ntfy.sh. Trả về danh sách báo cáo hợp lệ chưa từng thấy."""
    known = state.setdefault("reports", {})
    raw = []
    if MOCK:
        p = os.environ.get("MOCK_REPORTS")
        if p:
            for x in load_json(p, []):
                raw.append({"time": x.get("time", ts - 120), "message": json.dumps(x)})
    else:
        since = state.get("ntfy_since")
        since = str(int(since)) if since else "3h"
        try:
            r = requests.get(f"https://ntfy.sh/{REPORT_TOPIC}/json", params={"poll": "1", "since": since},
                             headers={"User-Agent": UA}, timeout=20)
            r.raise_for_status()
            for line in r.text.splitlines():
                try:
                    m = json.loads(line)
                except Exception:
                    continue
                if m.get("event") == "message":
                    raw.append(m)
            state["ntfy_since"] = ts - 120           # chồng lấn 2 phút để không sót, trùng thì bỏ theo id
        except Exception as e:
            log("Không lấy được báo cáo của người đi đường:", repr(e)[:120])
            return []
    new = []
    for m in raw:
        try:
            x = json.loads(m.get("message") or "")
            rid = str(x["id"])[:40]
            if not rid.replace("-", "").isalnum() or rid in known:
                continue
            if x.get("kind") == "dispute":
                # người dùng bấm "Không ngập" trên một điểm AI báo
                at = int(m.get("time") or ts)
                tgt = str(x.get("cam") or "")[:40] or ("rp:" + str(x.get("rid") or "")[:40])
                if ts - at <= 60 * 60 and len(tgt) > 3:
                    dis = state.setdefault("disputes", {}).setdefault(tgt, [])
                    dev = str(x.get("dev", ""))[:40] or rid
                    if all(d[1] != dev for d in dis):
                        dis.append([at, dev])
                    known[rid] = {"id": rid, "kind": "dispute", "at": at, "status": "dispute", "lat": 0, "lng": 0}
                continue
            lat, lng, level = float(x["lat"]), float(x["lng"]), int(x["level"])
            if not (10.3 <= lat <= 11.2 and 106.3 <= lng <= 107.1) or level not in (1, 2, 3, 4):
                continue
            at = int(m.get("time") or ts)            # giờ máy chủ nhận, không tin giờ trên máy người gửi
            if ts - at > 60 * 60:
                continue
            new.append({"id": rid, "lat": round(lat, 6), "lng": round(lng, 6), "level": level, "at": at,
                        "street": str(x.get("street", ""))[:80], "note": str(x.get("note", ""))[:120],
                        "dev": str(x.get("dev", ""))[:40] or rid, "status": "checking", "dry": 0})
        except Exception:
            continue
        if len(new) >= MAX_NEW_REPORTS:
            break
    for r in new:
        known[r["id"]] = r
    return new


def cams_near(cams, lat, lng, radius):
    out = []
    for c in cams:
        if abs(c["lat"] - lat) < 0.006 and abs(c["lng"] - lng) < 0.006:
            d = dist_m(lat, lng, c["lat"], c["lng"])
            if d <= radius:
                out.append((d, c))
    out.sort(key=lambda x: x[0])
    return out[:VERIFY_MAX_CAMS]


def disputed(state, key, ts):
    """Số người khác nhau bấm "Không ngập" cho camera / báo cáo này trong 60 phút qua."""
    return len([d for d in state.get("disputes", {}).get(key, []) if ts - d[0] <= 60 * 60])


def verify_reports(state, cams, fresh, ts, wet_recent):
    """Cập nhật trạng thái báo cáo theo kết quả AI vừa xem (fresh: cam_id -> kết quả lượt này)."""
    reps = state.get("reports", {})
    for r in reps.values():
        if r.get("kind") == "dispute":
            continue
        st = r["status"]
        if st in ("checking", "confirmed"):
            near = [(d, c) for d, c in cams_near(cams, r["lat"], r["lng"], VERIFY_RADIUS_M)]
            if not near and st == "checking":
                r["status"] = "no_camera"; continue
            seen = [(d, c, fresh[c["id"]]) for d, c in near if c["id"] in fresh and fresh[c["id"]]["usable"]]
            wet = [x for x in seen if x[2]["level"] >= 1 and x[2]["conf"] >= (0.85 if disputed(state, x[1]["id"], ts) else 0.6)]
            if wet:
                d, c, res = min(wet, key=lambda x: x[0])
                if st == "checking":
                    r["since"] = ts
                r.update(status="confirmed", cam=c["id"], cam_name=c["name"], cam_dist=round(d),
                         level_cam=max(x[2]["level"] for x in wet), conf=res["conf"], cam_note=res["note"], last_pos=ts, dry=0)
            elif seen:
                r["dry"] = r.get("dry", 0) + 1
                d, c, _ = seen[0]
                r.update(cam=c["id"], cam_name=c["name"], cam_dist=round(d), checked=ts)
                if st == "checking" and r["dry"] >= 2:
                    r["status"] = "not_seen"          # camera 2 lượt liền thấy đường khô
                elif st == "confirmed" and all(x[2]["level"] == 0 and x[2]["conf"] >= 0.6 for x in seen):
                    r["status"] = "cleared"           # camera thấy nước đã rút
            if r["status"] == "checking" and ts - r["at"] > CHECK_FOR:
                r["status"] = "unverifiable"          # không lấy được ảnh camera
            if r["status"] == "confirmed" and ts - r.get("last_pos", ts) > REPORT_KEEP:
                r["status"] = "expired"
    # chỗ không có camera: nhiều máy khác nhau cùng báo, lúc trời vừa mưa hoặc triều cao
    pool = [r for r in reps.values() if r["status"] in ("no_camera", "unverifiable", "crowd") and ts - r["at"] <= 60 * 60
            and disputed(state, "rp:" + r["id"], ts) < 2]
    for r in pool:
        group = [o for o in pool if abs(o["at"] - r["at"]) <= 45 * 60
                 and dist_m(r["lat"], r["lng"], o["lat"], o["lng"]) <= CROWD_RADIUS_M]
        devs = {o["dev"] for o in group}
        if len(devs) >= CROWD_MIN_DEVICES and wet_recent:
            lv = sorted(o["level"] for o in group)[len(group) // 2]
            r.update(status="crowd", crowd=len(devs), level_crowd=min(lv, 3), last_pos=max(o["at"] for o in group))
        elif r["status"] == "crowd":
            r["status"] = "expired"
    for r in reps.values():
        if r["status"] == "crowd" and ts - r.get("last_pos", r["at"]) > 60 * 60:
            r["status"] = "expired"
    # dọn báo cáo cũ hơn 6 giờ
    for k in [k for k, r in reps.items() if ts - r["at"] > 6 * 3600]:
        del reps[k]


# ---------------------------------------------------------------- kiểm chứng mưa bằng camera
RAIN_CHECK_CAMS = 12


def pick_rain_check_cams(cams, rd, hot, ts):
    """Chọn ~12 camera nằm ngay trong vùng radar báo mưa (hoặc rải đều nếu chỉ mô hình báo)."""
    chosen, ids = [], set()
    if rd:
        for lat, lng, mmh in rd.get("cells", []):
            if mmh < 2.7 or len(chosen) >= RAIN_CHECK_CAMS:
                continue
            best = None
            for c in cams:
                if c["id"] in ids or abs(c["lat"] - lat) > 0.015 or abs(c["lng"] - lng) > 0.015:
                    continue
                d = dist_m(lat, lng, c["lat"], c["lng"])
                if d <= 1500 and (best is None or d < best[0]):
                    best = (d, c)
            if best:
                chosen.append(best[1]); ids.add(best[1]["id"])
    if len(chosen) < RAIN_CHECK_CAMS // 2:
        # rải đều khắp thành phố: mỗi ô ~5 km lấy một camera, ưu tiên camera ở điểm hay ngập
        cells = {}
        for c in sorted(cams, key=lambda c: (c["id"] not in hot, hashlib.md5((c["id"] + str(ts // 3600)).encode()).hexdigest())):
            k = (round(c["lat"] / 0.05), round(c["lng"] / 0.05))
            cells.setdefault(k, c)
        for c in cells.values():
            if len(chosen) >= RAIN_CHECK_CAMS:
                break
            if c["id"] not in ids:
                chosen.append(c); ids.add(c["id"])
    return chosen


def rain_check(check_cams):
    """Xem ảnh các camera được chọn. Trả về (kết quả từng camera, token vào, token ra, số camera thấy mưa, số ảnh dùng được)."""
    imgs = []
    with cf.ThreadPoolExecutor(max_workers=12) as pool:
        for c, (data, info) in zip(check_cams, pool.map(fetch_image, check_cams)):
            if data is not None:
                imgs.append((c, data))
    res, tin, tout = {}, 0, 0
    for i in range(0, len(imgs), BATCH):
        try:
            r, (a, b) = classify(imgs[i:i + BATCH])
            res.update(r); tin += a; tout += b
        except Exception as e:
            log("Lỗi gọi AI khi kiểm chứng mưa:", repr(e)[:120])
    usable = [r for r in res.values() if r["usable"]]
    return res, tin, tout, sum(1 for r in usable if r["rain"]), len(usable)


# ---------------------------------------------------------------- AI
_client = None


def client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(max_retries=4, timeout=90)
    return _client


def classify(batch):
    """batch: list of (cam, jpeg). Trả về dict cam_id -> kết quả, và (input_tokens, output_tokens)."""
    if MOCK:
        return mock_classify(batch)
    content = []
    for i, (cam, jpeg) in enumerate(batch, 1):
        content.append({"type": "text", "text": f"Camera {i}: {cam['name']}"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                     "data": base64.b64encode(jpeg).decode()}})
    content.append({"type": "text", "text": PROMPT})
    msg = client().messages.create(
        model=MODEL, max_tokens=1200, tools=[TOOL],
        tool_choice={"type": "tool", "name": "report_cameras"},
        messages=[{"role": "user", "content": content}])
    results = {}
    for block in msg.content:
        if getattr(block, "type", "") == "tool_use":
            for r in (block.input or {}).get("results", []):
                k = r.get("camera")
                if isinstance(k, int) and 1 <= k <= len(batch):
                    results[batch[k - 1][0]["id"]] = sanitize(r)
    return results, (msg.usage.input_tokens, msg.usage.output_tokens)


def sanitize(r):
    lvl = r.get("level", 0)
    lvl = int(lvl) if isinstance(lvl, (int, float)) else 0
    conf = r.get("confidence", 0)
    conf = float(conf) if isinstance(conf, (int, float)) else 0.0
    return {"usable": bool(r.get("usable", False)), "level": max(0, min(4, lvl)), "rain": bool(r.get("raining", False)),
            "conf": round(max(0.0, min(1.0, conf)), 2), "note": str(r.get("note", ""))[:120]}


# ---------------------------------------------------------------- giả lập để chạy thử
def mock_image(cam):
    h = int(hashlib.md5(cam["id"].encode()).hexdigest(), 16)
    if h % 17 == 0:
        return None, "HTTP 503 (giả lập)"
    im = Image.new("RGB", (1280, 720), ((h >> 8) % 255, (h >> 16) % 255, 120))
    buf = io.BytesIO(); im.save(buf, "JPEG", quality=70)
    return buf.getvalue(), hashlib.sha1(buf.getvalue()).hexdigest()


def mock_classify(batch):
    res = {}
    for cam, _ in batch:
        h = int(hashlib.md5((cam["id"] + os.environ.get("MOCK_SEED", "")).encode()).hexdigest(), 16)
        lvl = [0] * 20 + [1, 1, 2, 2, 3]
        level = lvl[h % len(lvl)]
        res[cam["id"]] = {"usable": h % 23 != 0, "level": level, "conf": 0.6 + (h % 40) / 100,
                          "rain": (h % 100) < float(os.environ.get("MOCK_CAM_RAIN", "0.5")) * 100,
                          "note": "Nước ngập nửa bánh xe máy" if level >= 2 else ("Có nước trên đường" if level else "Đường khô")}
    return res, (len(batch) * 320 + 450, len(batch) * 40)


# ---------------------------------------------------------------- tổng hợp
def decide(cam_state, ts, wet=True, strict=False):
    """Từ lịch sử quét của một camera, quyết định có công bố ngập không.

    Công bố khi: AI rất chắc (từ 0,85), hoặc thấy ngập ở 2 lần xem liên tiếp và cả hai lần đều khá chắc
    (mắt cá chân cần từ 0,7; nửa bánh trở lên cần từ 0,6). Nếu 3 giờ qua không mưa và triều không cao
    (wet=False) thì mọi mức đều cần độ chắc từ 0,85, vì trời khô mà thấy "ngập" thường là ảnh phản chiếu.
    """
    hist = [h for h in cam_state.get("hist", []) if ts - h[0] <= EXPIRE_AFTER]
    if not hist:
        return None
    t, level, conf, note = hist[-1]
    need = 0.85 if (not wet or strict) else (0.7 if level == 1 else 0.6)
    if level < 1 or conf < min(need, 0.6):
        return None
    prev = [h for h in hist[:-1] if t - h[0] <= CONFIRM_WINDOW]
    prev_ok = prev and prev[-1][1] >= 1 and prev[-1][2] >= (0.85 if (not wet or strict) else (0.7 if prev[-1][1] == 1 else 0.6))
    if conf >= 0.85:
        shown = level
    elif conf >= need and prev_ok:
        shown = min(level, prev[-1][1] + 1)    # không nhảy quá 1 mức giữa hai lần quét
    else:
        return {"level": level, "conf": conf, "note": note, "confirmed": False, "seen": t}
    first = t
    for h in reversed(hist):
        if h[1] >= 1:
            first = h[0]
        else:
            break
    return {"level": shown, "conf": conf, "note": note, "confirmed": True, "seen": t, "since": first,
            "times": sum(1 for h in hist if h[1] >= 1)}


def main():
    ts = now_ts()
    state = load_json(STATE_FILE, {"cams": {}, "spend": {}, "last_wet": 0, "last_dry_scan": 0})
    state.setdefault("cams", {}); state.setdefault("reports", {})
    today = dt.datetime.now(VN).strftime("%Y-%m-%d")
    spent = state.setdefault("spend", {}).get(today, 0.0)
    budget = float(os.environ.get("DAILY_BUDGET_USD") or 1.5)
    scan_all = os.environ.get("SCAN_ALL", "").lower() in ("1", "true", "yes")

    cams = load_cameras()
    spots = load_json(SPOTS_FILE, [])
    hot, tide_cams = set(), set()
    for c in cams:
        for s in spots:
            if abs(c["lat"] - s["lat"]) < 0.01 and abs(c["lng"] - s["lng"]) < 0.01 and dist_m(c["lat"], c["lng"], s["lat"], s["lng"]) <= HOT_RADIUS_M:
                hot.add(c["id"])
                if s.get("c") == 2:
                    tide_cams.add(c["id"])

    w = weather()
    rd = kttv_radar() or radar()                  # ưu tiên radar của Đài KTTV Nam Bộ, dự phòng RainViewer
    if rd:
        w["radar"] = rd
        log(f"Radar ({rd.get('src', 'rainviewer')}) lúc {iso(rd['time'])}: mưa ở {rd['rain_px']} ô ~1,2 km trên TP.HCM, mạnh nhất {rd['max_mmh']} mm/giờ")
    new_reports = fetch_reports(state, ts)
    if new_reports:
        log(f"Nhận {len(new_reports)} báo cáo mới của người đi đường")
    # Radar (RainViewer) ở TP.HCM hay báo nhiễu, nhất là ban đêm (sóng dội vào nhà cửa quanh trạm Nhà Bè),
    # còn mô hình dự báo hay lệch chỗ, lệch giờ. Vì vậy radar hay mô hình báo mưa chỉ là gợi ý: bộ quét xem
    # ~12 camera ngay trong vùng được báo, AI thấy mưa thật ở ít nhất 3 camera (hoặc 1/3 số ảnh xem được) mới coi là đang mưa.
    radar_says = bool(rd and rd["heavy_px"] >= 3)
    model_says = (w["rain_1h"] or 0) >= 2
    pre, pre_tokens, check = {}, (0, 0), None
    raining = False
    if (radar_says or model_says) and not scan_all and (spent < budget or MOCK):
        chk = pick_rain_check_cams(cams, rd if radar_says else None, hot, ts)
        pre, tin0, tout0, n_rain, n_ok = rain_check(chk)
        pre_tokens = (tin0, tout0)
        if n_ok >= 4:
            raining = n_rain >= max(3, math.ceil(n_ok / 3))
        else:
            raining = radar_says and model_says        # camera không xem được: chỉ tin khi cả hai nguồn cùng báo
        check = {"cams": len(chk), "usable": n_ok, "raining": n_rain, "confirmed": raining,
                 "trigger": "radar" + ("+model" if model_says else "") if radar_says else "model"}
        log(f"Kiểm chứng mưa ({check['trigger']}): {n_rain}/{n_ok} camera thấy đang mưa -> {'ĐANG MƯA' if raining else 'không mưa, bỏ qua'}")
    w["rain_check"] = check
    if rd:
        rd["confirmed"] = bool(check and check["confirmed"] and radar_says)
    tide_high = (w["tide"] or 0) >= 1.45
    if raining:
        state["last_rain"] = ts
    if raining or tide_high:
        state["last_wet"] = ts
    recently_rained = ts - state.get("last_rain", 0) <= 2 * 3600     # nước còn đọng 2 giờ sau mưa
    full_due = ts - state.get("last_full_scan", 0) >= 19 * 60

    if scan_all or raining:
        mode, targets = ("all" if scan_all else "rain"), cams
        state["last_full_scan"] = ts
    elif recently_rained and full_due:
        # tạnh mưa nhưng nước còn đọng: 20 phút xem lại toàn bộ một lần
        mode, targets = "after-rain", cams
        state["last_full_scan"] = ts
    elif tide_high:
        # triều cao mà không mưa: chỉ ngập ở vùng trũng ven sông, xem camera gần các điểm hay ngập do triều
        mode, targets = "tide", [c for c in cams if c["id"] in tide_cams]
    elif not recently_rained and ts - state.get("last_dry_scan", 0) >= float(os.environ.get("DRY_SCAN_HOURS") or 3) * 3600 - 120:
        mode, targets = "dry-hotspots", [c for c in cams if c["id"] in hot]
        state["last_dry_scan"] = ts
    else:
        mode, targets = "idle", []
    # camera cần xem để kiểm chứng báo cáo của người đi đường, và camera vừa bị báo "Không ngập"
    verify = {k for k in state.get("disputes", {}) if not k.startswith("rp:") and disputed(state, k, ts)}
    for r in state.get("reports", {}).values():
        if r["status"] in ("checking", "confirmed"):
            for _, c in cams_near(cams, r["lat"], r["lng"], VERIFY_RADIUS_M):
                verify.add(c["id"])
    if verify:
        have = {c["id"] for c in targets}
        extra = [c for c in cams if c["id"] in verify and c["id"] not in have]
        if extra and mode == "idle":
            mode = "reports"
        targets = list(targets) + extra
    # ưu tiên camera kiểm chứng báo cáo, rồi camera gần điểm hay ngập
    prio = lambda cid: 0 if cid in verify else (1 if cid in hot else 2)
    targets = sorted(targets, key=lambda c: prio(c["id"]))
    log(f"Chế độ {mode}: radar {'mạnh nhất ' + str(rd['max_mmh']) + ' mm/giờ' if rd else 'lỗi'}, mô hình mưa 1 giờ {w['rain_1h']} mm, triều {w['tide']} m, sẽ xem {len(targets)} camera. Đã tiêu hôm nay ${spent:.3f}/{budget}")

    stats = {"targets": len(targets), "verify_cams": len(verify), "reports_new": len(new_reports), "images": 0, "failed": 0, "frozen": 0, "placeholder": 0, "classified": 0,
             "unusable": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "stopped": ""}

    if pre:
        stats["tokens_in"] += pre_tokens[0]; stats["tokens_out"] += pre_tokens[1]
        stats["cost_usd"] = round(stats["tokens_in"] / 1e6 * PRICE_IN + stats["tokens_out"] / 1e6 * PRICE_OUT, 5)
        stats["rain_check"] = len(pre)
        targets = [c for c in targets if c["id"] not in pre]
    if targets and spent >= budget and not MOCK:
        stats["stopped"] = "budget"
        targets = []
        log("Đã chạm trần chi phí trong ngày, không quét.")

    # 1. tải ảnh
    images = []
    if targets:
        with cf.ThreadPoolExecutor(max_workers=16) as pool:
            futs = {pool.submit(fetch_image, c): c for c in targets}
            for f in cf.as_completed(futs):
                c = futs[f]
                data, info = f.result()
                cs = state["cams"].setdefault(c["id"], {})
                if data is None:
                    stats["failed"] += 1
                    cs["err"] = info
                    continue
                if cs.get("hash") == info and ts - cs.get("hash_at", ts) > 15 * 60:
                    stats["frozen"] += 1        # ảnh không đổi quá 15 phút: camera treo
                    continue
                if cs.get("hash") != info:
                    cs["hash"], cs["hash_at"] = info, ts
                cs.pop("err", None)
                images.append((c, data, info))
        stats["images"] = len(images)
        # ảnh giống hệt nhau ở nhiều camera là ảnh "mất tín hiệu"
        count = {}
        for _, _, hsh in images:
            count[hsh] = count.get(hsh, 0) + 1
        before = len(images)
        images = [x for x in images if count[x[2]] < 3]
        stats["placeholder"] = before - len(images)
        images.sort(key=lambda x: prio(x[0]["id"]))
        log(f"Tải được {before} ảnh, lỗi {stats['failed']}, treo {stats['frozen']}, ảnh mất tín hiệu {stats['placeholder']}")

    # 2. gửi AI theo lô
    batches = [images[i:i + BATCH] for i in range(0, len(images), BATCH)]

    def run(b):
        return classify([(c, d) for c, d, _ in b])

    fresh = {}
    for cid, r in pre.items():                     # kết quả của lượt kiểm chứng mưa cũng tính vào
        fresh[cid] = r
        stats["classified"] += 1
        if r["usable"]:
            h = state["cams"].setdefault(cid, {}).setdefault("hist", [])
            h.append([ts, r["level"], r["conf"], r["note"]]); del h[:-4]
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        pending = {}
        it = iter(batches)
        while True:
            while len(pending) < 4:
                if time.time() - START > TIME_LIMIT:
                    stats["stopped"] = stats["stopped"] or "time"; break
                if spent + stats["cost_usd"] >= budget and not MOCK:
                    stats["stopped"] = "budget"; break
                b = next(it, None)
                if b is None:
                    break
                pending[pool.submit(run, b)] = b
            if not pending:
                break
            done, _ = cf.wait(pending, return_when=cf.FIRST_COMPLETED)
            for f in done:
                b = pending.pop(f)
                try:
                    res, (tin, tout) = f.result()
                except Exception as e:
                    log("Lỗi gọi AI:", repr(e)[:160]); continue
                stats["tokens_in"] += tin; stats["tokens_out"] += tout
                stats["cost_usd"] = round(stats["tokens_in"] / 1e6 * PRICE_IN + stats["tokens_out"] / 1e6 * PRICE_OUT, 5)
                for c, _, _ in b:
                    r = res.get(c["id"])
                    if not r:
                        continue
                    stats["classified"] += 1
                    fresh[c["id"]] = r
                    cs = state["cams"].setdefault(c["id"], {})
                    if not r["usable"]:
                        stats["unusable"] += 1; continue
                    hist = cs.setdefault("hist", [])
                    hist.append([ts, r["level"], r["conf"], r["note"]])
                    del hist[:-4]
    if stats["cost_usd"]:
        state["spend"][today] = round(spent + stats["cost_usd"], 5)
    # chỉ giữ chi phí 14 ngày gần nhất
    state["spend"] = dict(sorted(state["spend"].items())[-14:])

    # camera tự thấy mưa ở nhiều nơi (dù radar, mô hình không báo): coi như vừa mưa, các lượt sau xem toàn bộ camera
    seen_ok = [r for r in fresh.values() if r["usable"]]
    seen_rain = sum(1 for r in seen_ok if r.get("rain"))
    if not raining and seen_ok and seen_rain >= max(5, math.ceil(len(seen_ok) / 3)):
        state["last_rain"] = state["last_wet"] = ts
        state["last_full_scan"] = 0
        log(f"Camera thấy đang mưa ở {seen_rain}/{len(seen_ok)} chỗ: lượt sau sẽ xem toàn bộ camera")

    # dọn phản hồi "Không ngập" cũ hơn 1 giờ
    state["disputes"] = {k: [d for d in v if ts - d[0] <= 60 * 60] for k, v in state.get("disputes", {}).items()}
    state["disputes"] = {k: v for k, v in state["disputes"].items() if v}

    # 3. kiểm chứng báo cáo của người đi đường
    verify_reports(state, cams, fresh, ts, ts - state.get("last_wet", 0) <= 3 * 3600)

    # 4. tổng hợp kết quả
    wet_ctx = ts - state.get("last_rain", 0) <= 3 * 3600 or tide_high
    by_id = {c["id"]: c for c in cams}
    reps = state.get("reports", {})
    backed = {r["cam"] for r in reps.values() if r["status"] == "confirmed" and r.get("last_pos") == ts}
    detections = []
    for cid, cs in state["cams"].items():
        d = decide(cs, ts, wet_ctx, strict=disputed(state, cid, ts) > 0)
        if not d or cid not in by_id:
            continue
        if cid in backed and not d["confirmed"]:
            # người đi đường báo và camera cũng thấy nước: coi là đã xác nhận
            d.update(confirmed=True, since=d["seen"], times=1)
        c = by_id[cid]
        detections.append({"cam": cid, "name": c["name"], "lat": round(c["lat"], 6), "lng": round(c["lng"], 6), **d})
    placed = []                              # nhiều người báo cùng một chỗ thì chỉ hiện một điểm
    for r in sorted(reps.values(), key=lambda r: (r["status"] != "confirmed", -r.get("level_cam", r.get("level_crowd", 0)), r["at"])):
        if r["status"] not in ("confirmed", "crowd"):
            continue
        if any(dist_m(r["lat"], r["lng"], a, b) <= 100 for a, b in placed):
            continue
        placed.append((r["lat"], r["lng"]))
        if r["status"] == "confirmed":
            detections.append({"src": "report", "rid": r["id"], "cam": r["cam"], "cam_name": r["cam_name"], "cam_dist": r["cam_dist"],
                               "name": r["street"] or r["cam_name"], "lat": r["lat"], "lng": r["lng"],
                               "level": r["level_cam"], "reported_level": r["level"], "conf": r["conf"],
                               "note": r["cam_note"], "confirmed": True, "seen": r["last_pos"], "since": r["at"], "times": 1})
        elif r["status"] == "crowd":
            detections.append({"src": "crowd", "rid": r["id"], "name": r["street"] or "Người đi đường báo", "lat": r["lat"], "lng": r["lng"],
                               "level": r["level_crowd"], "reported_level": r["level"], "conf": 0.5, "crowd": r["crowd"],
                               "note": f"{r['crowd']} người cùng báo ngập quanh đây", "confirmed": True,
                               "seen": r["last_pos"], "since": r["at"], "times": 1})
    detections.sort(key=lambda x: (-x["confirmed"], -x["level"], -x["conf"]))
    # dọn lịch sử quá cũ
    for cid in list(state["cams"]):
        cs = state["cams"][cid]
        cs["hist"] = [h for h in cs.get("hist", []) if ts - h[0] <= 6 * 3600]
        if not cs["hist"] and "err" not in cs and ts - cs.get("hash_at", 0) > 6 * 3600:
            del state["cams"][cid]

    out = {
        "updated": iso(ts), "updated_ts": ts, "mode": mode, "model": MODEL,
        "weather": w, "stats": stats, "cameras_total": len(cams),
        "spend_today_usd": state["spend"].get(today, 0.0), "budget_usd": budget,
        "detections": detections,
        # camera AI vừa xem lượt này và thấy đường khô: bản đồ dùng để bỏ ước tính ngập ở gần đó
        "disputed": sorted(k for k in state.get("disputes", {}) if disputed(state, k, ts)),
        "raining_cams": sorted(cid for cid, r in fresh.items() if r["usable"] and r.get("rain")),
        "dry": sorted(cid for cid, r in fresh.items() if r["usable"] and r["level"] == 0 and r["conf"] >= 0.6),
        "reports": [{k: r[k] for k in ("id", "status", "cam_name", "cam_dist", "level_cam", "crowd", "at") if k in r}
                    for r in sorted(reps.values(), key=lambda r: -r["at"]) if ts - r["at"] <= 3 * 3600][:300],
    }
    save_json(OUT_FILE, out)
    save_json(STATE_FILE, state)
    conf = sum(1 for d in detections if d["confirmed"])
    rs = {}
    for r in reps.values():
        rs[r["status"]] = rs.get(r["status"], 0) + 1
    log(f"Xong: AI xem {stats['classified']} ảnh, {conf} chỗ ngập đã xác nhận, {len(detections) - conf} chỗ chờ xác nhận. "
        f"Chi phí lần này ${stats['cost_usd']:.4f}")
    if rs:
        log("Báo cáo của người đi đường:", ", ".join(f"{k} {v}" for k, v in sorted(rs.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
