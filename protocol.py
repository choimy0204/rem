"""호스트/뷰어 공용 프로토콜.

메시지 = [1바이트 타입][4바이트 길이][페이로드]
  T_JSON  : UTF-8 JSON (제어 메시지)
  T_FRAME : FRAME_META(캡처 시각, 캡처/인코딩 걸린 ms) + JPEG 바이트 (선택한 디스플레이의 한 프레임, 커서 제외)
  T_ACK   : 뷰어 -> 호스트, 프레임을 하나 받았다는 신호 (페이로드 없음).
            호스트는 ACK 를 못 받은 프레임이 MAX_INFLIGHT 개를 넘으면 더 보내지 않는다 (지연이 쌓이는 것 방지)

인증: 호스트가 보낸 nonce 에 대해 뷰어가 HMAC-SHA256(password, nonce) 를 돌려준다.
(비밀번호 자체는 네트워크로 전송되지 않는다. 단, 화면 데이터는 암호화되지 않는다.)
"""
import hashlib
import hmac
import json
import socket
import struct
import time

PORT = 50505
DISCOVERY_PORT = 50506
DISCOVERY_REQUEST = b"VMON_DISCOVER"
VERSION = 3

T_JSON = 1
T_FRAME = 2
T_ACK = 3

MAX_INFLIGHT = 2  # ACK 를 기다리는 프레임 최대 개수

_HDR = struct.Struct("!BI")
FRAME_META = struct.Struct("!dff")  # 호스트 time.monotonic() 캡처 시각, 캡처 ms, 인코딩 ms (지연 진단용)
MAX_MSG = 64 * 1024 * 1024


def send_msg(sock: socket.socket, mtype: int, payload: bytes) -> None:
    sock.sendall(_HDR.pack(mtype, len(payload)) + payload)


def send_json(sock: socket.socket, obj: dict) -> None:
    send_msg(sock, T_JSON, json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("연결이 닫혔습니다")
        buf.extend(chunk)
    return bytes(buf)


def recv_msg_timed(sock: socket.socket) -> tuple[int, bytes, float]:
    """recv_msg + 헤더를 받은 뒤 본문을 다 받기까지 걸린 시간(초). 프레임 전송 시간(대역폭) 측정용."""
    mtype, length = _HDR.unpack(recv_exact(sock, _HDR.size))
    if length > MAX_MSG:
        raise ConnectionError("메시지가 너무 큽니다")
    start = time.monotonic()
    payload = recv_exact(sock, length)
    return mtype, payload, time.monotonic() - start


def recv_msg(sock: socket.socket) -> tuple[int, bytes]:
    mtype, payload, _ = recv_msg_timed(sock)
    return mtype, payload


def recv_json(sock: socket.socket) -> dict:
    mtype, payload = recv_msg(sock)
    if mtype != T_JSON:
        raise ConnectionError("JSON 메시지가 아닙니다")
    return json.loads(payload.decode("utf-8"))


def make_proof(password: str, nonce: bytes) -> bytes:
    return hmac.new(password.encode("utf-8"), nonce, hashlib.sha256).digest()
