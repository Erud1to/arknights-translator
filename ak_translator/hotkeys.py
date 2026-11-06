"""전역 단축키. Win32 RegisterHotKey 를 쓰므로 관리자 권한이 필요 없다."""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

user32 = ctypes.windll.user32

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
SPI_GETKEYBOARDSPEED = 0x000A
SPI_GETKEYBOARDDELAY = 0x0016

_MODS = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "super": MOD_WIN,
}

_VK = {
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "backspace": 0x08,
    "insert": 0x2D,
    "delete": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "`": 0xC0,
    "-": 0xBD,
    "=": 0xBB,
    "[": 0xDB,
    "]": 0xDD,
    ";": 0xBA,
    "'": 0xDE,
    ",": 0xBC,
    ".": 0xBE,
    "/": 0xBF,
    "\\": 0xDC,
}
for _i in range(1, 25):
    _VK[f"f{_i}"] = 0x6F + _i


def parse(combo: str) -> tuple[int, int] | None:
    """'ctrl+alt+t' -> (modifiers, vk). 해석 불가면 None."""
    mods = 0
    vk = None
    for part in combo.lower().replace(" ", "").split("+"):
        if not part:
            continue
        if part in _MODS:
            mods |= _MODS[part]
        elif part in _VK:
            vk = _VK[part]
        elif len(part) == 1:
            vk = ord(part.upper())
        else:
            return None
    if vk is None:
        return None
    return mods | MOD_NOREPEAT, vk


def repeat_timing() -> tuple[float, float]:
    """키를 누르고 있을 때 (첫 반복까지 걸리는 시간, 그 뒤 반복 간격) 초. 윈도 키보드 설정에서 읽는다.
    설정값은 지연 0~3 (250~1000ms), 속도 0~31 (초당 약 2.5~30번)."""
    delay, speed = wintypes.UINT(1), wintypes.DWORD(31)
    user32.SystemParametersInfoW(SPI_GETKEYBOARDDELAY, 0, ctypes.byref(delay), 0)
    user32.SystemParametersInfoW(SPI_GETKEYBOARDSPEED, 0, ctypes.byref(speed), 0)
    first = 0.25 * (min(delay.value, 3) + 1)
    rate = 2.5 + min(speed.value, 31) * (30 - 2.5) / 31
    return first, 1 / rate


def key_down(vk: int) -> bool:
    """지금 그 키가 눌려 있는지. 앞 창이 우리보다 높은 권한(관리자로 돈 게임 등)이면 윈도가 막아 늘 False 다."""
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


class HotkeyManager:
    """별도 스레드에서 메시지 루프를 돌며 단축키를 감시한다."""

    def __init__(self):
        self._bindings: dict[int, tuple[str, callable]] = {}
        self._pending: list[tuple[str, str, callable, bool]] = []
        self._thread: threading.Thread | None = None
        self._tid = 0
        self._ready = threading.Event()
        self.failed: list[str] = []

    def register(self, name: str, combo: str, callback, repeat: bool = False) -> None:
        """repeat=True 면 키를 누르고 있는 동안 키 반복마다 callback 이 또 불린다 (뗐는지 알아낼 때 쓴다)."""
        self._pending.append((name, combo, callback, repeat))

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=3)

    def stop(self) -> None:
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        self._tid = 0
        self._thread = None

    def _run(self) -> None:
        self._tid = ctypes.windll.kernel32.GetCurrentThreadId()
        hid = 1
        for name, combo, cb, repeat in self._pending:
            parsed = parse(combo)
            if not parsed:
                self.failed.append(f"{name}: '{combo}' 해석 불가")
                continue
            mods, vk = parsed
            if repeat:
                mods &= ~MOD_NOREPEAT
            if user32.RegisterHotKey(None, hid, mods, vk):
                self._bindings[hid] = (name, cb)
                hid += 1
            else:
                self.failed.append(f"{name}: '{combo}' 는 다른 프로그램이 사용 중")
        self._ready.set()

        msg = wintypes.MSG()
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):
                break
            if msg.message == WM_HOTKEY:
                binding = self._bindings.get(msg.wParam)
                if binding:
                    try:
                        binding[1]()
                    except Exception:
                        pass

        for hid in list(self._bindings):
            user32.UnregisterHotKey(None, hid)
        self._bindings.clear()
