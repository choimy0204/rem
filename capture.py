"""디스플레이 캡처.

1순위 DXGI Desktop Duplication (dxcam, GPU) - 수 ms, 변경 없으면 None 을 돌려줘 변경 감지가 공짜
폴백     mss (GDI BitBlt)                  - 느리지만(~40ms) 어디서나 동작
DXGI 가 안 되는 환경(원격 데스크톱 세션, 일부 가상 어댑터 등)은 자동으로 mss 로 전환한다.
"""
import threading
import zlib

import mss

try:
    import dxcam
    _HAVE_DXCAM = True
except Exception:  # 미설치/초기화 실패
    dxcam = None
    _HAVE_DXCAM = False

MSS = getattr(mss, "MSS", None) or mss.mss

_dx_lock = threading.Lock()
_dx_busy: set[str] = set()  # 한 디스플레이당 DXGI 사용자는 1명 (프레임이 소비형이라 공유 불가)
_dx_sig = None


def _factory():
    return getattr(dxcam, "__factory", None)


def _dx_reset_if_changed(sig) -> None:
    """디스플레이 구성이 바뀌었으면 dxcam 의 (캐시된) 장치/출력 목록을 다시 만든다."""
    global _dx_sig
    if _dx_sig == sig:
        return
    if _dx_busy:  # 누군가 쓰는 중이면 건드리지 않는다
        return
    try:
        dxcam.Singleton._instances.clear()
        setattr(dxcam, "__factory", dxcam.DXFactory())
    except Exception:
        pass
    _dx_sig = sig


def _dx_acquire(display: dict, sig):
    """해당 디스플레이용 dxcam 카메라. 안 되면 None."""
    if not _HAVE_DXCAM:
        return None
    name = display["name"]
    with _dx_lock:
        if name in _dx_busy:
            return None
        try:
            _dx_reset_if_changed(sig)
            f = _factory()
            for di, outs in enumerate(f.outputs):
                for oi, out in enumerate(outs):
                    if out.devicename == name:
                        cam = dxcam.create(device_idx=di, output_idx=oi, output_color="BGRA")
                        if cam is None:
                            return None
                        _dx_busy.add(name)
                        return cam
        except Exception:
            return None
    return None


def _dx_release(name: str, cam) -> None:
    with _dx_lock:
        try:
            cam.release()
        except Exception:
            pass
        _dx_busy.discard(name)


class Capture:
    """클라이언트 1명이 선택한 디스플레이 하나를 캡처한다."""

    def __init__(self):
        self.d: dict | None = None
        self.cam = None
        self._sct = None
        self._last_crc = None
        self.backend = "-"

    def open(self, display: dict, all_displays: list[dict]) -> None:
        self.close()
        self.d = display
        sig = tuple((x["name"], x["left"], x["top"], x["width"], x["height"]) for x in all_displays)
        self.cam = _dx_acquire(display, sig)
        self.backend = "dxgi" if self.cam else "mss"
        self._last_crc = None

    def close(self) -> None:
        if self.cam is not None and self.d is not None:
            _dx_release(self.d["name"], self.cam)
        self.cam = None
        if self._sct is not None:
            self._sct.close()
            self._sct = None

    def _fallback(self) -> None:
        if self.cam is not None:
            _dx_release(self.d["name"], self.cam)
            self.cam = None
            self.backend = "mss"

    def _mss_grab(self):
        if self._sct is None:
            self._sct = MSS()
        d = self.d
        shot = self._sct.grab({"left": d["left"], "top": d["top"],
                               "width": d["width"], "height": d["height"]})
        return shot.bgra, shot.size

    def grab(self, force: bool = False):
        """(BGRA 버퍼, (w, h)) 또는 None(= 마지막 프레임과 같음). force=True 면 항상 프레임을 돌려준다."""
        d = self.d
        if self.cam is not None and not force:
            try:
                frame = self.cam.grab()
            except Exception:
                self._fallback()
            else:
                if frame is None:
                    return None
                h, w = frame.shape[:2]
                if (w, h) == (d["width"], d["height"]):
                    return frame, (w, h)
                self._fallback()  # 해상도가 바뀐 경우 등
        buf, size = self._mss_grab()
        if force:
            return buf, size
        crc = zlib.adler32(buf)
        if crc == self._last_crc:
            return None
        self._last_crc = crc
        return buf, size
