# -*- coding: utf-8 -*-
r"""在浏览器里放一张「永远是最新、且放大了」的 NapCat 登录二维码。

为什么要这个：`napcat\cache\qrcode.png` 只有 147x147，而且是每两分钟重画一次，
存成文件打开就定格了——用户扫到的很可能是过期码。
这里起一个只监听本机的小 HTTP 服务：/ 返回一个每 2 秒重新拉图的页面，
/qr 每次现读磁盘上最新的 qrcode.png 并放大，禁缓存。
"""
import http.server
import socketserver
import threading
import time
from io import BytesIO
from pathlib import Path

QR = Path(r"C:\Users\win11\qq-remote\napcat\cache\qrcode.png")
PORT = 18801
SCALE = 7

PAGE = """<!doctype html><meta charset=utf-8>
<title>NapCat QR</title>
<style>
 html,body{margin:0;height:100%;background:#fff;display:flex;flex-direction:column;
           align-items:center;justify-content:center;font:14px/1.6 system-ui,sans-serif;color:#333}
 img{width:min(80vh,90vw);image-rendering:pixelated;border:8px solid #fff}
 p{margin:6px 0 0}
</style>
<img id=q alt="QR">
<p id=s>加载中…</p>
<script>
async function tick(){
  try{
    const r = await fetch('/qr', {cache:'no-store'});
    if(!r.ok) throw new Error(r.status);
    const b = await r.blob();
    document.getElementById('q').src = URL.createObjectURL(b);
    document.getElementById('s').textContent = '已更新 ' + new Date().toLocaleTimeString();
  }catch(e){ document.getElementById('s').textContent = '读取失败：' + e.message; }
}
tick(); setInterval(tick, 2000);
</script>
""".encode("utf-8")


def upscale(raw: bytes) -> bytes:
    """PNG 放大：优先用 PIL；没有 PIL 就原样返回（浏览器里靠 CSS 放大也能扫）。"""
    try:
        from PIL import Image
    except Exception:
        return raw
    try:
        im = Image.open(BytesIO(raw))
        return_bytes = BytesIO()
        im.resize((im.width * SCALE, im.height * SCALE), Image.NEAREST) \
            .save(return_bytes, format="PNG")
        return return_bytes.getvalue()
    except Exception:
        return raw


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body: bytes, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/qr"):
            try:
                self._send(upscale(QR.read_bytes()), "image/png")
            except Exception as e:
                self._send(f"read error: {e}".encode(), "text/plain; charset=utf-8")
            return
        self._send(PAGE, "text/html; charset=utf-8")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    with Server(("127.0.0.1", PORT), H) as s:
        threading.Thread(target=s.serve_forever, daemon=True).start()
        print(f"http://127.0.0.1:{PORT}/", flush=True)
        # 前台挂着，靠 Ctrl+C / 杀进程结束；同时别让 stdout 撑满
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    main()
