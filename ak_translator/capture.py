"""화면 캡처. mss 를 우선 사용하고 없으면 PIL.ImageGrab 으로 대체."""
from __future__ import annotations

import threading

import numpy as np
from PIL import Image

try:
    import mss  # type: ignore

    _HAS_MSS = True
except Exception:  # pragma: no cover
    _HAS_MSS = False


_local = threading.local()


def _sct():
    """mss 인스턴스는 스레드마다 따로 만들어야 한다."""
    inst = getattr(_local, "sct", None)
    if inst is None:
        inst = mss.MSS()
        _local.sct = inst
    return inst


def grab(region: tuple[int, int, int, int]) -> Image.Image:
    """region = (x, y, w, h) 물리 픽셀. RGB 이미지를 반환."""
    x, y, w, h = (int(v) for v in region)
    w = max(1, w)
    h = max(1, h)
    if _HAS_MSS:
        raw = _sct().grab({"left": x, "top": y, "width": w, "height": h})
        # mss 는 BGRA
        arr = np.frombuffer(raw.raw, dtype=np.uint8).reshape(raw.height, raw.width, 4)
        return Image.fromarray(arr[:, :, [2, 1, 0]], "RGB")

    from PIL import ImageGrab

    img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
    return img.convert("RGB")


def copy_to_clipboard(img: Image.Image) -> bool:
    """이미지를 Windows 클립보드에 넣는다 (CF_DIB). 바로 붙여넣기 할 수 있게."""
    import ctypes
    import io
    from ctypes import wintypes

    try:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "BMP")
        # BMP 파일 헤더 14바이트를 뗀 나머지가 DIB
        dib = buf.getvalue()[14:]
    except Exception:
        return False

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    GMEM_MOVEABLE = 0x0002
    CF_DIB = 8

    # argtypes 를 지정하지 않으면 ctypes 가 핸들·크기를 32비트로 잘라 넘겨서
    # 64비트에서 GlobalLock / SetClipboardData 가 조용히 실패한다.
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE

    try:
        if not user32.OpenClipboard(None):
            return False
        handle = None
        try:
            user32.EmptyClipboard()
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(dib))
            if not handle:
                return False
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return False
            ctypes.memmove(ptr, dib, len(dib))
            kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(CF_DIB, handle):
                return False
            handle = None  # 성공하면 소유권이 시스템으로 넘어간다
            return True
        finally:
            if handle:
                kernel32.GlobalFree(handle)
            user32.CloseClipboard()
    except Exception:
        return False


FINGERPRINT_SIZE = 96
CELL_DELTA = 12.0  # 이만큼 밝기가 달라진 칸만 '바뀐 칸'으로 센다


def fingerprint(img: Image.Image, size: int = FINGERPRINT_SIZE) -> np.ndarray:
    """화면 변화 감지용 축소 그레이스케일 지문."""
    small = img.convert("L").resize((size, size), Image.BILINEAR)
    return np.asarray(small, dtype=np.float32)


def changed_ratio(
    a: np.ndarray | None, b: np.ndarray | None, cell_delta: float = CELL_DELTA
) -> float:
    """두 지문 사이에서 '크게 바뀐 칸'의 비율 (0~100 %).

    평균 밝기 차이를 쓰면 게임 배경 애니메이션과 실제 글자 변경을 구분하지 못한다
    (실측: 배경 2.6~3.3 vs 글자 변경 4.1~4.4, 겨우 1.4배 차이).
    반면 '많이 변한 칸의 비율'은 배경이 0.00%, 글자 변경이 2.5% 로 확연히 갈린다.
    부드러운 애니메이션은 모든 칸을 조금씩 흔들 뿐 크게 바꾸지는 않기 때문이다.
    """
    if a is None or b is None or a.shape != b.shape:
        return 100.0
    return float((np.abs(a - b) > cell_delta).mean()) * 100.0
