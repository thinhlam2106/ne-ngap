"""
Né Ngập – AI xem camera giao thông TP.HCM để phát hiện ngập.

Mỗi lần chạy (GitHub Actions gọi 10 phút/lần):
  1. Xem trời có mưa hoặc triều cao không (Open-Meteo). Đang mưa hoặc triều cao thì
     xem tất cả camera mỗi lần chạy; trong 2 giờ sau mưa thì 20 phút một lần;
     trời khô thì chỉ xem camera gần các điểm hay ngập, 3 giờ một lần.
  2. Tải ảnh chụp mới của từng camera (hệ thống camera TP.HCM do Notis vận hành).
  3. Gửi ảnh theo lô cho Claude Haiku, nhận lại mức ngập theo bánh xe máy.
  4. Chỉ công bố một chỗ ngập khi AI chắc chắn, hoặc thấy ngập ở 2 lần quét liên tiếp.
  5. Ghi kết quả ra data/flood-ai.json để bản đồ đọc.

Biến môi trường:
  ANTHROPIC_API_KEY   khoá API (bắt buộc, trừ khi chạy giả lập)
  SCAN_ALL=true       quét tất cả camera ngay, bỏ qua kiểm tra mưa
  DAILY_BUDGET_USD    trần chi phí mỗi ngày, mặc định 1.5
  DRY_SCAN_HOURS      trời khô thì bao nhiêu giờ xem camera điểm hay ngập một lần, mặc định 3
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

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 NeNgap/1.0"
HEADERS = {"User-Agent": UA, "Referer": "https://giaothong.hochiminhcity.gov.vn/"}

PROMPT = """You are checking snapshots from Ho Chi Minh City traffic cameras for street flooding.
Each image is preceded by its camera number. For EVERY camera, report:
- usable: false if the image is black, frozen, a "no signal"/placeholder graphic, or too dark/blurry to see the road surface. Otherwise true.
- level (integer 0-4), judged by water depth on the roadway, using vehicles, wheels, curbs and people's legs as a ruler:
  0 = no standing water. A wet, shiny or reflective road after rain is still 0. Small puddles at the edge that do not cover a travel lane are 0.
  1 = shallow water covering part of a travel lane, below ankle height, under about 10 cm.
  2 = water around half a motorbike wheel, about 15-25 cm.
  3 = water up to a motorbike's exhaust or engine, about 30-40 cm.
  4 = water at a motorbike seat or higher, over 50 cm.
  Only give level 1 or more if you can actually see a water surface on the road (ripples, wakes or spray from moving vehicles, submerged curbs or wheels). When unsure between two levels, choose the lower one.
- confidence: 0 to 1, how sure you are of the level.
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
                        "note": {"type": "string"},
                    },
                    "required": ["camera", "usable", "level", "confidence", "note"],
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
        return {"rain_1h": float(os.environ.get("MOCK_RAIN", "12")), "tide": 1.2}
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
    return {"usable": bool(r.get("usable", False)), "level": max(0, min(4, lvl)),
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
                          "note": "Nước ngập nửa bánh xe máy" if level >= 2 else ("Có nước trên đường" if level else "Đường khô")}
    return res, (len(batch) * 320 + 450, len(batch) * 40)


# ---------------------------------------------------------------- tổng hợp
def decide(cam_state, ts):
    """Từ lịch sử quét của một camera, quyết định có công bố ngập không."""
    hist = [h for h in cam_state.get("hist", []) if ts - h[0] <= EXPIRE_AFTER]
    if not hist:
        return None
    t, level, conf, note = hist[-1]
    if level < 1 or conf < 0.55:
        return None
    prev = [h for h in hist[:-1] if t - h[0] <= CONFIRM_WINDOW]
    prev_flood = prev and prev[-1][1] >= 1 and prev[-1][2] >= 0.5
    if conf >= 0.85:
        shown = level
    elif prev_flood:
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
    today = dt.datetime.now(VN).strftime("%Y-%m-%d")
    spent = state.setdefault("spend", {}).get(today, 0.0)
    budget = float(os.environ.get("DAILY_BUDGET_USD") or 1.5)
    scan_all = os.environ.get("SCAN_ALL", "").lower() in ("1", "true", "yes")

    cams = load_cameras()
    spots = load_json(SPOTS_FILE, [])
    hot = set()
    for c in cams:
        for s in spots:
            if abs(c["lat"] - s["lat"]) < 0.01 and abs(c["lng"] - s["lng"]) < 0.01 and dist_m(c["lat"], c["lng"], s["lat"], s["lng"]) <= HOT_RADIUS_M:
                hot.add(c["id"]); break

    w = weather()
    wet_now = (w["rain_1h"] or 0) >= 2 or (w["tide"] or 0) >= 1.45
    if wet_now:
        state["last_wet"] = ts
    recently_wet = ts - state.get("last_wet", 0) <= 2 * 3600       # nước còn đọng 2 giờ sau mưa

    if scan_all or wet_now:
        mode, targets = ("all" if scan_all else "rain"), cams
        state["last_full_scan"] = ts
    elif recently_wet:
        # tạnh mưa nhưng nước còn đọng: 20 phút xem lại một lần
        if ts - state.get("last_full_scan", 0) >= 19 * 60:
            mode, targets = "after-rain", cams
            state["last_full_scan"] = ts
        else:
            mode, targets = "idle", []
    elif ts - state.get("last_dry_scan", 0) >= float(os.environ.get("DRY_SCAN_HOURS") or 3) * 3600 - 120:
        mode, targets = "dry-hotspots", [c for c in cams if c["id"] in hot]
        state["last_dry_scan"] = ts
    else:
        mode, targets = "idle", []
    # ưu tiên camera gần điểm hay ngập
    targets = sorted(targets, key=lambda c: 0 if c["id"] in hot else 1)
    log(f"Chế độ {mode}: mưa 1 giờ {w['rain_1h']} mm, triều {w['tide']} m, sẽ xem {len(targets)} camera. Đã tiêu hôm nay ${spent:.3f}/{budget}")

    stats = {"targets": len(targets), "images": 0, "failed": 0, "frozen": 0, "placeholder": 0, "classified": 0,
             "unusable": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "stopped": ""}

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
        images.sort(key=lambda x: 0 if x[0]["id"] in hot else 1)
        log(f"Tải được {before} ảnh, lỗi {stats['failed']}, treo {stats['frozen']}, ảnh mất tín hiệu {stats['placeholder']}")

    # 2. gửi AI theo lô
    batches = [images[i:i + BATCH] for i in range(0, len(images), BATCH)]

    def run(b):
        return classify([(c, d) for c, d, _ in b])

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

    # 3. tổng hợp kết quả
    by_id = {c["id"]: c for c in cams}
    detections = []
    for cid, cs in state["cams"].items():
        d = decide(cs, ts)
        if not d or cid not in by_id:
            continue
        c = by_id[cid]
        detections.append({"cam": cid, "name": c["name"], "lat": round(c["lat"], 6), "lng": round(c["lng"], 6), **d})
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
    }
    save_json(OUT_FILE, out)
    save_json(STATE_FILE, state)
    conf = sum(1 for d in detections if d["confirmed"])
    log(f"Xong: AI xem {stats['classified']} ảnh, {conf} chỗ ngập đã xác nhận, {len(detections) - conf} chỗ chờ xác nhận. "
        f"Chi phí lần này ${stats['cost_usd']:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
