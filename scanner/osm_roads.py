"""Tách mạng lưới đường thật của OpenStreetMap (đúng từng nút giao, chiều đi, cấm xe, cấm rẽ) cho Né Ngập.

Chạy trên GitHub Actions sau khi đã cắt vùng TP.HCM khỏi file vietnam-latest.osm.pbf của Geofabrik:
    python scanner/osm_roads.py hcm-roads.osm.pbf data/osm/roads.json.gz

Kết quả là dữ liệu thô, gọn (nén gzip): nút (toạ độ), tuyến đường (thẻ cần cho tìm đường + danh sách nút),
quan hệ cấm rẽ. Bộ dựng bản đồ của Né Ngập đọc file này để dựng lại đồ thị tìm đường.
"""
import gzip
import json
import sys
import time

import osmium

HW = {"motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link", "secondary", "secondary_link",
      "tertiary", "tertiary_link", "unclassified", "road", "residential", "living_street", "service", "pedestrian", "track"}
SKIP_SERVICE = {"parking_aisle", "drive-through", "emergency_access"}
KEEP = {"highway", "name", "ref", "oneway", "junction", "bridge", "tunnel", "layer", "access", "vehicle", "motor_vehicle",
        "motorcar", "motorcycle", "moped", "foot", "oneway:motorcycle", "oneway:moped", "oneway:bicycle", "service",
        "route", "maxspeed", "lanes", "area", "covered", "ford", "toll", "hgv", "psv", "bus"}


class Roads(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.nid = {}
        self.nodes = []
        self.ways = []
        self.rels = []
        self.tagidx = {}
        self.tags = []

    def t(self, k, v):
        key = k + "=" + v
        i = self.tagidx.get(key)
        if i is None:
            i = self.tagidx[key] = len(self.tags)
            self.tags.append(key)
        return i

    def way(self, w):
        tg = {x.k: x.v for x in w.tags}
        hw = tg.get("highway")
        ferry = tg.get("route") == "ferry"
        if not ferry and hw not in HW:
            return
        if tg.get("area") == "yes":
            return
        if hw == "service" and tg.get("service") in SKIP_SERVICE:
            return
        refs = []
        for n in w.nodes:
            if not n.location.valid():
                continue
            i = self.nid.get(n.ref)
            if i is None:
                i = self.nid[n.ref] = len(self.nodes)
                self.nodes.append((round(n.location.lat * 1e6), round(n.location.lon * 1e6)))
            if not refs or refs[-1] != i:
                refs.append(i)
        if len(refs) < 2:
            return
        tags = [self.t(k, v) for k, v in tg.items() if k in KEEP or k.endswith(":conditional")]
        self.ways.append((w.id, tags, refs))

    def relation(self, r):
        tg = {x.k: x.v for x in r.tags}
        if tg.get("type") != "restriction":
            return
        mem = []
        for m in r.members:
            if m.type == "n":
                mem.append(["n", self.nid.get(m.ref, -1), m.role])
            elif m.type == "w":
                mem.append(["w", m.ref, m.role])
        self.rels.append([r.id, [self.t(k, v) for k, v in tg.items()], mem])


def main():
    src, out = sys.argv[1], sys.argv[2]
    t0 = time.time()
    h = Roads()
    h.apply_file(src, locations=True, idx="flex_mem")
    flat, pa, pb = [], 0, 0
    for a, b in h.nodes:
        flat += [a - pa, b - pb]
        pa, pb = a, b
    ways = []
    for wid, tags, refs in h.ways:
        d = [refs[0]] + [refs[i] - refs[i - 1] for i in range(1, len(refs))]
        ways.append([wid, tags, d])
    data = {"v": 1, "made": int(time.time()), "src": "OpenStreetMap (ODbL), Geofabrik vietnam-latest",
            "tags": h.tags, "nodes": flat, "ways": ways, "rels": h.rels}
    s = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    with gzip.open(out, "wb", compresslevel=9) as f:
        f.write(s)
    summ = {"made": data["made"], "nodes": len(h.nodes), "ways": len(ways), "restrictions": len(h.rels),
            "bytes_json": len(s), "secs": round(time.time() - t0)}
    print(json.dumps(summ, ensure_ascii=False))
    with open(out.replace(".json.gz", "_summary.json"), "w") as f:
        json.dump(summ, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
