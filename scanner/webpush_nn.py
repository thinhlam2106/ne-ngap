"""Thông báo đẩy (Web Push) cho Né Ngập, viết gọn bằng thư viện cryptography, không cần pywebpush.

Luồng:
  1. Trang web xin quyền thông báo, đăng ký với dịch vụ đẩy của trình duyệt (khoá công khai VAPID lấy từ flood-ai.json).
  2. Trang mã hoá gói đăng ký (endpoint + các ô ~3 km của "Nơi của tôi") bằng khoá công khai "enc", gửi vào một kênh ntfy.
     Chỉ bộ quét có khoá bí mật mới đọc được, nên kênh ntfy công khai cũng không lộ gì.
  3. Mỗi lượt, bộ quét đọc các gói mới, cập nhật danh sách, lưu danh sách đã mã hoá vào data/push.bin.
  4. Khi có tin cảnh báo cho một ô (ngập được xác nhận, sắp mưa to) hoặc tới giờ bản tin 6:30, 16:30,
     bộ quét gửi thông báo tới đúng những máy đã đăng ký ô đó.

Khoá: một secret GitHub tên PUSH_KEYS, nội dung JSON {"vapid": "<PEM>", "enc": "<PEM>"} (hai khoá EC P-256).
Không có secret thì toàn bộ phần này tự tắt.
"""
import base64
import json
import os
import struct
import time
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

CONTACT = "https://nengap-hcm.netlify.app"
MAX_SUBS = 5000


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def ub64u(s):
    s = s.strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hkdf(salt, ikm, info, n):
    return HKDF(algorithm=hashes.SHA256(), length=n, salt=salt, info=info).derive(ikm)


def pub_raw(priv):
    return priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def new_keys():
    """Tạo nội dung secret PUSH_KEYS mới."""
    out = {}
    for k in ("vapid", "enc"):
        p = ec.generate_private_key(ec.SECP256R1())
        out[k] = p.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    return json.dumps(out)


class Push:
    def __init__(self, secret, store_path):
        d = json.loads(secret)
        self.vapid = serialization.load_pem_private_key(d["vapid"].encode(), None)
        self.enc = serialization.load_pem_private_key(d["enc"].encode(), None)
        self.store_path = store_path
        enc_bytes = self.enc.private_numbers().private_value.to_bytes(32, "big")
        self.store_key = hkdf(b"ne-ngap", enc_bytes, b"ne-ngap-store", 32)
        self.subs = self._load()
        self._orig = json.dumps(self.subs, sort_keys=True)
        self.sent = 0
        self.dead = 0

    # ---- khoá công khai cho trang web ----
    def public(self):
        return {"vapid": b64u(pub_raw(self.vapid)), "enc": b64u(pub_raw(self.enc))}

    # ---- danh sách đăng ký (mã hoá khi lưu vào repo công khai) ----
    def _load(self):
        try:
            raw = open(self.store_path, "rb").read()
            return json.loads(AESGCM(self.store_key).decrypt(raw[:12], raw[12:], b"subs"))
        except Exception:
            return {}

    def save(self):
        if json.dumps(self.subs, sort_keys=True) == self._orig and os.path.exists(self.store_path):
            return                                   # không đổi thì không ghi, tránh commit thừa
        n = os.urandom(12)
        data = json.dumps(self.subs, separators=(",", ":")).encode()
        open(self.store_path, "wb").write(n + AESGCM(self.store_key).encrypt(n, data, b"subs"))

    # ---- đọc gói đăng ký trang web gửi lên ----
    def open_msg(self, msg):
        m = json.loads(msg)
        eph = ub64u(m["e"])
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), eph)
        shared = self.enc.exchange(ec.ECDH(), peer)
        key = hkdf(eph, shared, b"ne-ngap-dangky", 32)
        return json.loads(AESGCM(key).decrypt(ub64u(m["iv"]), ub64u(m["c"]), None))

    def apply(self, msg, now=None):
        now = now or int(time.time())
        try:
            p = self.open_msg(msg)
        except Exception:
            return False
        sub = p.get("sub") or {}
        ep = str(sub.get("endpoint") or "")
        if not ep.startswith("https://") or len(ep) > 600:
            return False
        if p.get("op") == "del" or not p.get("places"):
            return self.subs.pop(ep, None) is not None
        keys = sub.get("keys") or {}
        if not keys.get("p256dh") or not keys.get("auth"):
            return False
        places = []
        for x in p["places"][:6]:
            t = str(x.get("t", ""))
            if t.startswith("ne-ngap-o-"):
                places.append({"n": str(x.get("n", ""))[:24], "t": t})
        if not places:
            return False
        self.subs[ep] = {"k": {"p256dh": keys["p256dh"], "auth": keys["auth"]}, "p": places, "d": 1 if p.get("digest", True) else 0, "at": now}
        if len(self.subs) > MAX_SUBS:   # giữ các đăng ký mới nhất
            for k in sorted(self.subs, key=lambda e: self.subs[e]["at"])[: len(self.subs) - MAX_SUBS]:
                del self.subs[k]
        return True

    def pull(self, topic, since):
        """Đọc gói đăng ký mới trên kênh ntfy. Trả về id tin cuối để lần sau đọc tiếp."""
        url = f"https://ntfy.sh/{topic}/json?poll=1&since={since or '12h'}"
        r = requests.get(url, timeout=20)
        last, n = since, 0
        for line in r.text.splitlines():
            try:
                m = json.loads(line)
            except Exception:
                continue
            if m.get("event") != "message":
                continue
            last = m.get("id") or last
            if self.apply(m.get("message", ""), int(m.get("time") or 0) or None):
                n += 1
        return last, n

    # ---- gửi một thông báo (RFC 8291 aes128gcm + RFC 8292 VAPID) ----
    def _jwt(self, endpoint):
        u = urlparse(endpoint)
        head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
        body = b64u(json.dumps({"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": CONTACT}, separators=(",", ":")).encode())
        sig = self.vapid.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(sig)
        return f"{head}.{body}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"

    @staticmethod
    def encrypt(payload, p256dh, auth):
        ua_pub = ub64u(p256dh)
        secret = ub64u(auth)
        eph = ec.generate_private_key(ec.SECP256R1())
        as_pub = pub_raw(eph)
        shared = eph.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub))
        ikm = hkdf(secret, shared, b"WebPush: info\x00" + ua_pub + as_pub, 32)
        salt = os.urandom(16)
        cek = hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
        nonce = hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
        ct = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
        return salt + struct.pack(">IB", 4096, len(as_pub)) + as_pub + ct

    def send(self, endpoint, info, payload, ttl=3 * 3600, urgency="high"):
        body = self.encrypt(json.dumps(payload, ensure_ascii=False).encode(), info["k"]["p256dh"], info["k"]["auth"])
        h = {"Authorization": f"vapid t={self._jwt(endpoint)}, k={b64u(pub_raw(self.vapid))}", "Content-Encoding": "aes128gcm",
             "Content-Type": "application/octet-stream", "TTL": str(ttl), "Urgency": urgency}
        try:
            r = requests.post(endpoint, data=body, headers=h, timeout=10)
        except Exception:
            return False
        if r.status_code in (404, 410):          # máy đã huỷ đăng ký
            self.subs.pop(endpoint, None); self.dead += 1
            return False
        if r.status_code < 300:
            self.sent += 1
            return True
        return False

    def to_topic(self, topic, payload, limit=400):
        n = 0
        for ep, info in list(self.subs.items()):
            hit = [x for x in info["p"] if x["t"] == topic]
            if not hit:
                continue
            p = dict(payload)
            p["title"] = f"{payload['title']} (gần {hit[0]['n'].lower() or 'nơi của bạn'})"
            if self.send(ep, info, p):
                n += 1
            if n >= limit:
                break
        return n
