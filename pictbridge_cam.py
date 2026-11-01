#!/usr/bin/env python3
"""讓 Mac 假扮 PictBridge 相機，透過 Wi-Fi（DPS over PTP-IP，CIPA DC-005）跟 SELPHY 溝通。實驗用。

流程：
  1. Mac 在 TCP 15740 開 PTP-IP Responder（相機端）
  2. 對印表機 UPnP 服務 DPSConnectionManager 呼叫 ConnectionRequest(我的 IP)
  3. 印表機反過來連進來（PTP Initiator），讀到 DDISCVRY.DPS → 送 HDISCVRY.DPS，完成 DPS 探索
  4. 相機端用 DREQUEST.DPS 送 DPS 指令（configurePrintService / getCapability / startJob）
     並用 PTP 事件 RequestObjectTransfer 通知印表機來拿；印表機以 HRSPONSE.DPS 回覆（檔名以印表機＝Host 角度命名：H=印表機產生、D=相機產生）

用法：
  python3 pictbridge_cam.py PRINTER_IP --probe             # 只探測能力，不印
  python3 pictbridge_cam.py PRINTER_IP --print photo.jpg [--date 2026/10/31] [--dateprint default|on|off]

參考：Eclipse ThreadX USBX ux_pictbridge（DPS 檔名、XML 標籤）、CIPA DC-001/DC-005。
"""
import argparse
import re
import socket
import struct
import sys
import threading
import time

import uuid

PTPIP_PORT = 15740
LOG_FILE = None

# PTP-IP 封包類型
INIT_CMD_REQ, INIT_CMD_ACK, INIT_EVT_REQ, INIT_EVT_ACK, INIT_FAIL = 1, 2, 3, 4, 5
OP_REQ, OP_RESP, EVENT, START_DATA, DATA, CANCEL, END_DATA, PROBE_REQ, PROBE_RESP = 6, 7, 8, 9, 10, 11, 12, 13, 14

# PTP 操作碼
OPS = {
    0x1001: "GetDeviceInfo", 0x1002: "OpenSession", 0x1003: "CloseSession", 0x1004: "GetStorageIDs",
    0x1005: "GetStorageInfo", 0x1006: "GetNumObjects", 0x1007: "GetObjectHandles", 0x1008: "GetObjectInfo",
    0x1009: "GetObject", 0x100A: "GetThumb", 0x100B: "DeleteObject", 0x100C: "SendObjectInfo",
    0x100D: "SendObject", 0x1014: "GetDevicePropDesc", 0x1015: "GetDevicePropValue", 0x101B: "GetPartialObject",
}
RC_OK, RC_GENERAL, RC_NOT_SUPPORTED, RC_INVALID_HANDLE = 0x2001, 0x2002, 0x2005, 0x2009
EVT_REQUEST_OBJECT_TRANSFER = 0x4009
FMT_SCRIPT, FMT_JPEG, FMT_UNDEF = 0x3002, 0x3801, 0x3000
STORAGE_ID = 0x00010001

H_DDISCVRY = 1        # 相機放的探索檔
H_DREQUEST = 2        # 相機 → 印表機的 DPS 請求
H_DRSPONSE = 3        # 相機回覆印表機請求（DRSPONSE.DPS）
H_JPEG = 4            # 要列印的照片

DPS_HEAD = '<?xml version="1.0"?>\n<dps xmlns="http://www.cipa.jp/dps/schema/">\n'
DPS_TAIL = '</dps>\n'


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    if LOG_FILE:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")


# ---------- PTP 資料型別 ----------

def ptp_str(s):
    if not s:
        return b"\x00"
    enc = (s + "\0").encode("utf-16-le")
    return bytes([len(s) + 1]) + enc


def ptp_arr16(vals):
    return struct.pack("<I", len(vals)) + b"".join(struct.pack("<H", v) for v in vals)


def ptp_arr32(vals):
    return struct.pack("<I", len(vals)) + b"".join(struct.pack("<I", v) for v in vals)


def read_ptp_str(buf, off):
    n = buf[off]
    off += 1
    if n == 0:
        return "", off
    s = buf[off:off + 2 * n].decode("utf-16-le").rstrip("\0")
    return s, off + 2 * n


def device_info():
    ops = sorted(OPS)
    return (struct.pack("<HIH", 100, 0, 0) + ptp_str("") + struct.pack("<H", 0)
            + ptp_arr16(ops) + ptp_arr16([EVT_REQUEST_OBJECT_TRANSFER]) + ptp_arr16([])
            + ptp_arr16([]) + ptp_arr16([FMT_SCRIPT, FMT_JPEG, FMT_UNDEF])
            + ptp_str("selphy-pictbridge") + ptp_str("selphy-pictbridge") + ptp_str("1.0") + ptp_str("0001"))


def storage_info():
    return (struct.pack("<HHH", 0x0003, 0x0001, 0x0000) + struct.pack("<QQI", 64 << 20, 32 << 20, 0)
            + ptp_str("Mac Storage") + ptp_str("MACPB"))


def object_info(name, fmt, size, w=0, h=0, date=""):
    return (struct.pack("<IHHI", STORAGE_ID, fmt, 0, size)
            + struct.pack("<HIIIIIIIHII", 0, 0, 0, 0, w, h, 24 if fmt == FMT_JPEG else 0, 0, 0, 0, 0)
            + ptp_str(name) + ptp_str(date) + ptp_str(date) + ptp_str(""))


# ---------- PTP-IP 連線 ----------

class Chan:
    def __init__(self, sock):
        self.s = sock
        self.lock = threading.Lock()

    def recv_exact(self, n):
        out = b""
        while len(out) < n:
            c = self.s.recv(n - len(out))
            if not c:
                raise ConnectionError("對方關閉連線")
            out += c
        return out

    def recv_pkt(self):
        ln, typ = struct.unpack("<II", self.recv_exact(8))
        return typ, self.recv_exact(ln - 8)

    def send_pkt(self, typ, payload=b""):
        with self.lock:
            self.s.sendall(struct.pack("<II", len(payload) + 8, typ) + payload)


class Camera:
    def __init__(self, jpeg=None, jpeg_name="IMG_0001.JPG", jpeg_date=""):
        self.objects = {H_DDISCVRY: ("DDISCVRY.DPS", FMT_SCRIPT, b"")}
        self.jpeg = jpeg
        if jpeg:
            self.objects[H_JPEG] = (jpeg_name, FMT_JPEG, jpeg)
        self.jpeg_date = jpeg_date
        self.cmd = None
        self.evt = None
        self.guid = uuid.uuid4().bytes
        self.discovered = threading.Event()
        self.response = None
        self.response_ready = threading.Event()
        self.host_requests = []          # 印表機主動送來的 HREQUEST（例如 notifyJobStatus）
        self.pending_send = None         # SendObjectInfo 後等待 SendObject 的檔名
        self.closed = threading.Event()
        self.job_done = threading.Event()

    # --- 接受連線 ---
    def serve(self, srv):
        conn, addr = srv.accept()
        log(f"印表機連進來（command）：{addr}")
        self.cmd = Chan(conn)
        typ, p = self.cmd.recv_pkt()
        if typ != INIT_CMD_REQ:
            raise RuntimeError(f"預期 Init_Command_Request，收到 type {typ}")
        name = p[16:-4].decode("utf-16-le", "replace").rstrip("\0")   # GUID(16) + 名稱 + 版本(4)
        log(f"  Init_Command_Request：initiator 名稱={name!r}")
        ack = struct.pack("<I", 1) + self.guid + ("selphy-pictbridge\0").encode("utf-16-le") + struct.pack("<I", 0x00010000)
        self.cmd.send_pkt(INIT_CMD_ACK, ack)
        conn2, addr2 = srv.accept()
        log(f"印表機連進來（event）：{addr2}")
        self.evt = Chan(conn2)
        typ, p = self.evt.recv_pkt()
        if typ != INIT_EVT_REQ:
            raise RuntimeError(f"預期 Init_Event_Request，收到 type {typ}")
        self.evt.send_pkt(INIT_EVT_ACK)
        log("  PTP-IP 連線建立")
        threading.Thread(target=self._evt_loop, daemon=True).start()
        try:
            self._cmd_loop()
        except OSError as e:   # 含 ConnectionError，以及 shutdown() 關掉 socket 後的 Bad file descriptor
            log(f"連線結束：{e}")
        finally:
            self.closed.set()

    def _evt_loop(self):
        try:
            while True:
                typ, p = self.evt.recv_pkt()
                if typ == PROBE_REQ:
                    self.evt.send_pkt(PROBE_RESP)
                else:
                    log(f"  event 通道收到 type {typ} {p[:16].hex()}")
        except Exception:
            pass

    def send_event(self, code, *params):
        payload = struct.pack("<HI", code, 0xFFFFFFFF) + b"".join(struct.pack("<I", x) for x in params)
        self.evt.send_pkt(EVENT, payload)

    # --- 操作處理 ---
    def _send_data(self, tid, data):
        self.cmd.send_pkt(START_DATA, struct.pack("<IQ", tid, len(data)))
        self.cmd.send_pkt(END_DATA, struct.pack("<I", tid) + data)

    def _respond(self, tid, code=RC_OK, *params):
        self.cmd.send_pkt(OP_RESP, struct.pack("<HI", code, tid) + b"".join(struct.pack("<I", x) for x in params))

    def _recv_data(self):
        """接收 initiator 送來的資料階段（Start_Data → Data* → End_Data）。"""
        buf = b""
        while True:
            typ, p = self.cmd.recv_pkt()
            if typ == START_DATA:
                continue
            if typ in (DATA, END_DATA):
                buf += p[4:]
                if typ == END_DATA:
                    return buf
            elif typ == CANCEL:
                raise RuntimeError("印表機取消資料傳送")

    def _cmd_loop(self):
        while True:
            typ, p = self.cmd.recv_pkt()
            if typ == PROBE_REQ:
                self.cmd.send_pkt(PROBE_RESP)
                continue
            if typ != OP_REQ:
                log(f"  command 通道收到非預期 type {typ}")
                continue
            phase, code, tid = struct.unpack_from("<IHI", p, 0)
            params = list(struct.unpack_from(f"<{(len(p) - 10) // 4}I", p, 10))
            name = OPS.get(code, hex(code))
            if code not in (0x1009, 0x101B):
                log(f"  ← {name} {['0x%x' % x for x in params]}")
            self._handle(code, tid, params)

    def _handle(self, code, tid, params):
        if code == 0x1001:
            self._send_data(tid, device_info()); self._respond(tid)
        elif code in (0x1002, 0x1003):
            self._respond(tid)
        elif code == 0x1004:
            self._send_data(tid, ptp_arr32([STORAGE_ID])); self._respond(tid)
        elif code == 0x1005:
            self._send_data(tid, storage_info()); self._respond(tid)
        elif code in (0x1006, 0x1007):
            fmt = params[1] if len(params) > 1 else 0
            hs = [h for h, (_, f, _) in self.objects.items() if fmt in (0, 0xFFFFFFFF) or f == fmt]
            if code == 0x1006:
                self._respond(tid, RC_OK, len(hs))
            else:
                self._send_data(tid, ptp_arr32(hs)); self._respond(tid)
        elif code == 0x1008:
            h = params[0]
            if h not in self.objects:
                return self._respond(tid, RC_INVALID_HANDLE)
            n, f, d = self.objects[h]
            w = hgt = 0
            if f == FMT_JPEG:
                w, hgt = jpeg_size(d)
            self._send_data(tid, object_info(n, f, len(d), w, hgt, self.jpeg_date)); self._respond(tid)
        elif code == 0x1009:
            h = params[0]
            if h not in self.objects:
                return self._respond(tid, RC_INVALID_HANDLE)
            n, f, d = self.objects[h]
            log(f"  ← GetObject {n}（{len(d)} bytes）")
            self._send_data(tid, d); self._respond(tid)
        elif code == 0x101B:
            h, off, ln = params[0], params[1], params[2]
            if h not in self.objects:
                return self._respond(tid, RC_INVALID_HANDLE)
            d = self.objects[h][2][off:off + ln]
            self._send_data(tid, d); self._respond(tid, RC_OK, len(d))
        elif code == 0x100B:
            self.objects.pop(params[0], None) if params and params[0] not in (H_DDISCVRY, H_JPEG) else None
            self._respond(tid)
        elif code == 0x100C:
            data = self._recv_data()
            fname, _ = read_ptp_str(data, 52)
            log(f"  ← SendObjectInfo：{fname}")
            self._respond(tid, RC_OK, STORAGE_ID, 0, 0x100)
            if fname == "HDISCVRY.DPS":
                # 依 USBX：收到 HDISCVRY.DPS 的 ObjectInfo 就算探索完成，印表機不會再送 SendObject
                log("★ DPS 探索完成（印表機送來 HDISCVRY.DPS）")
                self.discovered.set()
            else:
                self.pending_send = fname
        elif code == 0x100D:
            data = self._recv_data()
            fname, self.pending_send = self.pending_send, None
            self._respond(tid)
            self._on_file(fname, data)
        else:
            log(f"  （不支援 {OPS.get(code, hex(code))}）")
            self._respond(tid, RC_NOT_SUPPORTED)

    def _on_file(self, fname, data):
        text = data.decode("utf-8", "replace")
        if fname == "HDISCVRY.DPS":
            log("★ DPS 探索完成（印表機送來 HDISCVRY.DPS）")
            self.discovered.set()
        elif fname == "HRSPONSE.DPS":
            log(f"  印表機回覆：\n{text}")
            self.response = text
            self.response_ready.set()
        elif fname == "HREQUEST.DPS":
            log(f"  印表機主動通知：\n{text}")
            self.host_requests.append(text)
            m = re.search(r"<jobEndReason>(7[0-9A-F]{7})</jobEndReason>", text)
            if m and m.group(1) != "71000000":
                log(f"★ 工作結束：jobEndReason={m.group(1)}（71010000＝正常完成）")
                self.job_done.set()
            if "<errorStatus>72020000</errorStatus>" in text:
                log("★ 印表機回報嚴重錯誤（72020000）")
                self.job_done.set()
            threading.Thread(target=self._answer_host, args=(text,), daemon=True).start()
        else:
            log(f"  收到檔案 {fname}（{len(data)} bytes）")

    def _answer_host(self, req):
        """印表機的 notifyJobStatus / notifyDeviceStatus 一律回 OK（10000000）。"""
        m = re.search(r"<input>\s*<(\w+)", req)
        op = m.group(1) if m else "notifyDeviceStatus"
        body = f"{DPS_HEAD}<output>\n<result>10000000</result>\n<{op}/>\n</output>\n{DPS_TAIL}"
        self.objects[H_DRSPONSE] = ("DRSPONSE.DPS", FMT_SCRIPT, body.encode())
        self.send_event(EVT_REQUEST_OBJECT_TRANSFER, H_DRSPONSE)

    # --- DPS 請求 ---
    def dps(self, inner, timeout=30):
        body = f"{DPS_HEAD}<input>\n{inner}</input>\n{DPS_TAIL}"
        brief = re.sub(r"\s+", " ", inner)[:200]
        log(f"→ DPS 請求：{brief}")
        self.response_ready.clear()
        self.objects[H_DREQUEST] = ("DREQUEST.DPS", FMT_SCRIPT, body.encode())
        self.send_event(EVT_REQUEST_OBJECT_TRANSFER, H_DREQUEST)
        if not self.response_ready.wait(timeout):
            raise TimeoutError("印表機沒有回覆 DPS 請求")
        return self.response


def jpeg_size(d):
    i = 2
    while i < len(d) - 9:
        if d[i] != 0xFF:
            i += 1
            continue
        m = d[i + 1]
        if m in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", d[i + 5:i + 9])
            return w, h
        i += 2 + struct.unpack(">H", d[i + 2:i + 4])[0]
    return 0, 0


def connection_request(printer_ip, my_ip):
    """SOAP ConnectionRequest。SELPHY 的 NetX HTTP 伺服器很挑格式：要 CRLF、大寫標頭、HOST 帶埠號，
    用 urllib 送會被直接 500 中止，所以用原始 socket（2026-11-01 實測此寫法回 ACCEPT）。"""
    st = "urn:schemas-cipa-jp:service:DPSConnectionManager:1"
    body = ('<?xml version="1.0"?>\r\n<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">\r\n<s:Body>\r\n'
            f'<u:ConnectionRequest xmlns:u="{st}">\r\n<PTPResponderIPAddress>{my_ip}</PTPResponderIPAddress>\r\n'
            '</u:ConnectionRequest>\r\n</s:Body>\r\n</s:Envelope>\r\n').encode()
    req = (f"POST /root/soap_folder HTTP/1.1\r\nHOST: {printer_ip}:80\r\nCONTENT-LENGTH: {len(body)}\r\n"
           f'CONTENT-TYPE: text/xml; charset="utf-8"\r\nSOAPACTION: "{st}#ConnectionRequest"\r\n'
           "USER-AGENT: Canon/1.0 UPnP/1.0 DPS/1.0\r\n\r\n").encode() + body
    s = socket.create_connection((printer_ip, 80), timeout=10)
    s.sendall(req)
    resp = b""
    try:
        while True:
            c = s.recv(4096)
            if not c:
                break
            resp += c
    except socket.timeout:
        pass
    s.close()
    text = resp.decode(errors="replace")
    m = re.search(r"<RequestResult>(\w+)</RequestResult>", text)
    return (m.group(1) if m else "?"), text


def my_ip_for(printer_ip):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect((printer_ip, 80))
    ip = s.getsockname()[0]
    s.close()
    return ip


CAPABILITY_ITEMS = ["qualities", "paperSizes", "paperTypes", "fileTypes", "datePrints",
                    "fileNamePrints", "imageOptimizes", "fixedSizes", "croppings"]
CONFIGURE = ("<configurePrintService>\n<dpsVersions>1.0</dpsVersions>\n<vendorName>selphy-pictbridge</vendorName>\n"
             "<vendorSpecificVersion>1.0</vendorSpecificVersion>\n<productName>selphy-pictbridge</productName>\n"
             "<serialNo>0001</serialNo>\n</configurePrintService>\n")


def start(printer_ip, cam):
    my_ip = my_ip_for(printer_ip)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", PTPIP_PORT))
    srv.listen(2)
    srv.settimeout(30)
    t = threading.Thread(target=cam.serve, args=(srv,), daemon=True)
    t.start()
    result, raw = connection_request(printer_ip, my_ip)
    log(f"ConnectionRequest({my_ip}) → {result}")
    if result != "ACCEPT":
        log(raw[:500])
        raise RuntimeError(f"印表機拒絕連線：{result}")
    if not cam.discovered.wait(40):
        raise TimeoutError("40 秒內沒有完成 DPS 探索")
    return srv


def probe(printer_ip):
    cam = Camera()
    srv = None
    try:
        srv = start(printer_ip, cam)
        cam.dps(CONFIGURE)
        for item in CAPABILITY_ITEMS:
            try:
                cam.dps(f"<getCapability>\n<capability>\n<{item}/>\n</capability>\n</getCapability>\n", timeout=15)
            except TimeoutError as e:
                log(f"  {item}：{e}")
        log("探測結束（沒有送 startJob，不會出紙）")
    finally:
        shutdown(srv, cam)


def shutdown(srv, cam):
    """不論成敗都關掉 15740 與兩條 PTP-IP 連線，避免印表機卡在 BUSY。"""
    for ch in (cam.cmd, cam.evt):
        try:
            ch and ch.s.close()
        except Exception:
            pass
    try:
        srv and srv.close()
    except Exception:
        pass


def print_bytes(printer_ip, data, date="", dateprint="default", layout="57000000", paper_size="51000000"):
    """用 PictBridge 列印一張 JPEG。回傳 (成功與否, 說明)。dateprint=default 會套用機器的日期設定（含模糊顯示）。"""
    cam = Camera(jpeg=data, jpeg_date=time.strftime("%Y%m%dT%H%M%S"))
    srv = None
    try:
        srv = start(printer_ip, cam)
        cam.dps(CONFIGURE)
        dp = {"default": "54000000", "off": "54010000", "on": "54020000"}[dateprint]
        date_tag = f"<date>{date}</date>\n" if date else ""
        job = ("<startJob>\n<jobConfig>\n<quality>50000000</quality>\n<paperType>52000000</paperType>\n"
               f"<paperSize>{paper_size}</paperSize>\n<fileType>53010000</fileType>\n<datePrint>{dp}</datePrint>\n"
               "<fileNamePrint>55000000</fileNamePrint>\n<imageOptimize>56000000</imageOptimize>\n"
               f"<layout>{layout}</layout>\n<fixedSize>58000000</fixedSize>\n<cropping>59000000</cropping>\n</jobConfig>\n"
               f"<printInfo>\n<fileID>{H_JPEG:08X}</fileID>\n{date_tag}</printInfo>\n</startJob>\n")
        resp = cam.dps(job, timeout=60)
        if "<result>10000000</result>" not in resp:
            words = re.sub(r"<[^>]+>", " ", resp).split()[:6]
            return False, f"印表機拒絕列印工作：{words}"
        log("startJob 已被接受，等印表機讀照片並列印（最多 5 分鐘）")
        # 進度由印表機的 HREQUEST（notifyJobStatus／notifyDeviceStatus）即時記錄；收到 jobEndReason 就結束
        end = time.time() + 300
        while time.time() < end and not cam.job_done.is_set() and not cam.closed.is_set():
            time.sleep(0.5)
        time.sleep(3)   # 讓最後一個 DRSPONSE 送出
        if not cam.job_done.is_set():
            return False, "等待逾時或連線中斷（看看印表機螢幕有沒有錯誤）"
        last = next((r for r in reversed(cam.host_requests) if "jobEndReason" in r), "")
        if "71010000" in last:
            return True, "完成"
        codes = re.findall(r"<(\w+)>(7[0-9A-F]{7})<", last)
        return False, f"印表機結束工作但不是正常完成：{codes}"
    except Exception as e:
        log(f"✘ {type(e).__name__}: {e}")
        return False, str(e)
    finally:
        shutdown(srv, cam)


def do_print(printer_ip, jpeg_path, date, dateprint, layout, paper_size):
    ok, msg = print_bytes(printer_ip, open(jpeg_path, "rb").read(), date, dateprint, layout, paper_size)
    log(f"結果：{'成功' if ok else '失敗'}－{msg}")
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("printer")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--print", dest="jpeg")
    ap.add_argument("--date", default="", help="printInfo 的日期字串，例如 2026/10/31")
    ap.add_argument("--dateprint", default="default", choices=["default", "on", "off"])
    ap.add_argument("--layout", default="57000000")
    ap.add_argument("--paper", default="51000000", help="paperSize，51000000=印表機預設")
    ap.add_argument("--log")
    a = ap.parse_args()
    LOG_FILE = a.log
    if a.probe:
        probe(a.printer)
    elif a.jpeg:
        do_print(a.printer, a.jpeg, a.date, a.dateprint, a.layout, a.paper)
    else:
        ap.error("要 --probe 或 --print")
