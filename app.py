"""VirtualMonitor - 가상 디스플레이 탭 + 원격 탭.

실행:  python app.py
"""
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image, ImageTk

import host as H
import protocol as P
import remote_client as RC
import virtual_display as VD

RESOLUTIONS = ["1920x1080", "2560x1440", "3840x2160", "1280x720", "1600x900", "2560x1080", "3440x1440"]
REFRESH_RATES = ["60", "120", "144", "90", "30"]


def run_async(widget: tk.Misc, work, done) -> None:
    """work() 를 스레드에서 실행하고 결과/예외를 done(result, error) 로 UI 스레드에 전달."""
    box = {}

    def target():
        try:
            box["r"] = work()
        except Exception as e:
            box["e"] = e
        widget.after(0, lambda: done(box.get("r"), box.get("e")))

    threading.Thread(target=target, daemon=True).start()


# ======================================================================
class VirtualTab(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=12)
        self.busy = False

        self.status = tk.StringVar(value="확인 중...")
        ttk.Label(self, textvariable=self.status, font=("", 10, "bold")).pack(anchor="w")
        self.admin_var = tk.StringVar()
        ttk.Label(self, textvariable=self.admin_var, foreground="#a15c00").pack(anchor="w", pady=(0, 8))

        opt = ttk.Frame(self)
        opt.pack(fill="x", pady=4)
        ttk.Label(opt, text="해상도").pack(side="left")
        self.res = ttk.Combobox(opt, values=RESOLUTIONS, width=11)
        self.res.set(RESOLUTIONS[0])
        self.res.pack(side="left", padx=(4, 12))
        ttk.Label(opt, text="주사율(Hz)").pack(side="left")
        self.hz = ttk.Combobox(opt, values=REFRESH_RATES, width=5)
        self.hz.set("60")
        self.hz.pack(side="left", padx=4)

        btns = ttk.Frame(self)
        btns.pack(fill="x", pady=8)
        self.btn_create = ttk.Button(btns, text="가상 디스플레이 만들기", command=self.create)
        self.btn_create.pack(side="left")
        self.btn_delete = ttk.Button(btns, text="삭제", command=self.delete)
        self.btn_delete.pack(side="left", padx=6)
        self.btn_refresh = ttk.Button(btns, text="새로고침", command=self.refresh)
        self.btn_refresh.pack(side="left")
        self.btn_install = ttk.Button(btns, text="드라이버 설치 (winget)", command=self.install)

        ttk.Label(self, text="현재 연결된 디스플레이 (우클릭 > 디스플레이 설정과 동일)").pack(anchor="w", pady=(10, 2))
        self.tree = ttk.Treeview(self, columns=("n", "name", "size", "pos"), show="headings", height=7)
        for c, t, w in (("n", "번호", 50), ("name", "장치", 150), ("size", "해상도", 110), ("pos", "위치", 110)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True)

        self.note = tk.StringVar()
        ttk.Label(self, textvariable=self.note, foreground="#666", wraplength=520).pack(anchor="w", pady=(8, 0))

        self.refresh()

    def _set_busy(self, busy: bool, msg: str = ""):
        self.busy = busy
        st = "disabled" if busy else "normal"
        for b in (self.btn_create, self.btn_delete, self.btn_refresh, self.btn_install):
            b.configure(state=st)
        if msg:
            self.note.set(msg)

    def _ensure_admin(self) -> bool:
        if VD.is_admin():
            return True
        if messagebox.askyesno("관리자 권한 필요",
                               "가상 디스플레이를 바꾸려면 관리자 권한이 필요합니다.\n관리자 권한으로 다시 실행할까요?"):
            if VD.relaunch_as_admin():
                self.winfo_toplevel().destroy()
        return False

    def refresh(self):
        self.admin_var.set("" if VD.is_admin() else "※ 관리자 권한이 아닙니다 (만들기/삭제 시 관리자로 다시 실행)")
        self._set_busy(True, "상태 확인 중...")

        def work():
            dev = VD.find_device()
            cnt = VD.active_count() if dev else 0
            return dev, cnt, H.list_displays()

        def done(res, err):
            self._set_busy(False, "")
            self.tree.delete(*self.tree.get_children())
            if err:
                self.status.set(f"상태 확인 실패: {err}")
                return
            dev, cnt, displays = res
            if dev:
                self.status.set(f"드라이버 설치됨 · 가상 디스플레이 {cnt}개 활성")
                self.btn_install.pack_forget()
            else:
                self.status.set("가상 디스플레이 드라이버가 설치되어 있지 않습니다")
                self.btn_install.pack(side="left", padx=6)
            for d in displays:
                self.tree.insert("", "end", values=(
                    d["index"], d["name"] + (" (주)" if d["primary"] else ""),
                    f"{d['width']}x{d['height']}", f"({d['left']}, {d['top']})"))

        run_async(self, work, done)

    def create(self):
        if not self._ensure_admin():
            return
        try:
            w, h = (int(x) for x in self.res.get().lower().split("x"))
            hz = int(self.hz.get())
        except ValueError:
            messagebox.showerror("입력 오류", "해상도는 1920x1080 형식, 주사율은 숫자로 입력하세요.")
            return
        self._set_busy(True, "가상 디스플레이를 만드는 중... (몇 초 걸립니다)")

        def done(n, err):
            self._set_busy(False, "")
            if err:
                messagebox.showerror("만들기 실패", str(err))
            self.after(1500, self.refresh)

        run_async(self, lambda: VD.create_display(w, h, hz), done)

    def delete(self):
        if not self._ensure_admin():
            return
        if not messagebox.askyesno("삭제", "마지막 가상 디스플레이를 삭제할까요?"):
            return
        self._set_busy(True, "삭제하는 중...")

        def done(n, err):
            self._set_busy(False, "")
            if err:
                messagebox.showerror("삭제 실패", str(err))
            self.after(1500, self.refresh)

        run_async(self, VD.delete_display, done)

    def install(self):
        if not self._ensure_admin():
            return
        if not messagebox.askyesno(
                "드라이버 설치",
                "오픈소스 Virtual Display Driver 를 winget 으로 설치합니다.\n"
                "(시스템 드라이버 설치입니다. 계속할까요?)"):
            return
        self._set_busy(True, "설치 중... (1~2분 걸릴 수 있습니다)")

        def done(_r, err):
            self._set_busy(False, "")
            if err:
                messagebox.showerror("설치 실패", str(err))
            self.refresh()

        run_async(self, VD.install_driver, done)


# ======================================================================
class ViewerWindow(tk.Toplevel):
    """선택한 디스플레이만 보여주는 창. F11 전체화면 / Esc 전체화면 해제."""

    def __init__(self, master, client: RC.RemoteClient, title: str, fullscreen: bool, on_close):
        super().__init__(master)
        self.client = client
        self.on_close = on_close
        self.title(title)
        self.geometry("1280x720")
        self.configure(bg="black")
        self.label = tk.Label(self, bg="black", bd=0)
        self.label.pack(fill="both", expand=True)
        self.seq = 0
        self.src: Image.Image | None = None
        self.photo = None
        self.fullscreen = False
        self.dirty = False
        self.bind("<F11>", lambda e: self.set_fullscreen(not self.fullscreen))
        self.bind("<Escape>", lambda e: self.set_fullscreen(False))
        self.bind("<Double-Button-1>", lambda e: self.set_fullscreen(not self.fullscreen))
        self.label.bind("<Configure>", lambda e: setattr(self, "dirty", True))
        self.protocol("WM_DELETE_WINDOW", self.close)
        if fullscreen:
            self.set_fullscreen(True)
        self.after(5, self.tick)

    def set_fullscreen(self, on: bool):
        self.fullscreen = on
        self.attributes("-fullscreen", on)
        self.dirty = True

    def close(self):
        self.on_close()
        self.destroy()

    def tick(self):
        if not self.winfo_exists():
            return
        got = self.client.take_frame(self.seq)
        if got:
            self.seq, self.src = got
            self.dirty = True
        if self.dirty and self.src is not None:
            self.dirty = False
            self.render()
        self.after(4, self.tick)  # 약 250Hz 폴링 -> 프레임 도착 즉시 표시

    def render(self):
        w, h = self.label.winfo_width(), self.label.winfo_height()
        if w < 2 or h < 2:
            return
        iw, ih = self.src.size
        if (iw, ih) == (w, h):
            img = self.src
        else:
            s = min(w / iw, h / ih)
            img = self.src.resize((max(1, int(iw * s)), max(1, int(ih * s))), Image.BILINEAR)
        self.photo = ImageTk.PhotoImage(img)
        self.label.configure(image=self.photo)


class RemoteTab(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=12)
        self.events: queue.Queue = queue.Queue()
        self.client: RC.RemoteClient | None = None
        self.displays: list[dict] = []
        self.viewer: ViewerWindow | None = None

        row = ttk.Frame(self)
        row.pack(fill="x")
        ttk.Label(row, text="IP").grid(row=0, column=0, sticky="w")
        self.ip = ttk.Combobox(row, width=18)
        self.ip.grid(row=0, column=1, padx=4)
        ttk.Label(row, text="포트").grid(row=0, column=2, padx=(8, 0))
        self.port = ttk.Entry(row, width=7)
        self.port.insert(0, str(P.PORT))
        self.port.grid(row=0, column=3, padx=4)
        ttk.Label(row, text="비밀번호").grid(row=0, column=4, padx=(8, 0))
        self.pw = ttk.Entry(row, width=12, show="*")
        self.pw.grid(row=0, column=5, padx=4)
        self.btn_scan = ttk.Button(row, text="PC 검색", command=self.scan)
        self.btn_scan.grid(row=0, column=6, padx=(8, 0))

        row2 = ttk.Frame(self)
        row2.pack(fill="x", pady=10)
        self.btn_connect = ttk.Button(row2, text="접속", command=self.toggle_connect)
        self.btn_connect.pack(side="left")
        ttk.Label(row2, text="   원격할 디스플레이").pack(side="left")
        self.disp = ttk.Combobox(row2, state="disabled", width=38)
        self.disp.pack(side="left", padx=4)

        row3 = ttk.Frame(self)
        row3.pack(fill="x")
        self.btn_start = ttk.Button(row3, text="원격 접속하기", command=self.start_view, state="disabled")
        self.btn_start.pack(side="left")
        self.full = tk.BooleanVar(value=True)
        ttk.Checkbutton(row3, text="전체화면으로 시작", variable=self.full).pack(side="left", padx=12)

        self.status = tk.StringVar(value="호스트 PC 의 IP 와 비밀번호를 입력하고 [접속]을 누르세요.")
        ttk.Label(self, textvariable=self.status, wraplength=560).pack(anchor="w", pady=(14, 0))
        ttk.Label(self, foreground="#666", wraplength=560, text=(
            "항상 원본(최고) 해상도 · 최대 60fps 로 전송합니다.\n"
            "보기 창: F11 / 더블클릭 = 전체화면 전환, Esc = 전체화면 해제, 창 닫기 = 종료")
        ).pack(anchor="w", pady=(10, 0))

        self.after(50, self.poll)

    # ---- 접속 ----
    def scan(self):
        self.btn_scan.configure(state="disabled")
        self.status.set("같은 네트워크에서 호스트 검색 중...")

        def done(res, err):
            self.btn_scan.configure(state="normal")
            if err or not res:
                self.status.set("호스트를 찾지 못했습니다. IP 를 직접 입력하세요. (호스트 PC 에서 host.py 실행 / 방화벽 확인)")
                return
            self.ip.configure(values=[ip for ip, _n, _p in res])
            self.ip.set(res[0][0])
            self.port.delete(0, "end")
            self.port.insert(0, str(res[0][2]))
            self.status.set("발견: " + ", ".join(f"{n} ({ip})" for ip, n, _p in res))

        run_async(self, RC.discover, done)

    def toggle_connect(self):
        if self.client:
            self.disconnect()
            return
        host = self.ip.get().strip()
        if not host:
            messagebox.showwarning("IP 필요", "접속할 PC 의 IP 를 입력하세요.")
            return
        try:
            port = int(self.port.get())
        except ValueError:
            messagebox.showwarning("포트 오류", "포트는 숫자여야 합니다.")
            return
        self.client = RC.RemoteClient(self.events)
        self.client.connect(host, port, self.pw.get())
        self.btn_connect.configure(text="접속 중...", state="disabled")
        self.status.set(f"{host}:{port} 에 접속 중...")

    def disconnect(self):
        self.close_viewer()
        if self.client:
            self.client.close()
            self.client = None
        self.displays = []
        self.disp.configure(state="disabled", values=[])
        self.disp.set("")
        self.btn_start.configure(state="disabled")
        self.btn_connect.configure(text="접속", state="normal")

    def _fill_displays(self, displays):
        self.displays = displays
        labels = [
            f"[{d['index']}] {d['name']}{' (주)' if d['primary'] else ''}  {d['width']}x{d['height']}"
            for d in displays]
        self.disp.configure(state="readonly", values=labels)
        if labels:
            self.disp.current(0)
            self.btn_start.configure(state="normal")

    # ---- 보기 ----
    def start_view(self):
        i = self.disp.current()
        if not self.client or i < 0:
            return
        d = self.displays[i]
        self.client.select(d["index"])
        self.close_viewer(stop_stream=False)
        self.viewer = ViewerWindow(
            self.winfo_toplevel(), self.client,
            f"{self.ip.get()} - {d['name']} {d['width']}x{d['height']}",
            self.full.get(), lambda: self.close_viewer(from_window=True))
        self.status.set(f"원격 보는 중: {d['name']} ({d['width']}x{d['height']})")

    def close_viewer(self, stop_stream: bool = True, from_window: bool = False):
        v, self.viewer = self.viewer, None
        if v is not None and not from_window:
            try:
                v.destroy()
            except tk.TclError:
                pass
        if stop_stream and self.client:
            self.client.stop_stream()
            if v is not None or from_window:
                self.status.set("원격을 종료했습니다. 다른 디스플레이를 고르거나 다시 시작할 수 있습니다.")

    # ---- 이벤트 ----
    def poll(self):
        try:
            while True:
                client, kind, data = self.events.get_nowait()
                if client is not self.client:
                    continue
                if kind == "connected":
                    hello, displays = data
                    self.btn_connect.configure(text="접속 해제", state="normal")
                    self.status.set(f"접속됨: {hello.get('host', '?')} · 디스플레이 {len(displays)}개. 원격할 디스플레이를 고르세요.")
                    self._fill_displays(displays)
                elif kind == "displays":
                    self._fill_displays(data)
                elif kind == "error":
                    msg = data
                    self.disconnect()
                    self.status.set(f"오류: {msg}")
                elif kind == "closed":
                    self.disconnect()
                    self.status.set("연결이 끊어졌습니다.")
        except queue.Empty:
            pass
        self.after(50, self.poll)


# ======================================================================
def main():
    H.set_dpi_aware()
    root = tk.Tk()
    root.title("VirtualMonitor")
    root.geometry("640x470")
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=6, pady=6)
    nb.add(VirtualTab(nb), text="가상 디스플레이")
    nb.add(RemoteTab(nb), text="원격")
    root.mainloop()


if __name__ == "__main__":
    main()
