#!/usr/bin/env python3
"""SELPHY CP1300 本機編輯列印小工具：提供編輯畫面，並把 JPEG 透過 IPP（ipptool）送到印表機。

只用 Python 標準庫。啟動：python3 server.py → 開 http://127.0.0.1:8765
"""
import json
import os
import re
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).parent
import cpnp  # noqa: E402  （同目錄的 CPNP 實驗實作）
import pictbridge_cam  # noqa: E402  （Mac 假扮相機的 PictBridge 實作，能印機器的模糊日期）
def discover_printer_uri():
    """沒設 SELPHY_URI 時，用 macOS 內建 ippfind 找名稱含 SELPHY 的 AirPrint 印表機（約 5 秒）。"""
    try:
        r = subprocess.run(["ippfind", "-T", "5", "_ipp._tcp", "--name", "SELPHY"],
                           capture_output=True, text=True, timeout=15)
        uris = r.stdout.split()
        return uris[0] if uris else None
    except (OSError, subprocess.TimeoutExpired):
        return None


PRINTER_URI = os.environ.get("SELPHY_URI") or "ipp://selphy.invalid:631/ipp/print"   # 啟動時若沒設定會自動搜尋
PORT = int(os.environ.get("SELPHY_PORT", "8765"))

# SELPHY 的網路晶片同時只能處理一個連線，多個請求並發會讓它整個卡死，所以所有 ipptool 呼叫都要排隊
PRINTER_LOCK = threading.Lock()
_last_status = {"online": False, "code": "unknown"}

# 紙張尺寸，單位 1/100 mm（IPP media-size 的單位）
PAPERS = {
    "L": (8900, 11900),
    "postcard": (10000, 14800),
    "card": (5400, 8600),
}
# 機器原生有邊框模式的邊界（左右, 上下），單位 1/100 mm；取自印表機回報的 media-col-ready / media-col-default
NATIVE_MARGINS = {
    "L": (250, 340),
    "postcard": (250, 370),
}

GET_ATTRS_TEST = """{
  OPERATION Get-Printer-Attributes
  GROUP operation-attributes-tag
  ATTR charset attributes-charset utf-8
  ATTR naturalLanguage attributes-natural-language en
  ATTR uri printer-uri $uri
  STATUS successful-ok
}
"""

JOB_TEST = """{
  OPERATION $op
  GROUP operation-attributes-tag
  ATTR charset attributes-charset utf-8
  ATTR naturalLanguage attributes-natural-language en
  ATTR uri printer-uri $uri
  ATTR name requesting-user-name selphy-pictbridge
  ATTR name job-name selphy-pictbridge
  ATTR mimeMediaType document-format image/jpeg
  GROUP job-attributes-tag
  ATTR integer copies $copies
  ATTR collection media-col {
    MEMBER collection media-size {
      MEMBER integer x-dimension $xdim
      MEMBER integer y-dimension $ydim
    }
    MEMBER integer media-bottom-margin $tb
    MEMBER integer media-left-margin $lr
    MEMBER integer media-right-margin $lr
    MEMBER integer media-top-margin $tb
    MEMBER keyword media-type photographic
    MEMBER keyword media-source photo
  }
  ATTR keyword print-scaling fill
  $file_line
  STATUS successful-ok
}
"""
# Validate-Job 不能附檔：SELPHY 驗證時不讀檔案內容，附上大檔會卡在上傳、拖垮印表機的網路


def build_job_args(test_path, jpeg_path, paper, copies, validate_only, bordered=False):
    """組出 ipptool 指令。validate_only=True 時用 Validate-Job（印表機只驗證不出紙）。"""
    if paper not in PAPERS:
        raise ValueError(f"未知紙張：{paper}")
    if not 1 <= copies <= 10:
        raise ValueError("張數需介於 1–10")
    if bordered and paper not in NATIVE_MARGINS:
        raise ValueError("這種紙沒有原生有邊框模式")
    x, y = PAPERS[paper]
    lr, tb = NATIVE_MARGINS[paper] if bordered else (0, 0)
    args = ["ipptool", "-tv", "-T", "60",
            "-d", f"xdim={x}", "-d", f"ydim={y}", "-d", f"copies={copies}",
            "-d", f"lr={lr}", "-d", f"tb={tb}"]
    if validate_only:
        args += ["-d", "op=Validate-Job"]
    else:
        args += ["-d", "op=Print-Job", "-f", str(jpeg_path)]
    return args + [PRINTER_URI, str(test_path)]


def job_test_text(validate_only):
    return JOB_TEST.replace("$file_line", "" if validate_only else "FILE $filename")


def parse_attrs(text):
    """把 ipptool -tv 輸出的 `name (type) = value` 行解析成 dict。"""
    attrs = {}
    for m in re.finditer(r"^\s+([\w-]+) \([^)]*\) = (.*)$", text, re.M):
        attrs.setdefault(m.group(1), m.group(2).strip())
    return attrs


def run_ipptool(args, timeout=90):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "連線逾時（印表機沒開或不在同一個 Wi-Fi？）"
    out = r.stdout + r.stderr
    return ("[PASS]" in out), out


def printer_status():
    global _last_status
    # 印表機正在處理別的請求就不插隊，回上一次的狀態
    if not PRINTER_LOCK.acquire(blocking=False):
        return {**_last_status, "busy": True}
    try:
        with tempfile.TemporaryDirectory() as d:
            t = Path(d) / "gpa.test"
            t.write_text(GET_ATTRS_TEST)
            ok, out = run_ipptool(["ipptool", "-tv", "-T", "10", PRINTER_URI, str(t)], timeout=15)
    finally:
        PRINTER_LOCK.release()
    if not ok:
        _last_status = {"online": False, "code": "not_found"}
        return _last_status
    a = parse_attrs(out)
    _last_status = {
        "online": True,
        "state": a.get("printer-state", "?"),
        "reasons": a.get("printer-state-reasons", "?"),
        "media_ready": a.get("media-ready", "?"),
        "queued": a.get("queued-job-count", "?"),
    }
    return _last_status


def submit_job(jpeg_bytes, paper, copies, validate_only, bordered=False):
    if not PRINTER_LOCK.acquire(timeout=30):
        return {"ok": False, "job_id": None, "code": "busy", "status": ""}
    try:
        return _submit_job(jpeg_bytes, paper, copies, validate_only, bordered)
    finally:
        PRINTER_LOCK.release()


def submit_cpnp(jpeg_bytes, paper, bordered, date, w, h):
    """CPNP（Canon App 協定，舊版 flags 開始指令）送印；有日期就寫進 EXIF，看印表機會不會自己加日期。"""
    if not PRINTER_LOCK.acquire(timeout=30):
        return {"ok": False, "job_id": None, "code": "busy", "status": ""}
    host = urlparse(PRINTER_URI).hostname
    try:
        if date:
            jpeg_bytes = cpnp.add_exif_date(jpeg_bytes, date)
        cpnp.LOG_FILE = str(ROOT / "cpnp_last.log")
        Path(cpnp.LOG_FILE).write_text(f"paper={paper} bordered={bordered} date={date} {w}x{h}\n")
        ok = cpnp.print_jpeg(host, jpeg_bytes, w, h, paper, bordered, legacy=True)
        return {"ok": ok, "job_id": "CPNP", "code": "done" if ok else "no_end", "status": ""}
    except Exception as e:
        return {"ok": False, "job_id": None, "code": "error", "status": str(e)}
    finally:
        PRINTER_LOCK.release()


def submit_pictbridge(jpeg_bytes, date, dateprint):
    """PictBridge（DPS over PTP-IP）送印。dateprint=default 時套用機器「打印設定→日期」（模糊顯示就是在這裡設）。
    日期同時寫進 EXIF 與 DPS printInfo，機器不管讀哪一個都拿得到。"""
    if not PRINTER_LOCK.acquire(timeout=30):
        return {"ok": False, "job_id": None, "code": "busy", "status": ""}
    host = urlparse(PRINTER_URI).hostname
    try:
        if date:
            jpeg_bytes = cpnp.add_exif_date(jpeg_bytes, date)
        pictbridge_cam.LOG_FILE = str(ROOT / "pictbridge_last.log")
        Path(pictbridge_cam.LOG_FILE).write_text(f"date={date} dateprint={dateprint} size={len(jpeg_bytes)}\n")
        ok, code, detail = pictbridge_cam.print_bytes(host, jpeg_bytes, date[:10].replace(":", "/") if date else "", dateprint)
        return {"ok": ok, "job_id": "PictBridge", "code": code, "status": detail}
    finally:
        PRINTER_LOCK.release()


def _submit_job(jpeg_bytes, paper, copies, validate_only, bordered):
    with tempfile.TemporaryDirectory() as d:
        jp = Path(d) / "print.jpg"
        jp.write_bytes(jpeg_bytes)
        t = Path(d) / "job.test"
        t.write_text(job_test_text(validate_only))
        ok, out = run_ipptool(build_job_args(t, jp, paper, copies, validate_only, bordered))
    a = parse_attrs(out)
    return {"ok": ok, "job_id": a.get("job-id"), "code": "done" if ok else "ipp_error", "status": a.get("status-code", out[-300:])}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")   # 改版後重新整理一定拿到新版
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/status":
            self._send(200, printer_status())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/print":
            return self._send(404, {"error": "not found"})
        q = parse_qs(u.query)
        try:
            paper = q.get("paper", ["L"])[0]
            copies = int(q.get("copies", ["1"])[0])
            validate_only = q.get("validate", ["0"])[0] == "1"
            bordered = q.get("bordered", ["0"])[0] == "1"
            n = int(self.headers.get("Content-Length", "0"))
            if not 0 < n < 30_000_000:
                raise ValueError("影像大小不合理")
            body = self.rfile.read(n)
            if body[:2] != b"\xff\xd8":
                raise ValueError("不是 JPEG")
            transport = q.get("transport", ["ipp"])[0]
            date = q.get("date", [""])[0]
            if date and not re.fullmatch(r"\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}", date):
                raise ValueError("日期格式要 YYYY:MM:DD HH:MM:SS")
            if transport == "pictbridge" and not validate_only:
                dateprint = q.get("dateprint", ["default"])[0]
                if dateprint not in ("default", "off"):
                    raise ValueError("dateprint 只能是 default 或 off")
                self._send(200, submit_pictbridge(body, date, dateprint))
            elif transport == "cpnp" and not validate_only:
                w, h = int(q.get("w", ["0"])[0]), int(q.get("h", ["0"])[0])
                self._send(200, submit_cpnp(body, paper, bordered, date, w, h))
            else:
                self._send(200, submit_job(body, paper, copies, validate_only, bordered))
        except ValueError as e:
            self._send(400, {"ok": False, "code": "bad_request", "status": str(e)})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    if not os.environ.get("SELPHY_URI"):
        print("SELPHY_URI not set, searching for a SELPHY on the network… (press the printer's Wi-Fi (( )) button first)", flush=True)
        found = discover_printer_uri()
        if found:
            PRINTER_URI = found
        else:
            print("No SELPHY found. Set SELPHY_URI=ipp://<printer-ip>:631/ipp/print and start again.", flush=True)
    print(f"SELPHY editor: http://127.0.0.1:{PORT}  (printer: {PRINTER_URI})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
