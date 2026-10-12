"""Tải dữ liệu OpenStreetMap mà bản nền bản đồ đã lược bỏ, để Né Ngập tìm đường đúng luật:
  - đường cấm/hạn chế xe (access, motor_vehicle, motorcar, motorcycle, hgv, maxwidth, maxweight..., kể cả cấm theo giờ)
  - cấm rẽ / chỉ được rẽ ở ngã tư (relation type=restriction)
  - trạm xe buýt và tuyến xe buýt (relation route=bus)
Chạy trên GitHub Actions (máy của Claude không vào được Overpass). Kết quả: data/osm/*.json, không hiện lên bản đồ,
chỉ để bộ tìm đường đọc.
"""
import json, time, pathlib, sys
import requests

BB = "10.35,106.35,11.20,107.05"          # TP.HCM (cũ) và vùng giáp ranh
EPS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
       "https://overpass.private.coffee/api/interpreter", "https://maps.mail.ru/osm/tools/overpass/api/interpreter"]
OUT = pathlib.Path(__file__).resolve().parent.parent / "data" / "osm"
TAGS = ["access", "vehicle", "motor_vehicle", "motorcar", "motorcycle", "moped", "hgv", "goods", "bus", "psv",
        "maxwidth", "maxweight", "maxheight", "maxlength", "width", "oneway", "oneway:motorcycle", "oneway:moped", "oneway:bus"]
COND = [t + ":conditional" for t in ["access", "vehicle", "motor_vehicle", "motorcar", "motorcycle", "hgv", "oneway"]]

Q = {
    "restrict": f'[out:json][timeout:600];way[highway]({BB})[~"^({"|".join(TAGS + COND)})$"~"."];out tags geom;',
    "turns": f'[out:json][timeout:600];relation[type=restriction]({BB});out tags geom;',
    "stops": f'[out:json][timeout:600];(node[highway=bus_stop]({BB});node[public_transport=platform][bus=yes]({BB});node[public_transport=stop_position][bus=yes]({BB}););out tags;',
    "bus": f'[out:json][timeout:900];relation[type=route][route=bus]({BB});out tags geom;',
}


def fetch(q):
    last = None
    for ep in EPS:
        for k in range(2):
            try:
                r = requests.post(ep, data={"data": q}, timeout=960, headers={"User-Agent": "ne-ngap/1.0 (student flood map)"})
                if r.status_code == 200:
                    return r.json()
                last = f"{ep} HTTP {r.status_code} {r.text[:200]!r}"
            except Exception as e:
                last = f"{ep} {repr(e)[:200]}"
            log("  thử", last)
            time.sleep(20)
    raise RuntimeError(last)


def rc(v):
    return round(v, 5)


def geom(el):
    return [[rc(p["lon"]), rc(p["lat"])] for p in el.get("geometry") or [] if p]


LOG = []


def log(*a):
    line = time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a)
    print(line, flush=True); LOG.append(line)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    summary = {"fetched": int(time.time())}
    only = sys.argv[1:] or list(Q)
    for name in only:
        try:
            one(name, summary)
        except Exception as e:
            import traceback
            summary[name] = {"error": repr(e)[:300]}
            log(name, "LỖI XỬ LÝ", traceback.format_exc()[-1500:])
        (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
        (OUT / "log.txt").write_text("\n".join(LOG))


def one(name, summary):
        t0 = time.time()
        try:
            d = fetch(Q[name])
        except Exception as e:
            summary[name] = {"error": str(e)}
            log(name, "LỖI TẢI", e)
            return
        els = d.get("elements", [])
        if d.get("remark"):
            log(name, "Overpass ghi chú:", d["remark"][:300])
        if name == "restrict":
            out = [{"id": e["id"], "t": {k: v for k, v in e.get("tags", {}).items() if k in TAGS or k in COND or k in ("highway", "name")}, "g": geom(e)} for e in els if e["type"] == "way"]
        elif name == "turns":
            out = []
            for e in els:
                mem = []
                for m in e.get("members", []):
                    if m["type"] == "node":
                        mem.append({"r": m["role"], "n": [rc(m.get("lon", 0)), rc(m.get("lat", 0))]})
                    elif m["type"] == "way":
                        mem.append({"r": m["role"], "g": geom(m)})
                out.append({"id": e["id"], "t": e.get("tags", {}), "m": mem})
        elif name == "stops":
            out = [{"id": e["id"], "ll": [rc(e["lon"]), rc(e["lat"])], "t": {k: v for k, v in e.get("tags", {}).items() if k in ("name", "route_ref", "ref", "shelter", "bench", "public_transport", "highway")}} for e in els if e["type"] == "node"]
        else:  # bus
            out = []
            for e in els:
                ways, stops = [], []
                for m in e.get("members", []):
                    if m["type"] == "way" and m.get("role", "") in ("", "forward", "backward"):
                        ways.append(geom(m))
                    elif m["type"] == "node" and ("stop" in m.get("role", "") or "platform" in m.get("role", "")):
                        stops.append([rc(m.get("lon", 0)), rc(m.get("lat", 0))])
                t = e.get("tags", {})
                out.append({"id": e["id"], "ref": t.get("ref", ""), "name": t.get("name", ""), "from": t.get("from", ""), "to": t.get("to", ""),
                            "op": t.get("operator", ""), "int": t.get("interval", ""), "hrs": t.get("opening_hours", ""), "w": ways, "s": stops})
        (OUT / f"{name}.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
        summary[name] = {"n": len(out), "secs": round(time.time() - t0), "bytes": (OUT / f"{name}.json").stat().st_size}
        log(name, len(out), "mục", round(time.time() - t0), "giây,", summary[name]["bytes"], "byte")
        time.sleep(15)


if __name__ == "__main__":
    main()
