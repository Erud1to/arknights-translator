"""오류 기록: 잡히지 않은 예외, 작업 스레드·화면 처리에서 난 예외를 data/오류기록.txt 에 남긴다.

exe 판은 콘솔이 없어 오류가 어디에도 남지 않았다. 문제 화면 저장(debug_report)이 이 파일의 끝부분을 같이 적는다.
파일이 MAX_BYTES 를 넘으면 '오류기록.old.txt' 로 돌리고 새로 쓴다. 같은 오류가 잇달아 나면 SAME_GAP 초 동안은 한 번만 적는다.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback

from .version import VERSION

FILE = "오류기록.txt"
MAX_BYTES = 512 * 1024
SAME_GAP = 60.0

_path = ""
_lock = threading.Lock()
_last: dict[str, float] = {}


def setup(data_dir: str) -> str:
    """기록 파일을 정하고 잡히지 않은 예외(주 스레드·작업 스레드)를 기록하게 한다."""
    global _path
    os.makedirs(data_dir, exist_ok=True)
    _path = os.path.join(data_dir, FILE)
    previous = sys.excepthook

    def hook(kind, value, tb):
        write("잡히지 않은 오류", (kind, value, tb))
        previous(kind, value, tb)

    def thread_hook(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "?"
        write(f"'{name}' 스레드 오류", (args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = hook
    threading.excepthook = thread_hook
    return _path


def write(where: str, exc_info=None, note: str = "") -> None:
    """exc_info 가 True 면 지금 처리 중인 예외를 적는다."""
    if not _path:
        return
    if exc_info is True:
        exc_info = sys.exc_info()
    if exc_info and exc_info[0] is None:
        exc_info = None
    detail = f"{exc_info[0].__name__}: {exc_info[1]}" if exc_info else note
    now = time.time()
    try:
        with _lock:
            same = f"{where}|{detail}"
            if now - _last.get(same, 0.0) < SAME_GAP:
                return
            _last[same] = now
            if os.path.exists(_path) and os.path.getsize(_path) > MAX_BYTES:
                os.replace(_path, os.path.splitext(_path)[0] + ".old.txt")
            with open(_path, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {where} (프로그램 {VERSION})\n")
                if note:
                    f.write(note.rstrip() + "\n")
                if exc_info:
                    f.write("".join(traceback.format_exception(*exc_info)))
                f.write("\n")
    except OSError:
        pass


def tail(chars: int = 4000) -> str:
    """기록 파일의 끝부분 (문제 화면 보고서에 붙인다). 없으면 빈 글."""
    try:
        with open(_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - chars * 3))
            return f.read().decode("utf-8", errors="replace")[-chars:]
    except OSError:
        return ""
