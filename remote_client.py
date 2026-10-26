"""뷰어 쪽 네트워크 클라이언트 (GUI 없음).

이벤트는 queue 로 전달된다: (client, 종류, 데이터)
  connected(hello, displays) / displays(list) / selected(display) / error(msg) / closed(None)
프레임은 최신 것 하나만 보관한다 (느린 디코딩/UI 가 프레임을 쌓지 않도록). 받는 즉시 호스트에 ACK 를 보내고,
JPEG 디코딩·축소는 별도 스레드에서 한다. 커서는 영상과 따로 받아서 UI 가 위에 그린다.
지연 진단: 프레임마다 단계별 시간(info)을 같이 돌려주고, 1초마다 핑으로 호스트와의 시계 차이/왕복 시간을 잰다.
"""
import base64
import json
import queue
import socket
import threading
import time
from collections import deque
from io import BytesIO

from PIL import Image

import protocol as P


def fit_size(src: tuple[int, int], box: tuple[int, int]) -> tuple[int, int]:
    """src 를 비율을 유지한 채 box 안에 최대한 크게 넣은 크기."""
    s = min(box[0] / src[0], box[1] / src[1])
    return max(1, int(src[0] * s)), max(1, int(src[1] * s))


class RemoteClient:
    def __init__(self, events: queue.Queue):
        self.events = events
        self.sock: socket.socket | None = None
        self._closed = False
        self._send_lock = threading.Lock()  # 수신 스레드(ACK)와 UI 스레드(명령)가 같은 소켓에 쓴다
        self._lock = threading.Lock()
        self._latest: tuple[Image.Image, Image.Image, dict] | None = None  # (원본, 보기 창 크기에 맞춘 것, 진단 정보)
        self._seq = 0
        self._cursor = None  # (x, y, 모양 id) 또는 None
        self._shapes: dict[int, tuple[Image.Image, int, int]] = {}  # 모양 id -> (RGBA 이미지, 핫스팟 x, y)
        self.view_size: tuple[int, int] | None = None  # 보기 창 크기. 디코딩 스레드가 미리 이 크기로 줄여 둔다
        self._pending: tuple[bytes, dict] | None = None  # 아직 디코딩 안 한 가장 최신 (JPEG, 진단 정보)
        self._cond = threading.Condition()
        self._finished = False
        self._stopped = threading.Event()
        self._clock_samples: deque[tuple[float, float]] = deque(maxlen=10)  # (핑 왕복 초, 호스트-뷰어 시계 차이)
        self.clock_offset: float | None = None  # 호스트 monotonic - 뷰어 monotonic (왕복이 가장 짧았던 핑 기준)
        self.ping_ms: float | None = None  # 제어 메시지 왕복. 프레임이 밀려 있으면 같이 커진다

    # ---- 프레임 ----
    def take_frame(self, last_seq: int):
        """새 프레임이 있으면 (seq, 원본, 창 크기에 맞춘 것, 진단 정보), 없으면 None."""
        with self._lock:
            if self._latest is None or self._seq == last_seq:
                return None
            return (self._seq, *self._latest)

    def host_latency_ms(self, info: dict) -> float | None:
        """호스트가 이 프레임을 캡처한 뒤 지금까지 걸린 시간 추정(ms). 시계 차이를 아직 못 쟀으면 None."""
        if self.clock_offset is None:
            return None
        return (time.monotonic() - (info["cap_ts"] - self.clock_offset)) * 1000

    def cursor_state(self):
        """(x, y, 모양 id, (RGBA 이미지, 핫스팟 x, y) 또는 None) 또는 커서가 안 보이면 None."""
        with self._lock:
            if self._cursor is None:
                return None
            return (*self._cursor, self._shapes.get(self._cursor[2]))

    def _offer_jpeg(self, item: tuple[bytes, dict] | None) -> None:
        """디코딩이 밀려 있으면 이전 프레임은 버리고 최신 것만 남긴다. (None 이면 디코딩 스레드 종료)"""
        with self._cond:
            if item is None:
                self._finished = True
            self._pending = item
            self._cond.notify()

    def _decode_loop(self) -> None:
        """JPEG 디코딩과 창 크기에 맞춘 축소를 수신 스레드/UI 스레드 밖에서 한다."""
        while True:
            with self._cond:
                while self._pending is None and not self._finished:
                    self._cond.wait()
                if self._finished:
                    return
                (jpeg, info), self._pending = self._pending, None
            start = time.monotonic()
            try:
                full = Image.open(BytesIO(jpeg))
                full.load()
            except Exception:
                continue
            fitted = self._fit(full)
            info["decode_ms"] = (time.monotonic() - start) * 1000
            info["decoded_at"] = time.monotonic()
            with self._lock:
                self._latest = (full, fitted, info)
                self._seq += 1

    def _fit(self, full: Image.Image) -> Image.Image:
        size = fit_size(full.size, self.view_size) if self.view_size else full.size
        return full if size == full.size else full.resize(size, Image.BILINEAR)

    # ---- 연결 ----
    def connect(self, host: str, port: int, password: str) -> None:
        threading.Thread(target=self._run, args=(host, port, password), daemon=True).start()

    def _emit(self, kind: str, data=None) -> None:
        self.events.put((self, kind, data))

    def _run(self, host: str, port: int, password: str) -> None:
        try:
            s = socket.create_connection((host, port), timeout=5)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.settimeout(15)
            hello = P.recv_json(s)
            if hello.get("version") != P.VERSION:
                raise RuntimeError("호스트와 뷰어의 버전이 다릅니다. 양쪽을 같은 버전으로 업데이트하세요")
            proof = P.make_proof(password.strip(), bytes.fromhex(hello["nonce"]))
            P.send_json(s, {"proof": proof.hex()})
            res = P.recv_json(s)
            if res.get("type") == "error":
                raise RuntimeError(res.get("message", "접속 거부"))
            s.settimeout(None)
            self.sock = s
            self._emit("connected", (hello, res["displays"]))
            threading.Thread(target=self._decode_loop, daemon=True).start()
            threading.Thread(target=self._ping_loop, daemon=True).start()

            while True:
                mtype, payload, recv_sec = P.recv_msg_timed(s)
                if mtype == P.T_FRAME:
                    self._send_msg(P.T_ACK, b"")  # 받는 즉시 알려서 호스트가 다음 프레임을 보내게 한다
                    cap_ts, capture_ms, encode_ms = P.FRAME_META.unpack_from(payload)
                    info = {"cap_ts": cap_ts, "capture_ms": capture_ms, "encode_ms": encode_ms,
                            "recv_ms": recv_sec * 1000, "kb": (len(payload) - P.FRAME_META.size) / 1024}
                    self._offer_jpeg((payload[P.FRAME_META.size:], info))
                else:
                    self._handle_json(json.loads(payload.decode("utf-8")))
        except Exception as e:  # 연결 실패/끊김 전부 여기로
            if not self._closed:
                self._emit("error", f"{type(e).__name__}: {e}" if not str(e) else str(e))
        finally:
            self._stopped.set()
            self._offer_jpeg(None)
            if not self._closed:
                self._emit("closed")

    def _handle_json(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "displays":
            self._emit("displays", msg["displays"])
        elif kind == "selected":
            with self._lock:
                self._cursor = None
            self._emit("selected", msg["display"])
        elif kind == "error":
            self._emit("error", msg.get("message", "오류"))
        elif kind == "pong":
            self._on_pong(msg["t"], msg["h"])
        elif kind == "cursor_shape":
            icon = Image.open(BytesIO(base64.b64decode(msg["png"]))).convert("RGBA")
            with self._lock:
                self._shapes[msg["id"]] = (icon, msg["hx"], msg["hy"])
        elif kind == "cursor":
            with self._lock:
                self._cursor = tuple(msg["pos"]) if msg["pos"] else None

    def _ping_loop(self) -> None:
        while not self._stopped.wait(1.0):
            self._send({"cmd": "ping", "t": time.monotonic()})

    def _on_pong(self, sent_at: float, host_time: float) -> None:
        rtt = time.monotonic() - sent_at
        self.ping_ms = rtt * 1000
        # 호스트 시각이 왕복의 한가운데라고 가정한다. 프레임에 막혀 늦게 온 핑은 부정확하므로 왕복이 가장 짧은 것을 쓴다.
        self._clock_samples.append((rtt, host_time - (sent_at + rtt / 2)))
        self.clock_offset = min(self._clock_samples)[1]

    # ---- 명령 ----
    def _send_msg(self, mtype: int, payload: bytes) -> None:
        if self.sock and not self._closed:
            try:
                with self._send_lock:
                    P.send_msg(self.sock, mtype, payload)
            except OSError:
                pass

    def _send(self, obj: dict) -> None:
        self._send_msg(P.T_JSON, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def select(self, index: int) -> None:
        self._send({"cmd": "select", "index": index})

    def refresh(self) -> None:
        self._send({"cmd": "refresh"})

    def stop_stream(self) -> None:
        self._send({"cmd": "stop"})

    def close(self) -> None:
        self._closed = True
        if self.sock:
            try:
                with self._send_lock:
                    P.send_json(self.sock, {"cmd": "bye"})
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass


def discover(timeout: float = 1.5) -> list[tuple[str, str, int]]:
    """같은 네트워크의 호스트를 UDP 브로드캐스트로 찾는다. [(ip, 이름, 포트)]"""
    import time

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.3)
    targets = {"255.255.255.255"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                targets.add(ip.rsplit(".", 1)[0] + ".255")
    except OSError:
        pass
    for t in targets:
        try:
            s.sendto(P.DISCOVERY_REQUEST, (t, P.DISCOVERY_PORT))
        except OSError:
            pass
    found = {}
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            data, src = s.recvfrom(512)
            info = json.loads(data.decode("utf-8"))
            found[src[0]] = (src[0], info.get("name", "?"), int(info.get("port", P.PORT)))
        except socket.timeout:
            continue
        except (OSError, ValueError):
            break
    s.close()
    return sorted(found.values())
