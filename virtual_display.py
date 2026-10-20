"""가상 디스플레이 관리 (Virtual Display Driver / IddCx 기반).

Windows 의 '디스플레이 설정' 에 나타나는 가상 모니터는 Indirect Display Driver 가 있어야 만들 수 있다.
여기서는 오픈소스 Virtual Display Driver (VirtualDrivers/Virtual-Display-Driver) 를 사용한다.
  - 모니터 개수/해상도 : C:\\VirtualDisplayDriver\\vdd_settings.xml
  - 적용               : 드라이버 장치를 비활성화 -> 활성화 (관리자 권한 필요)
  - 드라이버 설치      : winget install --id=VirtualDrivers.Virtual-Display-Driver -e (관리자 권한)
"""
import ctypes
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import display_mode

CONFIG_PATH = r"C:\VirtualDisplayDriver\vdd_settings.xml"
DEVICE_PATTERN = "Virtual Display Driver|MttVDD|IddSampleDriver"
WINGET_ID = "VirtualDrivers.Virtual-Display-Driver"
MAX_MONITORS = 16
WINGET_NO_UPDATE = -1978335189  # 0x8A15002B: 이미 설치됨 / 업그레이드 없음


class VddError(Exception):
    pass


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    """관리자 권한으로 이 프로그램을 다시 실행한다. 성공하면 True (호출한 쪽은 종료해야 함)."""
    exe = sys.executable
    if getattr(sys, "frozen", False):  # PyInstaller 로 만든 exe: 자기 자신을 다시 실행
        params, cwd = None, os.path.dirname(exe)
    else:
        script = os.path.abspath(sys.argv[0])
        if exe.lower().endswith("python.exe"):  # 콘솔 창 없이 실행
            pythonw = exe[:-10] + "pythonw.exe"
            if os.path.exists(pythonw):
                exe = pythonw
        params, cwd = f'"{script}"', os.path.dirname(script)
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, cwd, 1)
    return rc > 32


def _ps(command: str, timeout: int = 60) -> str:
    full = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command
    p = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", full],
        capture_output=True, timeout=timeout, creationflags=0x08000000)  # CREATE_NO_WINDOW
    out = p.stdout.decode("utf-8", "replace").strip()
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace").strip()
        raise VddError(err or out or f"PowerShell 오류 (코드 {p.returncode})")
    return out


def find_device() -> dict | None:
    """설치된 가상 디스플레이 드라이버 장치. 없으면 None."""
    out = _ps(
        "$d = Get-PnpDevice -Class Display -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.FriendlyName -match '{DEVICE_PATTERN}' }} | "
        "Select-Object -First 1 FriendlyName, InstanceId, Status; "
        "if ($d) { $d | ConvertTo-Json -Compress }")
    return json.loads(out) if out else None


def _tree() -> ET.ElementTree:
    if not os.path.exists(CONFIG_PATH):
        raise VddError(f"설정 파일이 없습니다: {CONFIG_PATH}\n드라이버를 먼저 설치하세요.")
    return ET.parse(CONFIG_PATH)


def _child(parent: ET.Element, tag: str) -> ET.Element:
    el = parent.find(tag)
    if el is None:
        el = ET.SubElement(parent, tag)
    return el


def get_count() -> int:
    el = _tree().getroot().find("./monitors/count")
    try:
        return int(el.text) if el is not None else 1
    except (TypeError, ValueError):
        return 1


def _write_config(count: int, width: int | None, height: int | None, refresh: int | None) -> None:
    tree = _tree()
    root = tree.getroot()
    _child(_child(root, "monitors"), "count").text = str(count)

    if refresh:
        g = _child(root, "global")
        if str(refresh) not in [e.text for e in g.findall("g_refresh_rate")]:
            ET.SubElement(g, "g_refresh_rate").text = str(refresh)

    if width and height:
        res = _child(root, "resolutions")
        exists = any(
            r.findtext("width") == str(width) and r.findtext("height") == str(height)
            and (not refresh or r.findtext("refresh_rate") == str(refresh))
            for r in res.findall("resolution"))
        if not exists:
            r = ET.SubElement(res, "resolution")
            ET.SubElement(r, "width").text = str(width)
            ET.SubElement(r, "height").text = str(height)
            ET.SubElement(r, "refresh_rate").text = str(refresh or 60)

    try:
        ET.indent(tree)
        tree.write(CONFIG_PATH, encoding="utf-8", xml_declaration=True)
    except PermissionError:
        raise VddError("설정 파일을 쓸 수 없습니다. 관리자 권한으로 실행하세요.")


def _restart(device: dict) -> None:
    iid = device["InstanceId"].replace("'", "''")
    _ps(f"Disable-PnpDevice -InstanceId '{iid}' -Confirm:$false; "
        f"Start-Sleep -Seconds 2; Enable-PnpDevice -InstanceId '{iid}' -Confirm:$false", timeout=90)


def _enable(device: dict) -> None:
    iid = device["InstanceId"].replace("'", "''")
    _ps(f"Enable-PnpDevice -InstanceId '{iid}' -Confirm:$false", timeout=60)


def _disable(device: dict) -> None:
    iid = device["InstanceId"].replace("'", "''")
    _ps(f"Disable-PnpDevice -InstanceId '{iid}' -Confirm:$false", timeout=60)


def active_count() -> int:
    """지금 만들어져 있는 가상 모니터 수 (드라이버 비활성/미설치면 0)."""
    dev = find_device()
    if not dev or dev.get("Status") != "OK":
        return 0
    return get_count()


def _display_names() -> list[str]:
    import host  # 지연 import (host 는 이 모듈을 쓰지 않는다)
    return [d["name"] for d in host.list_displays()]


def create_display(width: int = 1920, height: int = 1080, refresh: int = 60) -> tuple[int, str]:
    """가상 디스플레이 1개 추가. (만들어진 뒤의 총 개수, 해상도/주사율 적용 경고 문구 - 정상이면 빈 문자열)."""
    if not is_admin():
        raise VddError("관리자 권한이 필요합니다.")
    dev = find_device()
    if not dev:
        raise VddError("가상 디스플레이 드라이버가 설치되어 있지 않습니다.")
    n = active_count() + 1
    if n > MAX_MONITORS:
        raise VddError(f"가상 디스플레이는 최대 {MAX_MONITORS}개입니다.")
    before = set(_display_names())
    _write_config(n, width, height, refresh)
    _restart(dev)  # 비활성 상태였다면 활성화로 끝난다

    # 새로 생긴 모니터는 Windows 가 가장 작은 모드(800x600)로 잡으므로 고른 해상도를 직접 적용하고, 실제로 먹혔는지 확인한다
    want = (width, height, refresh)
    warnings = []
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        new = [x for x in _display_names() if x not in before]
        if new:
            time.sleep(1.0)  # 모니터가 안정될 때까지 잠깐 대기
            for name in new:
                if not display_mode.supports_mode(name, *want):
                    warnings.append(f"드라이버가 {width}x{height} @{refresh}Hz 모드를 제공하지 않습니다 "
                                    f"(vdd_settings.xml 의 g_refresh_rate/resolution 확인).")
                display_mode.set_mode(name, *want)
                got = display_mode.current_mode(name)
                if got != want:
                    shown = f"{got[0]}x{got[1]} @{got[2]}Hz" if got else "확인 불가"
                    warnings.append(f"{name}: 요청 {width}x{height} @{refresh}Hz → 실제 {shown}")
            break
        time.sleep(0.5)
    else:
        warnings.append("새 디스플레이를 찾지 못해 해상도/주사율을 적용하지 못했습니다. [새로고침] 후 확인하세요.")
    return n, "\n".join(warnings)


def delete_display() -> int:
    """가상 디스플레이 1개 삭제(마지막 것). 삭제 후 남은 개수를 반환."""
    if not is_admin():
        raise VddError("관리자 권한이 필요합니다.")
    dev = find_device()
    if not dev:
        raise VddError("가상 디스플레이 드라이버가 설치되어 있지 않습니다.")
    n = active_count()
    if n == 0:
        raise VddError("삭제할 가상 디스플레이가 없습니다.")
    if n == 1:  # 드라이버는 최소 1개를 요구하므로 장치를 꺼서 0개로 만든다
        _disable(dev)
        return 0
    _write_config(n - 1, None, None, None)
    _restart(dev)
    return n - 1


def _package_dir() -> str | None:
    """winget 이 파일을 풀어 놓은 폴더 (VDD Control.exe, SignedDrivers, Dependencies 가 들어 있음)."""
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages")
    if os.path.isdir(base):
        for name in os.listdir(base):
            p = os.path.join(base, name)
            if name.startswith("VirtualDrivers.Virtual-Display-Driver") and \
                    os.path.exists(os.path.join(p, "SignedDrivers", "x86", "VDD", "MttVDD.inf")):
                return p
    return None


def _run(args: list[str], timeout: int):
    p = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=0x08000000)
    return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")


def install_driver() -> str:
    """드라이버 설치 (관리자 권한).

    1) winget 으로 패키지 파일을 받는다 (winget 은 파일만 풀어 놓고 드라이버는 설치하지 않는다)
    2) 서명된 INF 를 devcon 으로 설치한다 (하드웨어 ID: Root\\MttVDD)
    3) 드라이버가 읽는 설정 파일 C:\\VirtualDisplayDriver\\vdd_settings.xml 을 준비한다
    """
    if not is_admin():
        raise VddError("관리자 권한이 필요합니다.")
    if find_device():
        return "이미 설치되어 있습니다."

    pkg = _package_dir()
    if pkg is None:
        rc, out = _run(["winget", "install", f"--id={WINGET_ID}", "-e",
                        "--accept-package-agreements", "--accept-source-agreements"], 600)
        if rc not in (0, WINGET_NO_UPDATE):
            raise VddError(out.strip()[-800:] or f"winget 오류 (코드 {rc})")
        pkg = _package_dir()
    if pkg is None:
        raise VddError("winget 패키지 폴더를 찾지 못했습니다:\n"
                       r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\VirtualDrivers.Virtual-Display-Driver*")

    drv_dir = os.path.join(pkg, "SignedDrivers", "x86", "VDD")  # 폴더명은 x86 이지만 INF 는 NTamd64(64비트)
    inf = os.path.join(drv_dir, "MttVDD.inf")
    devcon = os.path.join(pkg, "Dependencies", "devcon.exe")
    for f in (inf, devcon):
        if not os.path.exists(f):
            raise VddError(f"필요한 파일이 없습니다: {f}")

    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        import shutil
        shutil.copyfile(os.path.join(drv_dir, "vdd_settings.xml"), CONFIG_PATH)

    rc, out = _run([devcon, "install", inf, r"Root\MttVDD"], 180)
    # devcon 종료 코드: 0 성공, 1 재부팅 필요, 2 이상 실패
    if rc >= 2 or (rc == 0 and not find_device()):
        raise VddError(
            "드라이버 설치에 실패했습니다.\n"
            f"{out.strip()[-600:]}\n\n"
            "수동 설치: 아래 폴더의 'VDD Control.exe' 를 실행해 Install 을 누르세요.\n" + pkg)
    if rc == 1:
        raise VddError("설치는 되었지만 PC 재부팅이 필요합니다. 재부팅 후 다시 [새로고침] 하세요.")
    return out
