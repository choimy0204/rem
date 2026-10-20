"""원격 PC에서 실행하는 호스트.

- 연결된 디스플레이 목록(가상 디스플레이 포함)을 뷰어에 알려주고
- 뷰어가 선택한 디스플레이 '하나'만 JPEG 로 스트리밍한다. (보기 전용, 입력 제어 없음)

사용:  python host.py [--password 1234] [--port 50505]
"""
import argparse
import ctypes
import hmac
import json
import os
import secrets
import select
import socket
import threading
import time
from ctypes import wintypes
from io import BytesIO

from PIL import Image

import cursor
import display_mode
import protocol as P
from capture import MSS, Capture

MAX_CLIENTS = 3
KEEPALIVE_SEC = 2.0
MAX_FPS = 144  # 실제 전송 fps 는 디스플레이 주사율과 인코딩 속도에 맞춰 이 값 이하로 정해진다
JPEG_QUALITY = 90


def set_dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _win_monitors() -> dict:
    """{(left, top, right, bottom): (장치명, 주 모니터 여부)}"""

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    result = {}
    try:
        user32 = ctypes.windll.user32
        proc_t = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
            ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

        def cb(hmon, _hdc, _rect, _lp):
            info = MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(MONITORINFOEXW)
            if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                r = info.rcMonitor
                result[(r.left, r.top, r.right, r.bottom)] = (
                    info.szDevice, bool(info.dwFlags & 1))
            return True

        user32.EnumDisplayMonitors(None, None, proc_t(cb), 0)
    except Exception:
        pass
    return result


def list_displays() -> list[dict]:
    """현재 연결된 디스플레이 정보를 가져온다."""
    extra = _win_monitors()
    displays = []
    with MSS() as sct:
        for i, m in enumerate(sct.monitors[1:]):  # [0] 은 전체 가상 화면
            key = (m["left"], m["top"], m["left"] + m["width"], m["top"] + m["height"])
            name, primary = extra.get(key, (f"DISPLAY{i + 1}", m["left"] == 0 and m["top"] == 0))
            mode = display_mode.current_mode(name)
            displays.append({
                "index": i,
                "name": name,
                "primary": primary,
                "hz": mode[2] if mode else 60,
                "left": m["left"], "top": m["top"],
                "width": m["width"], "height": m["height"],
            })
    return displays


def local_ips() -> list[str]:
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


def encode_frame(bgra, size, scale: float, quality: int, cur=None) -> bytes:
    img = Image.frombuffer("RGB", size, bgra, "raw", "BGRX", 0, 1)
    cursor.draw(img, cur)  # 캡처에는 커서가 없으므로 직접 그린다
    if scale < 0.999:
        w = max(1, int(img.width * scale))
        h = max(1, int(img.height * scale))
        img = img.resize((w, h), Image.BILINEAR)
    buf = BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def handle_client(conn: socket.socket, addr, password: str) -> None:
    ip = addr[0]
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    try:
        # ---- 인증 ----
        nonce = os.urandom(16)
        conn.settimeout(10)
        P.send_json(conn, {"type": "hello", "version": P.VERSION,
                           "host": socket.gethostname(), "nonce": nonce.hex()})
        msg = P.recv_json(conn)
        try:
            proof = bytes.fromhex(msg.get("proof", ""))
        except ValueError:
            proof = b""
        if not hmac.compare_digest(proof, P.make_proof(password, nonce)):
            time.sleep(1.0)  # 무차별 대입 지연
            P.send_json(conn, {"type": "error", "message": "인증 실패 (비밀번호 확인)"})
            print(f"[{ip}] 인증 실패")
            return
        print(f"[{ip}] 접속됨")
        P.send_json(conn, {"type": "displays", "displays": list_displays()})

        # ---- 스트리밍 루프 ----
        displays = list_displays()
        index = None
        fps, quality, scale = MAX_FPS, JPEG_QUALITY, 1.0  # 항상 원본 해상도 + 최고 프레임 (fps 는 선택한 디스플레이 주사율)
        next_t = 0.0
        last_sent = 0.0
        cap = Capture()
        last_frame = None  # 마지막으로 캡처한 화면 (커서만 움직였을 때 재사용)
        last_cur = None
        try:
            while True:
                now = time.monotonic()
                wait = max(0.0, next_t - now) if index is not None else 1.0
                readable, _, _ = select.select([conn], [], [], wait)
                if readable:
                    conn.settimeout(10)
                    cmd = P.recv_json(conn)
                    c = cmd.get("cmd")
                    if c == "refresh":
                        cap.close()
                        displays = list_displays()
                        index = None
                        P.send_json(conn, {"type": "displays", "displays": displays})
                    elif c == "select":
                        cap.close()
                        displays = list_displays()
                        i = int(cmd.get("index", 0))
                        if 0 <= i < len(displays):
                            index = i
                            cap.open(displays[i], displays)
                            fps = min(MAX_FPS, max(1, displays[i]["hz"]))
                            last_sent = 0.0  # 첫 프레임은 즉시 보낸다
                            next_t = 0.0
                            last_frame = last_cur = None
                            P.send_json(conn, {"type": "selected", "display": displays[i]})
                            print(f"[{ip}] {displays[i]['name']} 보기 시작 ({cap.backend}, 목표 {fps}fps)")
                        else:
                            P.send_json(conn, {"type": "error", "message": "잘못된 디스플레이 번호"})
                    elif c == "stop":
                        cap.close()
                        index = None
                    elif c == "bye":
                        return
                if index is not None and time.monotonic() >= next_t:
                    t = time.monotonic()
                    # 변경 없음(None)이어도 첫 프레임/킵얼라이브 시점에는 강제로 한 장 보낸다
                    frame = cap.grab(force=(t - last_sent > KEEPALIVE_SEC))
                    cur = cursor.state(cap.d)
                    if frame is not None:
                        buf = frame[0]
                        if not isinstance(buf, (bytes, bytearray)):
                            buf = buf.tobytes()  # dxcam 프레임은 재사용될 수 있어 복사해 둔다
                        last_frame = (buf, frame[1])
                    elif cur != last_cur and last_frame is not None:
                        frame = last_frame  # 화면은 그대로, 커서만 움직임
                    if frame is not None:
                        conn.settimeout(15)
                        P.send_msg(conn, P.T_FRAME, encode_frame(frame[0], frame[1], scale, quality, cur))
                        last_cur = cur
                        last_sent = time.monotonic()
                    next_t = t + 1.0 / fps
        finally:
            cap.close()
    except (ConnectionError, OSError, ValueError, json.JSONDecodeError):
        pass
    finally:
        print(f"[{ip}] 연결 종료")
        try:
            conn.close()
        except OSError:
            pass


def discovery_responder(port: int) -> None:
    """같은 네트워크의 뷰어가 호스트를 찾을 수 있도록 UDP 브로드캐스트에 응답한다."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", P.DISCOVERY_PORT))
    except OSError as e:
        print(f"(자동 검색 비활성: {e})")
        return
    reply = json.dumps({"name": socket.gethostname(), "port": port}).encode()
    while True:
        try:
            data, src = s.recvfrom(256)
            if data == P.DISCOVERY_REQUEST:
                s.sendto(reply, src)
        except OSError:
            return


def main() -> None:
    ap = argparse.ArgumentParser(description="VirtualMonitor 호스트")
    ap.add_argument("--port", type=int, default=P.PORT)
    ap.add_argument("--password", help="미지정 시 실행할 때마다 임의 생성")
    args = ap.parse_args()

    set_dpi_aware()
    password = args.password or "".join(secrets.choice("23456789") for _ in range(6))

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", args.port))
    srv.listen(5)
    threading.Thread(target=discovery_responder, args=(args.port,), daemon=True).start()

    print("=" * 50)
    print(" VirtualMonitor 호스트 실행 중")
    print(f" 접속 주소 : {', '.join(local_ips()) or '(IP 확인 불가)'}  포트 {args.port}")
    print(f" 비밀번호  : {password}")
    print(" 연결된 디스플레이:")
    for d in list_displays():
        star = " (주)" if d["primary"] else ""
        print(f"   [{d['index']}] {d['name']}{star} {d['width']}x{d['height']} {d['hz']}Hz @ ({d['left']},{d['top']})")
    print(" 종료: Ctrl+C")
    print("=" * 50)

    active = threading.BoundedSemaphore(MAX_CLIENTS)
    try:
        while True:
            conn, addr = srv.accept()
            if not active.acquire(blocking=False):
                conn.close()
                continue

            def run(c=conn, a=addr):
                try:
                    handle_client(c, a, password)
                finally:
                    active.release()

            threading.Thread(target=run, daemon=True).start()
    except KeyboardInterrupt:
        print("\n종료합니다.")


if __name__ == "__main__":
    main()
