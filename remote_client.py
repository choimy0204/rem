"""뷰어 쪽 네트워크 클라이언트 (GUI 없음).

이벤트는 queue 로 전달된다: (client, 종류, 데이터)
  connected(hello, displays) / displays(list) / selected(display) / error(msg) / closed(None)
프레임은 최신 것 하나만 latest 에 보관한다 (느린 UI 가 프레임을 쌓지 않도록).
"""
import json
import queue
import socket
import threading
from io import BytesIO

from PIL import Image

import protocol as P


class RemoteClient:
    def __init__(self, events: queue.Queue):
        self.events = events
        self.sock: socket.socket | None = None
        self._closed = False
        self._lock = threading.Lock()
        self._latest: Image.Image | None = None
        self._seq = 0

    # ---- 프레임 ----
    def take_frame(self, last_seq: int):
        """새 프레임이 있으면 (seq, image), 없으면 None."""
        with self._lock:
            if self._latest is None or self._seq == last_seq:
                return None
            return self._seq, self._latest

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
            proof = P.make_proof(password, bytes.fromhex(hello["nonce"]))
            P.send_json(s, {"proof": proof.hex()})
            res = P.recv_json(s)
            if res.get("type") == "error":
                raise RuntimeError(res.get("message", "접속 거부"))
            s.settimeout(None)
            self.sock = s
            self._emit("connected", (hello, res["displays"]))

            while True:
                mtype, payload = P.recv_msg(s)
                if mtype == P.T_FRAME:
                    img = Image.open(BytesIO(payload))
                    img.load()
                    with self._lock:
                        self._latest = img
                        self._seq += 1
                else:
                    msg = json.loads(payload.decode("utf-8"))
                    kind = msg.get("type")
                    if kind == "displays":
                        self._emit("displays", msg["displays"])
                    elif kind == "selected":
                        self._emit("selected", msg["display"])
                    elif kind == "error":
                        self._emit("error", msg.get("message", "오류"))
        except Exception as e:  # 연결 실패/끊김 전부 여기로
            if not self._closed:
                self._emit("error", f"{type(e).__name__}: {e}" if not str(e) else str(e))
        finally:
            if not self._closed:
                self._emit("closed")

    # ---- 명령 ----
    def _send(self, obj: dict) -> None:
        if self.sock and not self._closed:
            try:
                P.send_json(self.sock, obj)
            except OSError:
                pass

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
