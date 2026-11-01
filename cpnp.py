#!/usr/bin/env python3
"""Canon CPNP（SELPHY 官方 App 用的 Wi-Fi 協定）Python 實作，實驗用。

移植自 tlambertz/selphy-print 的 server/cpnp.js（MIT），該實作由反編譯 SELPHY Photo Layout 4.3.10 而來，
原作者只在 CP1500 驗證過；CP1300 屬於實驗。
Ported from https://github.com/tlambertz/selphy-print server/cpnp.js
Copyright (c) 2026 selphy-print contributors, MIT License (full text in LICENSE).
CP1300 addition: the CP900-era 64-byte "flags" start command (--legacy), from selphy_go.

用法：
  python3 cpnp.py HOST --handshake                 # 只連線＋讀狀態＋斷線，不出紙
  python3 cpnp.py HOST photo.jpg --size L --finish 2 [--bordered] [--optimize]
"""
import argparse
import socket
import struct
import sys
import time

PORT = 8609
MAGIC = b"CPNP"
OP_SESSION_START, OP_SESSION_END, OP_READ, OP_WRITE = 0x10, 0x11, 0x20, 0x21
OP_GET_MAX, OP_SET_MAX = 0x51, 0x52
CODE_TRANSFER, CODE_CANCEL, CODE_END, CODE_RESUME, CODE_START_SPOOL, CODE_EXECUTE = 1, 2, 3, 6, 7, 8
SIZES = {"card": 2, "L": 3, "postcard": 4}
MAX_CHUNK = 33792

ERRORS = {
    0x0000010A: "等待進紙", 0x00010101: "沒紙", 0x00010102: "沒有紙匣", 0x00010103: "紙匣尺寸不對",
    0x00010109: "紙匣蓋開著", 0x0001010C: "紙張尺寸不同", 0x00010201: "色帶用完", 0x00010202: "沒有色帶匣",
    0x0003FF05: "印表機解不開 JPEG", 0x00FF0104: "卡紙",
}
WAITABLE = {0x0000010A, 0x00010101, 0x00010102}
PASSES = {0xFF01: "初始化", 0xFF02: "黃（加熱）", 0xFF03: "黃", 0xFF04: "洋紅（加熱）", 0xFF05: "洋紅",
          0xFF06: "青（加熱）", 0xFF07: "青", 0xFF08: "保護膜（加熱）", 0xFF09: "保護膜",
          0xFF0A: "收尾", 0xFF10: "進紙"}

_pid = 0


LOG_FILE = None


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    if LOG_FILE:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")


def next_pid():
    global _pid
    _pid = 1 if _pid >= 0xFFFF else _pid + 1
    return _pid


def header(op, session=0, payload_len=0):
    h = bytearray(MAGIC + bytes([1, op]) + bytes(10))
    struct.pack_into(">H", h, 8, next_pid())
    struct.pack_into(">H", h, 10, session)
    struct.pack_into(">I", h, 12, payload_len)
    return h


def udp(host, packet, expect, timeout=3.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(bytes(packet), (host, PORT))
        end = time.time() + timeout
        while time.time() < end:
            data, _ = s.recvfrom(4096)
            if data[:4] == MAGIC and data[4] == 0x81 and data[5] == expect:
                return data
        raise TimeoutError("CPNP UDP 沒回應")
    finally:
        s.close()


def session_start(host):
    buf = bytearray(408)
    buf[:24] = bytes([67, 80, 78, 80, 1, 0x10, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0x88]) + bytes(8)
    struct.pack_into(">H", buf, 8, next_pid())
    name = "selphy-pictbridge".encode("utf-16-be")[:64]
    buf[24:24 + len(name)] = name
    user, doc = "SPL v2.0".encode("utf-16-be"), "Square".encode("utf-16-be")
    buf[88:88 + len(user)] = user
    buf[152:152 + len(doc)] = doc
    r = udp(host, buf, OP_SESSION_START)
    result = struct.unpack_from(">H", r, 6)[0]
    if result != 0:
        raise RuntimeError(f"sessionStart 失敗 result={result}")
    sid, port = struct.unpack_from(">H", r, 10)[0], struct.unpack_from(">H", r, 20)[0]
    if not sid or not port:
        raise RuntimeError("sessionStart 回應沒有 session/port")
    return sid, port


def session_end(host, sid):
    try:
        udp(host, header(OP_SESSION_END, sid), OP_SESSION_END, timeout=2)
    except Exception:
        pass


class Conn:
    def __init__(self, host, port):
        self.s = socket.create_connection((host, port), timeout=15)

    def read(self, n):
        out = b""
        while len(out) < n:
            chunk = self.s.recv(n - len(out))
            if not chunk:
                raise ConnectionError("印表機關閉連線")
            out += chunk
        return out

    def frame(self):
        head = self.read(16)
        ln = struct.unpack_from(">I", head, 12)[0]
        body = self.read(ln) if ln else b""
        return struct.unpack_from(">H", head, 6)[0], body

    def send(self, data):
        self.s.sendall(bytes(data))

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


def negotiate(conn, sid):
    p = header(OP_SET_MAX, sid, 4) + bytes([0, 0, 0x84, 0])
    conn.send(p)
    res, _ = conn.frame()
    if res != 0:
        raise RuntimeError(f"SetMaxWriteSize 失敗 {res}")
    conn.send(header(OP_GET_MAX, sid))
    res, body = conn.frame()
    if res == 0 and len(body) >= 4:
        g = struct.unpack_from(">I", body, 0)[0]
        if 1024 <= g <= 1 << 20:
            return g
    return MAX_CHUNK


def read_status(conn, sid):
    conn.send(header(OP_READ, sid))
    _, b = conn.frame()
    u32 = lambda o: struct.unpack_from("<I", b, o)[0] if o + 4 <= len(b) else 0
    req = u32(0x10)
    return {
        "state": b[0x12] if len(b) > 0x12 else -1,
        "dev": struct.unpack_from("<H", b, 8)[0] if len(b) >= 10 else 0,
        "error": u32(0x0C), "req": req, "oc": (req | 0xFF) == 0x2FFFF,
        "retry": u32(0x14), "off": u32(0x18), "len": u32(0x1C), "raw": b,
    }


def write_data(conn, sid, payload, maxw):
    sent, stalls = 0, 0
    while sent < len(payload):
        piece = payload[sent:sent + maxw]
        conn.send(header(OP_WRITE, sid, len(piece)) + piece)
        res, body = conn.frame()
        if res != 0:
            raise RuntimeError(f"資料被拒 result={res}")
        consumed = struct.unpack_from(">I", body, 0)[0] if len(body) >= 4 else len(piece)
        if consumed > 0:
            sent += min(consumed, len(piece))
            stalls = 0
        else:
            stalls += 1
            if stalls > 50:
                raise RuntimeError("印表機不再收資料")
            time.sleep(0.05)


def make_start_spool(jpeg_size, size, bordered, optimize, finish):
    b = bytearray(192)
    struct.pack_into("<HHI", b, 0, 0, CODE_START_SPOOL, 192)
    struct.pack_into("<HH", b, 12, 1, SIZES[size])
    b[16] = 1                       # overcoatSetting（each page）
    b[17] = 3 if optimize else 2    # imageOptimize ON=3 / OFF=2
    b[18] = 3 if bordered else 2    # border：2 無邊框 / 3 有邊框
    b[19] = finish                  # printFinish：0 AUTO / 2 GLOSSY / 其他值未知
    struct.pack_into("<I", b, 32, jpeg_size)
    return b


def make_flags(bordered):
    """CP900 世代（selphy_go）的開始指令：64 bytes，長度@4、1@0x0c、邊框@0x12（2 無邊框／3 有邊框）。"""
    b = bytearray(0x40)
    struct.pack_into("<I", b, 0x04, 0x40)
    struct.pack_into("<I", b, 0x0C, 1)
    struct.pack_into("<I", b, 0x12, 3 if bordered else 2)
    return b


def make_simple(code):
    b = bytearray(64)
    struct.pack_into("<HHI", b, 0, 0, code, 64)
    return b


def make_transfer(jpeg, off, length, w, h):
    hd = bytearray(0x68)
    struct.pack_into("<I", hd, 0x02, 1)
    struct.pack_into("<I", hd, 0x04, length + 0x68)
    struct.pack_into("<I", hd, 0x0C, 1)
    struct.pack_into("<I", hd, 0x14, len(jpeg))
    struct.pack_into("<I", hd, 0x18, w)
    struct.pack_into("<I", hd, 0x1C, h)
    struct.pack_into("<I", hd, 0x60, off)
    struct.pack_into("<I", hd, 0x64, length)
    data = jpeg[off:off + length].ljust(length, b"\0")
    return hd + data


def add_exif_date(jpeg, dt):
    """在 JPEG 開頭插入最小 EXIF（DateTime＋DateTimeOriginal），dt 格式 'YYYY:MM:DD HH:MM:SS'。
    原本就有 EXIF（APP1 Exif）就不動。"""
    if jpeg[:2] != b"\xff\xd8":
        raise ValueError("不是 JPEG")
    if b"Exif\0\0" in jpeg[:4096]:
        return jpeg
    val = dt.encode("ascii")[:19].ljust(19, b"0") + b"\0"            # 20 bytes
    # TIFF（little-endian）：header 8 + IFD0(2 entries) 2+24+4=30 → 38；ExifIFD(1 entry) 2+12+4=18 → 56；字串放後面
    ifd0_off, exif_off, s1_off = 8, 38, 56
    s2_off = s1_off + 20
    t = bytearray(b"II*\0" + struct.pack("<I", ifd0_off))
    t += struct.pack("<H", 2)
    t += struct.pack("<HHII", 0x0132, 2, 20, s1_off)                  # DateTime
    t += struct.pack("<HHII", 0x8769, 4, 1, exif_off)                 # ExifIFD pointer
    t += struct.pack("<I", 0)
    t += struct.pack("<H", 1)
    t += struct.pack("<HHII", 0x9003, 2, 20, s2_off)                  # DateTimeOriginal
    t += struct.pack("<I", 0)
    t += val + val
    app1 = b"Exif\0\0" + bytes(t)
    seg = b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1
    return jpeg[:2] + seg + jpeg[2:]


def connect(host):
    sid, port = session_start(host)
    log(f"session={sid} data port={port}")
    for _ in range(12):
        try:
            return sid, Conn(host, port)
        except OSError:
            time.sleep(0.3)
    session_end(host, sid)
    raise RuntimeError("印表機沒有開資料埠")


def handshake(host):
    sid, conn = connect(host)
    try:
        log(f"max write size={negotiate(conn, sid)}")
        st = read_status(conn, sid)
        log(f"status state={st['state']} dev=0x{st['dev']:x} error=0x{st['error']:x} raw={st['raw'][:32].hex(' ')}")
    finally:
        conn.close()
        session_end(host, sid)


def print_jpeg(host, jpeg, w, h, size="L", bordered=False, optimize=False, finish=2, legacy=False):
    sid, conn = connect(host)
    try:
        maxw = negotiate(conn, sid)
        deadline, last_key, last_pass, served, transferred, idle = time.time() + 300, "", 0, "", False, 0
        start_sent_at, last_raw, oc_since = 0.0, b"", 0.0
        while time.time() < deadline:
            st = read_status(conn, sid)
            err = st["error"]
            if err and err not in WAITABLE:
                conn.send(header(OP_WRITE, sid, 64) + make_simple(CODE_CANCEL))
                raise RuntimeError(f"印表機錯誤：{ERRORS.get(err, hex(err))}")
            if st["dev"] == 0xFFFF:
                raise RuntimeError("印表機嚴重錯誤")
            if st["dev"] == 0xFF0D and last_pass != 0xFF0D:
                write_data(conn, sid, make_simple(CODE_RESUME), maxw)
            if (st["dev"] & 0xFF00) == 0xFF00 and st["dev"] != last_pass:
                last_pass = st["dev"]
                log(f"列印中：{PASSES.get(st['dev'], hex(st['dev']))}")
            key = f"{st['state']}:{st['req']:x}:{st['retry']}:{st['off']}:{st['len']}:{st['dev']:x}:{err:x}"
            if key != last_key:
                log(f"status {key}")
                last_key = key
            if st["raw"] != last_raw:
                log(f"  raw {st['raw'].hex(' ')}")
                last_raw = st["raw"]
            s = st["state"]
            if s == 0x01:
                # 開始指令只送一次；印表機 3 秒內沒反應才重送，避免洗版
                if time.time() - start_sent_at > 3:
                    if legacy:
                        write_data(conn, sid, make_flags(bordered), maxw)
                        log("flags（舊版開始指令）送出")
                    else:
                        write_data(conn, sid, make_start_spool(len(jpeg), size, bordered, optimize, finish), maxw)
                        log("startSpool 送出")
                    start_sent_at = time.time()
                time.sleep(0.3)
            elif s == 0x07:
                write_data(conn, sid, make_simple(CODE_EXECUTE), maxw)
                log("executeSpoolPrint 送出")
            elif s == 0x02:
                if st["oc"]:
                    log(f"★ 印表機要保護膜資料 off={st['off']} len={st['len']}（尚未實作）")
                    oc_since = oc_since or time.time()
                    if time.time() - oc_since > 30:   # 不要讓紙卡在機器裡乾等
                        write_data(conn, sid, make_simple(CODE_CANCEL), maxw)
                        raise RuntimeError("印表機要保護膜資料 30 秒沒給，已取消")
                    time.sleep(0.3)
                    continue
                rk = f"{st['req']}:{st['retry']}:{st['off']}:{st['len']}"
                if rk == served:
                    time.sleep(0.1)
                    continue
                off, ln = st["off"], st["len"]
                if not (0 < ln <= len(jpeg)):
                    ln = max(0, len(jpeg) - off)
                write_data(conn, sid, make_transfer(jpeg, off, ln, w, h), maxw)
                served = rk
                transferred = transferred or off + ln >= len(jpeg)
                log(f"送出資料 off={off} len={ln}")
            elif s == 0x03:
                try:
                    write_data(conn, sid, make_simple(CODE_END), maxw)
                except Exception:
                    pass
                log("完成（endPrint）")
                return True
            elif s == 0x04:
                raise RuntimeError(f"印表機回報錯誤狀態 {ERRORS.get(err, hex(err))}")
            else:
                if transferred and st["dev"] == 0:
                    idle = idle or time.time()
                    if time.time() - idle > 60:
                        log("傳完 60 秒沒收到結束訊號")
                        return False
                else:
                    idle = 0
                time.sleep(0.3)
        raise TimeoutError("超過 5 分鐘")
    except Exception as e:
        log(f"✘ {type(e).__name__}: {e}")
        raise
    finally:
        conn.close()
        session_end(host, sid)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("jpeg", nargs="?")
    ap.add_argument("--handshake", action="store_true")
    ap.add_argument("--size", default="L", choices=SIZES)
    ap.add_argument("--finish", type=int, default=2)
    ap.add_argument("--bordered", action="store_true")
    ap.add_argument("--optimize", action="store_true")
    ap.add_argument("--legacy", action="store_true", help="用 CP900 世代 flags 開始指令")
    ap.add_argument("--log", help="完整紀錄存檔路徑")
    ap.add_argument("--w", type=int, required=False)
    ap.add_argument("--h", type=int, required=False)
    a = ap.parse_args()
    LOG_FILE = a.log
    if a.handshake:
        handshake(a.host)
        sys.exit(0)
    data = open(a.jpeg, "rb").read()
    ok = print_jpeg(a.host, data, a.w, a.h, a.size, a.bordered, a.optimize, a.finish, a.legacy)
    sys.exit(0 if ok else 1)
