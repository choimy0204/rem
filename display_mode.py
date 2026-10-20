"""디스플레이 모드(해상도/주사율) 조회·변경. host.py 와 virtual_display.py 가 같이 쓴다."""
import ctypes

ENUM_CURRENT_SETTINGS = -1
DM_PELSWIDTH = 0x80000
DM_PELSHEIGHT = 0x100000
DM_DISPLAYFREQUENCY = 0x400000
CDS_UPDATEREGISTRY = 1


class _DEVMODEW(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", ctypes.c_wchar * 32), ("dmSpecVersion", ctypes.c_ushort),
        ("dmDriverVersion", ctypes.c_ushort), ("dmSize", ctypes.c_ushort),
        ("dmDriverExtra", ctypes.c_ushort), ("dmFields", ctypes.c_ulong),
        ("dmPositionX", ctypes.c_long), ("dmPositionY", ctypes.c_long),
        ("dmDisplayOrientation", ctypes.c_ulong), ("dmDisplayFixedOutput", ctypes.c_ulong),
        ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short), ("dmFormName", ctypes.c_wchar * 32),
        ("dmLogPixels", ctypes.c_ushort), ("dmBitsPerPel", ctypes.c_ulong),
        ("dmPelsWidth", ctypes.c_ulong), ("dmPelsHeight", ctypes.c_ulong),
        ("dmDisplayFlags", ctypes.c_ulong), ("dmDisplayFrequency", ctypes.c_ulong),
        ("dmICMMethod", ctypes.c_ulong), ("dmICMIntent", ctypes.c_ulong),
        ("dmMediaType", ctypes.c_ulong), ("dmDitherType", ctypes.c_ulong),
        ("dmReserved1", ctypes.c_ulong), ("dmReserved2", ctypes.c_ulong),
        ("dmPanningWidth", ctypes.c_ulong), ("dmPanningHeight", ctypes.c_ulong),
    ]


def _devmode() -> _DEVMODEW:
    dm = _DEVMODEW()
    dm.dmSize = ctypes.sizeof(_DEVMODEW)
    return dm


def current_mode(device_name: str) -> tuple[int, int, int] | None:
    """디스플레이(예: \\\\.\\DISPLAY5)가 지금 쓰는 (너비, 높이, 주사율Hz). 못 읽으면 None."""
    dm = _devmode()
    if not ctypes.windll.user32.EnumDisplaySettingsW(device_name, ENUM_CURRENT_SETTINGS, ctypes.byref(dm)):
        return None
    return dm.dmPelsWidth, dm.dmPelsHeight, dm.dmDisplayFrequency


def supports_mode(device_name: str, width: int, height: int, refresh: int) -> bool:
    """드라이버가 이 디스플레이에 해당 해상도/주사율 모드를 제공하는지."""
    dm = _devmode()
    i = 0
    while ctypes.windll.user32.EnumDisplaySettingsW(device_name, i, ctypes.byref(dm)):
        if (dm.dmPelsWidth, dm.dmPelsHeight, dm.dmDisplayFrequency) == (width, height, refresh):
            return True
        i += 1
    return False


def set_mode(device_name: str, width: int, height: int, refresh: int | None = None) -> bool:
    """해상도/주사율을 바꾼다. 성공하면 True. 주사율이 안 맞으면 해상도만 적용한다 (그 경우 current_mode 로 확인)."""
    user32 = ctypes.windll.user32
    dm = _devmode()
    if not user32.EnumDisplaySettingsW(device_name, ENUM_CURRENT_SETTINGS, ctypes.byref(dm)):
        return False
    dm.dmPelsWidth, dm.dmPelsHeight = width, height
    fields = DM_PELSWIDTH | DM_PELSHEIGHT
    for hz in ([refresh] if refresh else []) + [None]:
        if hz:
            dm.dmDisplayFrequency = hz
        dm.dmFields = fields | (DM_DISPLAYFREQUENCY if hz else 0)
        if user32.ChangeDisplaySettingsExW(device_name, ctypes.byref(dm), None, CDS_UPDATEREGISTRY, None) == 0:
            return True
    return False
