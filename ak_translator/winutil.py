"""Windows 전용 유틸: DPI 인식, 창 목록/추적, 창 스타일 조작."""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

GWL_EXSTYLE = -20
GWL_STYLE = -16
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_CHILD = 0x40000000

WDA_NONE = 0x00
WDA_EXCLUDEFROMCAPTURE = 0x11

GA_ROOT = 2
GA_ROOTOWNER = 3

DWMWA_CLOAKED = 14
DWMWA_EXTENDED_FRAME_BOUNDS = 9

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
FR_PRIVATE = 0x10


def load_private_fonts(folder: str) -> list[str]:
    """folder 의 글꼴을 이 프로세스에서만 쓰게 등록한다 (PC 에 설치하지 않는다). Tk 를 만들기 전에 불러야
    Tk 의 글꼴 목록에 보인다. 등록한 파일 이름을 돌려준다."""
    if not os.path.isdir(folder):
        return []
    gdi32 = ctypes.WinDLL("gdi32")
    gdi32.AddFontResourceExW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p)
    loaded = []
    for name in sorted(os.listdir(folder)):
        if name.lower().endswith((".otf", ".ttf", ".ttc")) and \
                gdi32.AddFontResourceExW(os.path.join(folder, name), FR_PRIVATE, None):
            loaded.append(name)
    return loaded


def enable_dpi_awareness() -> None:
    """Tk 좌표와 실제 픽셀 좌표를 일치시킨다. 반드시 Tk 생성 전에 호출."""
    try:
        # PER_MONITOR_AWARE_V2
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def virtual_screen() -> tuple[int, int, int, int]:
    """모든 모니터를 포함한 가상 화면 (x, y, w, h)."""
    return (
        user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    )


def get_hwnd(widget) -> int:
    """Tk 최상위 창의 HWND.

    overrideredirect 창에서 GetParent 는 '소유자' 창을 돌려주므로 쓰면 안 된다.
    wm_frame() 을 먼저 시도하고, 실패하면 GetAncestor(GA_ROOT) 로 올라간다.
    """
    try:
        frame = widget.wm_frame()
        if frame:
            return int(frame, 16)
    except Exception:
        pass
    hwnd = widget.winfo_id()
    try:
        root = user32.GetAncestor(wintypes.HWND(hwnd), GA_ROOT)
        if root:
            return root
    except Exception:
        pass
    return hwnd


# ---------------------------------------------------------------- 창 스타일
def _get_exstyle(hwnd: int) -> int:
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        return user32.GetWindowLongPtrW(wintypes.HWND(hwnd), GWL_EXSTYLE)
    return user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE)


def _set_exstyle(hwnd: int, style: int) -> None:
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        user32.SetWindowLongPtrW(wintypes.HWND(hwnd), GWL_EXSTYLE, ctypes.c_longlong(style))
    else:
        user32.SetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE, style)


def set_click_through(hwnd: int, enabled: bool) -> None:
    """창 전체를 마우스 통과시킨다(게임/영상 위에 올려도 방해되지 않음)."""
    style = _get_exstyle(hwnd)
    flags = WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_NOACTIVATE
    style = (style | flags) if enabled else (style & ~WS_EX_TRANSPARENT)
    _set_exstyle(hwnd, style)


def set_tool_window(hwnd: int) -> None:
    """Alt+Tab / 작업표시줄에 나타나지 않게 한다."""
    _set_exstyle(hwnd, _get_exstyle(hwnd) | WS_EX_TOOLWINDOW)


def exclude_from_capture(hwnd: int, enabled: bool = True) -> bool:
    """이 창이 화면 캡처에 찍히지 않게 한다 (Windows 10 2004+)."""
    try:
        affinity = WDA_EXCLUDEFROMCAPTURE if enabled else WDA_NONE
        ok = user32.SetWindowDisplayAffinity(wintypes.HWND(hwnd), ctypes.c_uint(affinity))
        return bool(ok)
    except Exception:
        return False


# ------------------------------------------------------------------ 창 영역
def frame_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """창 테두리 기준 화면 영역. DWM 의 실제 보이는 경계를 우선 쓴다."""
    rect = wintypes.RECT()
    try:
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd),
            ctypes.c_uint(DWMWA_EXTENDED_FRAME_BOUNDS),
            ctypes.byref(rect),
            ctypes.sizeof(rect),
        )
        if hr != 0:
            raise OSError(hr)
    except Exception:
        if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return None
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None
    return rect.left, rect.top, w, h


def client_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """제목 표시줄과 테두리를 뺀 내용 영역의 화면 좌표."""
    rect = wintypes.RECT()
    if not user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return None
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None
    pt = wintypes.POINT(rect.left, rect.top)
    if not user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(pt)):
        return None
    return pt.x, pt.y, w, h


def window_rect(hwnd: int, area: str = "client") -> tuple[int, int, int, int] | None:
    if area == "frame":
        return frame_rect(hwnd)
    return client_rect(hwnd) or frame_rect(hwnd)


def is_cloaked(hwnd: int) -> bool:
    val = ctypes.c_int(0)
    try:
        ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd),
            ctypes.c_uint(DWMWA_CLOAKED),
            ctypes.byref(val),
            ctypes.sizeof(val),
        )
    except Exception:
        return False
    return val.value != 0


def window_alive(hwnd: int) -> bool:
    """창이 아직 살아 있고 화면에 떠 있는가(최소화 제외)."""
    if not hwnd or not user32.IsWindow(wintypes.HWND(hwnd)):
        return False
    if not user32.IsWindowVisible(wintypes.HWND(hwnd)):
        return False
    if user32.IsIconic(wintypes.HWND(hwnd)):
        return False
    return not is_cloaked(hwnd)


def window_title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(wintypes.HWND(hwnd), buf, n + 1)
    return buf.value


def window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(wintypes.HWND(hwnd), buf, 256)
    return buf.value


def window_exe(hwnd: int) -> str:
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    if not pid.value:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(handle)
    return ""


_ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _is_pickable(
    hwnd: int, exclude: set[int], min_size: int = 80, allow_tool: bool = False
) -> tuple | None:
    """번역 대상 후보이거나(allow_tool=False) 화면을 가릴 수 있는 창(True)인가.

    도구 창은 대상 목록에서는 빼지만, 무언가를 가리는지 볼 때는 포함해야 한다.
    """
    if hwnd in exclude:
        return None
    if not user32.IsWindowVisible(wintypes.HWND(hwnd)):
        return None
    if user32.IsIconic(wintypes.HWND(hwnd)):
        return None
    if is_cloaked(hwnd):
        return None
    if not allow_tool and (_get_exstyle(hwnd) & WS_EX_TOOLWINDOW):
        return None
    rect = frame_rect(hwnd)
    if not rect or rect[2] < min_size or rect[3] < min_size:
        return None
    return rect


def list_windows(exclude: set[int] | None = None) -> list[dict]:
    """번역 대상이 될 만한 최상위 창 목록 (앞에 있는 것부터)."""
    exclude = exclude or set()
    out: list[dict] = []

    def cb(hwnd, _lparam):
        rect = _is_pickable(hwnd, exclude)
        if rect is None:
            return True
        title = window_title(hwnd)
        exe = window_exe(hwnd)
        if not title and not exe:
            return True
        if exe.lower() in ("applicationframehost.exe",) and not title:
            return True
        out.append(
            {
                "hwnd": int(hwnd),
                "title": title or "(제목 없음)",
                "exe": exe,
                "cls": window_class(hwnd),
                "rect": rect,
            }
        )
        return True

    try:
        user32.EnumWindows(_ENUMPROC(cb), 0)
    except Exception:
        pass
    return out


def window_at(x: int, y: int, exclude: set[int] | None = None) -> dict | None:
    """(x, y) 지점에서 실제로 보이는 최상위 창.

    EnumWindows 는 Z 순서(위에서 아래)로 돌기 때문에, 우리 창을 건너뛰면서
    처음 만나는 창이 눈에 보이는 창이다.
    """
    exclude = exclude or set()
    found: list[dict] = []

    def cb(hwnd, _lparam):
        rect = _is_pickable(hwnd, exclude, min_size=40)
        if rect is None:
            return True
        rx, ry, rw, rh = rect
        if rx <= x < rx + rw and ry <= y < ry + rh:
            found.append(
                {
                    "hwnd": int(hwnd),
                    "title": window_title(hwnd) or "(제목 없음)",
                    "exe": window_exe(hwnd),
                    "cls": window_class(hwnd),
                    "rect": rect,
                }
            )
            return False
        return True

    try:
        user32.EnumWindows(_ENUMPROC(cb), 0)
    except Exception:
        return None
    return found[0] if found else None


def visible_at(hwnd: int, x: int, y: int, exclude: set[int] | None = None) -> bool:
    """(x, y) 에서 hwnd 가 다른 창에 가려지지 않고 보이는가."""
    top = window_at(x, y, exclude)
    if not top:
        return False
    if top["hwnd"] == hwnd:
        return True
    # 대상 창이 띄운 팝업/자식 창도 같은 창으로 본다
    owner = user32.GetAncestor(wintypes.HWND(top["hwnd"]), GA_ROOTOWNER)
    return int(owner) == hwnd


def visible_fraction(
    hwnd: int, rect: tuple[int, int, int, int], exclude: set[int] | None = None
) -> tuple[float, str]:
    """(rect 안에서 대상 창이 보이는 비율, 가장 많이 가리고 있는 창 이름).

    창 목록은 한 번만 훑는다. 지점마다 EnumWindows 를 도는 것보다 훨씬 싸다.
    """
    x, y, w, h = rect
    if w <= 0 or h <= 0:
        return 0.0, ""

    exclude = set(exclude or set())
    stack: list[tuple[int, tuple[int, int, int, int]]] = []

    def cb(h_, _lparam):
        # 가림 판정에서는 도구 창도 세어야 한다 (화면은 똑같이 가리므로)
        r = _is_pickable(h_, exclude, min_size=40, allow_tool=True)
        if r is not None:
            stack.append((int(h_), r))
        return True

    try:
        user32.EnumWindows(_ENUMPROC(cb), 0)
    except Exception:
        return 1.0, ""

    owners: dict[int, int] = {}

    def belongs(candidate: int) -> bool:
        if candidate == hwnd:
            return True
        owner = owners.get(candidate)
        if owner is None:
            owner = int(user32.GetAncestor(wintypes.HWND(candidate), GA_ROOTOWNER))
            owners[candidate] = owner
        return owner == hwnd

    seen = 0
    blockers: dict[int, int] = {}
    points = [
        (int(x + w * fx), int(y + h * fy))
        for fy in (0.2, 0.5, 0.8)
        for fx in (0.2, 0.5, 0.8)
    ]
    for px, py in points:
        for cand, (rx, ry, rw, rh) in stack:  # Z 순서: 위에 있는 창부터
            if rx <= px < rx + rw and ry <= py < ry + rh:
                if belongs(cand):
                    seen += 1
                else:
                    blockers[cand] = blockers.get(cand, 0) + 1
                break

    name = ""
    if blockers:
        top = max(blockers, key=blockers.get)
        name = window_title(top) or window_exe(top)
    return seen / len(points), name


SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def covering_window(hwnd: int, rect: tuple[int, int, int, int], exclude: set[int]) -> str | None:
    """Z 순서로 hwnd 보다 위에 있으면서 rect 를 덮는 창의 이름. 없으면 None.

    맨 앞 창만 봐서는 안 된다. 게임을 덮은 브라우저가 맨 앞이 아닐 수 있다(다른 모니터를
    쓰는 중일 때). 그 상태로 캡처하면 브라우저 내용을 읽어 번역 서버로 보내게 된다.
    """
    x, y, w, h = rect
    found: list[str] = []

    def cb(cand, _lparam):
        cand = int(cand)
        if cand == hwnd:
            return False       # 여기까지 내려왔으면 위에 덮은 창이 없다
        if cand in exclude or not user32.IsWindowVisible(wintypes.HWND(cand)):
            return True
        if user32.IsIconic(wintypes.HWND(cand)) or is_cloaked(cand):
            return True
        ex = _get_exstyle(cand)
        if ex & WS_EX_LAYERED and ex & WS_EX_TRANSPARENT:
            return True        # 클릭 통과 창은 그래픽 드라이버의 오버레이 같은 투명한 창이다
        if window_class(cand) in SHELL_CLASSES:
            return True
        other = frame_rect(cand)
        if not other or other[2] < 8 or other[3] < 8:
            return True
        ox, oy, ow, oh = other
        iw = min(x + w, ox + ow) - max(x, ox)
        ih = min(y + h, oy + oh) - max(y, oy)
        if iw > 0 and ih > 0:
            found.append(window_title(cand) or window_class(cand))
            return False
        return True

    try:
        user32.EnumWindows(_ENUMPROC(cb), 0)
    except Exception:
        return "알 수 없음"    # 확인할 수 없으면 캡처하지 않는 쪽을 택한다
    return found[0] if found else None


def find_window(
    title: str = "", exe: str = "", cls: str = "", exclude: set[int] | None = None
) -> int:
    """다시 켰을 때 같은 프로그램의 창을 찾아 붙기 위한 재연결.

    exclude 에는 반드시 우리 자신의 창들을 넣어야 한다. 그러지 않으면 우리도
    python.exe 로 도는 Tk 창이라 자기 자신에게 붙어버릴 수 있다.
    """
    best = 0
    best_score = 0
    for info in list_windows(exclude):
        score = 0
        if exe and info["exe"].lower() == exe.lower():
            score += 4
        if cls and info["cls"] == cls:
            score += 2
        if title and info["title"] == title:
            score += 3
        elif title and (title in info["title"] or info["title"] in title):
            score += 1
        if score > best_score:
            best, best_score = info["hwnd"], score
    return best if best_score >= 4 else 0
