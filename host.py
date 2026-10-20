"""원격 PC에서 실행하는 호스트.

- 연결된 디스플레이 목록(가상 디스플레이 포함)을 뷰어에 알려주고
- 뷰어가 선택한 디스플레이 '하나'만 JPEG 로 스트리밍한다. (보기 전용, 입력 제어 없음)

사용:  python host.py [--password 1234] [--port 50505] [--console]
트레이 아이콘으로 백그라운드 실행 (--console 이면 콘솔 창). 아이콘 우클릭 > 접속 정보 보기 / 종료
"""
import argparse
import base64
import ctypes
import hmac
import json
import os
import secrets
import select
import socket
import sys
import threading
import time
import traceback
from ctypes import wintypes
from io import BytesIO

from PIL import Image, ImageDraw

import cursor
import display_mode
import protocol as P
from capture import MSS, Capture

WINDOWED = sys.stdout is None  # 콘솔 없는 실행 (pythonw / --noconsole exe): 출력이 사라진다
MAX_CLIENTS = 3
KEEPALIVE_SEC = 2.0
MAX_FPS = 144  # 실제 전송 fps 는 디스플레이 주사율과 인코딩 속도에 맞춰 이 값 이하로 정해진다
JPEG_QUALITY = 75  # 90 -> 75: 인코딩 시간·프레임 크기가 대략 절반
ACK_TIMEOUT_SEC = 1.0  # 이 시간 동안 ACK 가 없으면 흐름 제어를 풀고 계속 보낸다 (ACK 를 안 보내는 뷰어 대비)
STATS_SEC = 5.0


def set_dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def set_high_res_timer() -> None:
    """Windows 기본 타이머 해상도(15.6ms)로는 144Hz(6.9ms) 간격을 못 맞추므로 1ms 로 올린다."""
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
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


def encode_frame(bgra, size, scale: float, quality: int) -> bytes:
    img = Image.frombuffer("RGB", size, bgra, "raw", "BGRX", 0, 1)
    if scale < 0.999:
        w = max(1, int(img.width * scale))
        h = max(1, int(img.height * scale))
        img = img.resize((w, h), Image.BILINEAR)
    buf = BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def cursor_shape_msg(hcursor: int) -> dict:
    icon, hx, hy = cursor.shape(hcursor)
    buf = BytesIO()
    icon.save(buf, "PNG")
    return {"type": "cursor_shape", "id": hcursor, "hx": hx, "hy": hy,
            "png": base64.b64encode(buf.getvalue()).decode("ascii")}


class StreamStats:
    """실제로 나가는 fps / 인코딩 시간 / 프레임 크기를 STATS_SEC 마다 로그로 남긴다 (병목 확인용)."""

    def __init__(self, ip: str):
        self.ip = ip
        self.reset()

    def reset(self) -> None:
        self.start = time.monotonic()
        self.frames = self.bytes = 0
        self.encode_sec = 0.0

    def add(self, size: int, encode_sec: float) -> None:
        self.frames += 1
        self.bytes += size
        self.encode_sec += encode_sec
        elapsed = time.monotonic() - self.start
        if elapsed >= STATS_SEC:
            print(f"[{self.ip}] 실제 {self.frames / elapsed:.1f}fps · 인코딩 평균 {self.encode_sec / self.frames * 1000:.1f}ms"
                  f" · 프레임 평균 {self.bytes / self.frames / 1024:.0f}KB")
            self.reset()


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
        if not hmac.compare_digest(proof, P.make_proof(password, nonce)):  # 호스트 콘솔에 이 메시지가 안 뜨면 다른 프로세스가 응답한 것
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
        inflight = 0  # 보냈지만 뷰어가 아직 ACK 하지 않은 프레임 수
        cap = Capture()
        last_cur = None
        sent_shapes: set[int] = set()  # 뷰어에 이미 보낸 커서 모양 (핸들)
        stats = StreamStats(ip)
        try:
            while True:
                now = time.monotonic()
                if index is None:
                    wait = 1.0
                elif inflight >= P.MAX_INFLIGHT:
                    wait = 0.05  # ACK 가 오면 select 가 바로 깨운다
                else:
                    wait = max(0.0, next_t - now)
                readable, _, _ = select.select([conn], [], [], wait)
                if readable:
                    conn.settimeout(10)
                    mtype, payload = P.recv_msg(conn)
                    if mtype == P.T_ACK:
                        inflight = max(0, inflight - 1)
                        continue
                    cmd = json.loads(payload.decode("utf-8"))
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
                            inflight = 0
                            last_cur = None
                            P.send_json(conn, {"type": "selected", "display": displays[i]})
                            print(f"[{ip}] {displays[i]['name']} 보기 시작 ({cap.backend}, 목표 {fps}fps)")
                        else:
                            P.send_json(conn, {"type": "error", "message": "잘못된 디스플레이 번호"})
                    elif c == "stop":
                        cap.close()
                        index = None
                    elif c == "bye":
                        return
                if index is None:
                    continue

                conn.settimeout(15)
                # 커서는 영상과 따로, 바뀔 때만 보낸다 (프레임 인코딩을 기다리지 않으므로 빠르다)
                cur = cursor.state(cap.d)
                if cur != last_cur:
                    if cur and cur[2] not in sent_shapes:
                        P.send_json(conn, cursor_shape_msg(cur[2]))
                        sent_shapes.add(cur[2])
                    P.send_json(conn, {"type": "cursor", "pos": cur})
                    last_cur = cur

                now = time.monotonic()
                if inflight and now - last_sent > ACK_TIMEOUT_SEC:
                    inflight = 0
                if inflight >= P.MAX_INFLIGHT or now < next_t:
                    continue
                # 변경 없음(None)이어도 첫 프레임/킵얼라이브 시점에는 강제로 한 장 보낸다
                frame = cap.grab(force=(now - last_sent > KEEPALIVE_SEC))
                if frame is not None:
                    data = encode_frame(frame[0], frame[1], scale, quality)
                    P.send_msg(conn, P.T_FRAME, data)
                    inflight += 1
                    last_sent = time.monotonic()
                    stats.add(len(data), last_sent - now)
                # 고정 간격을 유지한다 (인코딩이 늦었을 때만 밀린 만큼 건너뛰고 지금부터 다시 시작)
                next_t = max(next_t + 1.0 / fps, now)
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


def info_text(port: int, password: str) -> str:
    lines = [f"접속 주소 : {', '.join(local_ips()) or '(IP 확인 불가)'}  포트 {port}",
             f"비밀번호  : {password}",
             "연결된 디스플레이:"]
    for d in list_displays():
        star = " (주)" if d["primary"] else ""
        lines.append(f"  [{d['index']}] {d['name']}{star} {d['width']}x{d['height']} {d['hz']}Hz @ ({d['left']},{d['top']})")
    return "\n".join(lines)


def message_box(text: str, title: str = "VirtualMonitor 호스트") -> None:
    ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)  # MB_ICONINFORMATION


def redirect_output_to_log() -> str:
    """콘솔이 없을 때 print/오류 출력을 %LOCALAPPDATA%\\VirtualMonitor\\host.log 로 남긴다."""
    folder = os.path.join(os.environ.get("LOCALAPPDATA", "."), "VirtualMonitor")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "host.log")
    sys.stdout = sys.stderr = open(path, "a", encoding="utf-8", buffering=1)
    print(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} 호스트 시작 ====")
    return path


def serve(srv: socket.socket, password: str) -> None:
    active = threading.BoundedSemaphore(MAX_CLIENTS)
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


def tray_icon_image() -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    blue = (37, 99, 235, 255)
    d.rounded_rectangle((4, 8, 60, 44), 6, fill=blue)  # 모니터
    d.rectangle((9, 13, 55, 39), fill=(219, 234, 254, 255))  # 화면
    d.rectangle((28, 44, 36, 52), fill=blue)  # 목
    d.rectangle((18, 52, 46, 57), fill=blue)  # 받침
    return img


def run_tray(port: int, password: str) -> None:
    """트레이 아이콘을 띄우고 메뉴의 [종료] 를 누를 때까지 대기한다 (서버는 다른 스레드에서 돈다)."""
    import pystray

    def show(_icon=None, _item=None):
        threading.Thread(target=message_box, args=(info_text(port, password),), daemon=True).start()

    icon = pystray.Icon(
        "VirtualMonitorHost", tray_icon_image(), f"VirtualMonitor 호스트 ({', '.join(local_ips())})",
        menu=pystray.Menu(pystray.MenuItem("접속 정보 보기", show, default=True),
                          pystray.MenuItem("종료", lambda icon, _item: icon.stop())))

    def setup(icon):
        icon.visible = True
        try:
            icon.notify(f"주소 {', '.join(local_ips())}\n비밀번호 {password}", "VirtualMonitor 호스트 실행 중")
        except Exception:  # 알림이 실패해도 아이콘/서버는 계속 돌아야 한다
            traceback.print_exc()

    icon.run(setup)


def main() -> None:
    ap = argparse.ArgumentParser(description="VirtualMonitor 호스트")
    ap.add_argument("--port", type=int, default=P.PORT)
    ap.add_argument("--password", help="미지정 시 실행할 때마다 임의 생성")
    ap.add_argument("--console", action="store_true", help="트레이 아이콘 대신 콘솔 창에서 실행")
    args = ap.parse_args()

    try:
        import pystray  # noqa: F401
        tray = not args.console
    except Exception:  # 미설치 -> 콘솔 모드
        tray = False

    def fail(msg: str):
        print(msg)
        if tray or WINDOWED:  # 콘솔이 없으면 메시지 상자로 알린다
            message_box(msg)
        raise SystemExit(msg)

    if WINDOWED and not tray:
        fail("pystray 가 없어 트레이 아이콘을 만들 수 없습니다. 'pip install -r requirements.txt' 후 다시 실행하세요.")

    set_dpi_aware()
    set_high_res_timer()
    password = args.password or "".join(secrets.choice("23456789") for _ in range(6))

    # Windows 는 SO_REUSEADDR 로 같은 포트를 여러 프로세스가 같이 열 수 있다 -> 예전 호스트가 남아 있으면
    # 접속이 그쪽(다른 비밀번호)으로 가서 '인증 실패'가 난다. 이미 떠 있으면 시작하지 않는다.
    try:
        socket.create_connection(("127.0.0.1", args.port), timeout=0.5).close()
    except OSError:
        pass
    else:
        fail(f"이미 포트 {args.port} 에서 호스트가 실행 중입니다. 먼저 종료하세요 (3_호스트_종료.bat).")

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("0.0.0.0", args.port))
    except OSError as e:
        fail(f"포트 {args.port} 를 열 수 없습니다: {e}")
    srv.listen(5)
    threading.Thread(target=discovery_responder, args=(args.port,), daemon=True).start()

    if tray:
        threading.Thread(target=serve, args=(srv, password), daemon=True).start()
        run_tray(args.port, password)
        return

    print("=" * 50)
    print(" VirtualMonitor 호스트 실행 중")
    for line in info_text(args.port, password).splitlines():
        print(" " + line)
    print(" 종료: Ctrl+C")
    print("=" * 50)
    try:
        serve(srv, password)
    except KeyboardInterrupt:
        print("\n종료합니다.")


if __name__ == "__main__":
    log_path = redirect_output_to_log() if WINDOWED else None
    try:
        main()
    except Exception:
        traceback.print_exc()
        if WINDOWED:
            message_box(f"호스트가 오류로 종료되었습니다.\n\n{traceback.format_exc()[-600:]}\n로그: {log_path}")
        raise
