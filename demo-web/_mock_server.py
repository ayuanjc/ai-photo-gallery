"""本地自测 mock：模拟演示站后端契约 + 静态发 index.html。仅自测用，不部署。"""
import json
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CALLS = {}
SCENES = [
    {"key": "lake", "label": "lake", "count": 34}, {"key": "city_lights", "label": "city_lights", "count": 33},
    {"key": "building", "label": "building", "count": 26}, {"key": "graduation", "label": "graduation", "count": 26},
    {"key": "dog", "label": "dog", "count": 24}, {"key": "cat", "label": "cat", "count": 24},
    {"key": "fireworks", "label": "fireworks", "count": 8}, {"key": "food", "label": "food", "count": 12},
]

TRANS = {
    "海边日落": "a photo of the sunset at the beach", "城市夜景": "a photo of a city at night",
    "火锅": "a photo of hot pot", "蓝色的车": "a photo of a blue car",
    "人物肖像": "a photo of a portrait", "雪山": "a photo of a snow-capped mountain",
    "森林": "a photo of the forest", "猫": "a photo of a cat", "烟花": "a photo of fireworks",
    "毕业典礼": "a photo of a graduation", "湖": "a photo of a lake", "街边小吃": "a photo of street food",
}

def svg_thumb(name, w=321, h=480):
    hue = sum(ord(c) for c in name) % 360
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">'
            f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
            f'<stop offset="0" stop-color="hsl({hue},40%,22%)"/><stop offset="1" stop-color="hsl({(hue+60)%360},45%,12%)"/>'
            f'</linearGradient></defs><rect width="100%" height="100%" fill="url(#g)"/>'
            f'<text x="50%" y="50%" fill="rgba(255,255,255,.5)" font-size="18" text-anchor="middle" font-family="monospace">{name[:18]}</text></svg>'
            ).encode()

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p == "/api/health":
            self._json({"status": "ok", "model": "ViT-B-32__openai", "count": 487, "dim": 512, "load_ms": 374.2})
        elif p == "/api/scenes":
            self._json({"scenes": SCENES})
        elif p.startswith("/thumbs/"):
            name = p.split("/")[-1].replace(".webp", "")
            b = svg_thumb(name)
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif p == "/" or p == "/index.html":
            b = open("index.html", "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/api/search":
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            q = body.get("query", "")
            top_k = int(body.get("top_k", 24))
        except Exception:
            return self._json({"detail": "parse error"}, 422)
        if not isinstance(q, str) or not (1 <= len(q) <= 200):
            return self._json({"detail": "query invalid"}, 422)
        if not (1 <= top_k <= 24):
            return self._json({"detail": "top_k invalid"}, 422)

        key = q.strip()
        cached = CALLS.get(key, 0) > 0
        CALLS[key] = CALLS.get(key, 0) + 1
        en = TRANS.get(key)
        used = en is not None
        if not en:
            en = q if all(ord(c) < 128 for c in q) else "a photo of something"

        base = 0.2917 if used else 0.2400
        results = []
        n_res = 0 if key == "空空如也" else min(top_k, 24)
        for i in range(n_res):
            sub = ["单人", "合影", "海洋"][i % 3]
            results.append({
                "id": f"74f00ea7-bf40-49ad-9f4f-8c0515772{i:03d}",
                "score": round(base - i * 0.0012, 4),
                "rel": round(1.0 - i * 0.004, 4),
                "thumb": f"/thumbs/mock-{i:03d}.webp", "w": 321, "h": 480,
                "takenAt": "2025-11-09T09:05:31.000Z", "fileName": f"A01079{i}.ARW",
                "city": None if i % 2 else "上海", "country": None,
                "scene": None, "sub": sub, "scene_sim": 0.27,
            })
        self._json({
            "query": q, "translated": en, "translated_used": used, "text_cached": cached,
            "top_k": top_k, "results": results,
            "timing_ms": {"translate": 0.37, "text_embed": 0.0 if cached else 12.5, "total": 0.6 if cached else 14.2},
        })


if __name__ == "__main__":
    print("mock: http://127.0.0.1:8899")
    ThreadingHTTPServer(("127.0.0.1", 8899), H).serve_forever()