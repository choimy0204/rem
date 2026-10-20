"""마우스 커서 전송용 도우미.

DXGI/GDI 캡처 화면에는 커서가 들어있지 않으므로, 호스트가 현재 커서 위치와 모양을 읽어 영상과 따로 뷰어에 보낸다
(영상에 합성하면 커서가 움직일 때마다 프레임을 다시 인코딩해야 해서 커서가 늦어진다).
커서 모양은 검은 배경/흰 배경에 각각 그려 알파를 복원한다 (알파 커서, 마스크 커서 모두 대응).
"""
import ctypes
from ctypes import wintypes

import numpy as np
from PIL import Image, ImageDraw

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

user32.GetCursorInfo.argtypes = [ctypes.c_void_p]
user32.GetIconInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.DrawIconEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.HANDLE,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.HANDLE, wintypes.UINT]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.restype = wintypes.HANDLE
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi32.SelectObject.restype = wintypes.HANDLE
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]

CURSOR_SHOWING = 1
DI_NORMAL = 3


class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hCursor", wintypes.HANDLE), ("ptScreenPos", wintypes.POINT)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD),
                ("hbmMask", wintypes.HANDLE), ("hbmColor", wintypes.HANDLE)]


class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG), ("bmHeight", wintypes.LONG),
                ("bmWidthBytes", wintypes.LONG), ("bmPlanes", wintypes.WORD),
                ("bmBitsPixel", wintypes.WORD), ("bmBits", ctypes.c_void_p)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


_cache: dict = {}
_FALLBACK_ARROW = [(0, 0), (0, 17), (4, 13), (7, 20), (10, 19), (7, 12), (12, 12)]


def state(display: dict):
    """디스플레이 안에 보이는 커서라면 (x, y, 핸들) (디스플레이 기준 좌표), 아니면 None."""
    try:
        ci = CURSORINFO()
        ci.cbSize = ctypes.sizeof(CURSORINFO)
        if not user32.GetCursorInfo(ctypes.byref(ci)) or not (ci.flags & CURSOR_SHOWING) or not ci.hCursor:
            return None
        x = ci.ptScreenPos.x - display["left"]
        y = ci.ptScreenPos.y - display["top"]
        if not (0 <= x < display["width"] and 0 <= y < display["height"]):
            return None
        return (x, y, int(ci.hCursor))
    except Exception:
        return None


def _bitmap_size(hbm):
    bm = BITMAP()
    gdi32.GetObjectW(hbm, ctypes.sizeof(BITMAP), ctypes.byref(bm))
    return bm.bmWidth, bm.bmHeight


def _render(hcursor):
    """(RGBA 이미지, 반전 마스크 또는 None, 핫스팟 x, 핫스팟 y) 또는 None."""
    ii = ICONINFO()
    if not user32.GetIconInfo(hcursor, ctypes.byref(ii)):
        return None
    hdc = memdc = dib = old = None
    try:
        if ii.hbmColor:
            w, h = _bitmap_size(ii.hbmColor)
        else:  # 흑백 커서: 마스크 비트맵이 AND+XOR 로 위아래 2배 높이
            w, h = _bitmap_size(ii.hbmMask)
            h //= 2
        if w <= 0 or h <= 0 or w > 512 or h > 512:
            return None
        hdc = user32.GetDC(None)
        memdc = gdi32.CreateCompatibleDC(hdc)
        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth, bmi.biHeight, bmi.biPlanes, bmi.biBitCount = w, -h, 1, 32
        bits = ctypes.c_void_p()
        dib = gdi32.CreateDIBSection(memdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        if not dib or not bits.value:
            return None
        old = gdi32.SelectObject(memdc, dib)
        size = w * h * 4
        shots = []
        for fill in (0x00, 0xFF):
            ctypes.memset(bits, fill, size)
            user32.DrawIconEx(memdc, 0, 0, hcursor, w, h, 0, None, DI_NORMAL)
            shots.append(np.frombuffer(ctypes.string_at(bits, size), np.uint8).reshape(h, w, 4)[:, :, :3].astype(np.int32))  # int16 이면 *255 에서 오버플로
        black, white = shots
        diff = (white - black).mean(axis=2)  # 투명한 곳: 검정배경→0, 흰배경→255 → 차이 255
        invert = diff < -128  # 검정배경→흰색, 흰배경→검정: 화면 반전(XOR) 픽셀
        alpha = np.where(invert, 0, np.clip(255 - diff, 0, 255))
        safe = np.maximum(alpha, 1)[:, :, None]
        rgb = np.clip(black * 255 // safe, 0, 255)  # BGR (DIB 순서)
        rgba = np.dstack([rgb[:, :, 2], rgb[:, :, 1], rgb[:, :, 0], alpha]).astype(np.uint8)
        mask = Image.fromarray((invert * 255).astype(np.uint8), "L") if invert.any() else None
        return Image.fromarray(rgba, "RGBA"), mask, int(ii.xHotspot), int(ii.yHotspot)
    finally:
        if memdc and old:
            gdi32.SelectObject(memdc, old)
        if dib:
            gdi32.DeleteObject(dib)
        if memdc:
            gdi32.DeleteDC(memdc)
        if hdc:
            user32.ReleaseDC(None, hdc)
        if ii.hbmColor:
            gdi32.DeleteObject(ii.hbmColor)
        if ii.hbmMask:
            gdi32.DeleteObject(ii.hbmMask)


def overlay(hcursor: int):
    """커서 핸들 → (RGBA 이미지, 반전 마스크 또는 None, 핫스팟 x, 핫스팟 y). 핸들별로 캐시한다."""
    if hcursor in _cache:
        return _cache[hcursor]
    try:
        r = _render(hcursor)
    except Exception:
        r = None
    if len(_cache) > 64:
        _cache.clear()
    _cache[hcursor] = r
    return r


def _fallback_shape():
    """모양을 못 읽은 커서도 위치는 보이도록 쓰는 기본 화살표."""
    icon = Image.new("RGBA", (14, 22), (0, 0, 0, 0))
    ImageDraw.Draw(icon).polygon(_FALLBACK_ARROW, fill=(255, 255, 255, 255), outline=(0, 0, 0, 255))
    return icon, 0, 0


def shape(hcursor: int):
    """뷰어로 보낼 커서 모양 (RGBA 이미지, 핫스팟 x, 핫스팟 y).

    화면 반전(XOR) 픽셀은 뷰어가 배경을 뒤집을 수 없으므로 어떤 배경에서나 보이는 회색으로 대신한다.
    """
    o = overlay(hcursor)
    if not o:
        return _fallback_shape()
    icon, mask, hx, hy = o
    if mask is not None:
        icon = icon.copy()
        icon.paste((128, 128, 128, 255), mask=mask)
    return icon, hx, hy
