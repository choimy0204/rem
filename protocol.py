"""호스트/뷰어 공용 프로토콜.

메시지 = [1바이트 타입][4바이트 길이][페이로드]
  T_JSON  : UTF-8 JSON (제어 메시지)
  T_FRAME : JPEG 바이트 (선택한 디스플레이의 한 프레임)

인증: 호스트가 보낸 nonce 에 대해 뷰어가 HMAC-SHA256(password, nonce) 를 돌려준다.
(비밀번호 자체는 네트워크로 전송되지 않는다. 단, 화면 데이터는 암호화되지 않는다.)
"""
import hashlib
import hmac
import json
import socket
import struct

PORT = 50505
DISCOVERY_PORT = 50506
DISCOVERY_REQUEST = b"VMON_DISCOVER"
VERSION = 1

T_JSON = 1
T_FRAME = 2

_HDR = struct.Struct("!BI")
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


def recv_msg(sock: socket.socket) -> tuple[int, bytes]:
    mtype, length = _HDR.unpack(recv_exact(sock, _HDR.size))
    if length > MAX_MSG:
        raise ConnectionError("메시지가 너무 큽니다")
    return mtype, recv_exact(sock, length)


def recv_json(sock: socket.socket) -> dict:
    mtype, payload = recv_msg(sock)
    if mtype != T_JSON:
        raise ConnectionError("JSON 메시지가 아닙니다")
    return json.loads(payload.decode("utf-8"))


def make_proof(password: str, nonce: bytes) -> bytes:
    return hmac.new(password.encode("utf-8"), nonce, hashlib.sha256).digest()
