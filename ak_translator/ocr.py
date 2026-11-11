"""Windows 내장 중국어 OCR.

게임이 GPU 를 쓰고 있어도 영향을 주지 않고, 게임 글꼴을 잘 읽으며(剩余可放置角色：4 를
12ms 에 정확히 읽음) 설치할 것이 없다.
"""
from __future__ import annotations

import asyncio
import math
import threading
from dataclasses import dataclass, replace

from PIL import Image

from winsdk.windows.globalization import Language
from winsdk.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
from winsdk.windows.media.ocr import OcrEngine
from winsdk.windows.security.cryptography import CryptographicBuffer

LANG_TAGS = ("zh-Hans-CN", "zh-Hans", "zh-CN")

_loop_local = threading.local()


@dataclass
class OCRLine:
    text: str
    box: tuple[int, int, int, int]
    conf: float = 1.0
    line_h: int = 0
    # 넓은 한자가 반쪽 둘로 읽힌 자리를 '?' 로 바꾼 글자와 그 자리들. 없으면 빈 값
    masked: str = ""
    unknown: tuple = ()
    # 낱자(단어)마다 (글자, x, y, 폭, 높이). 나란한 꼬리표를 바탕색으로 가를 때 쓴다
    words: tuple = ()

    def __post_init__(self):
        if not self.line_h:
            self.line_h = self.box[3]

    def shifted(self, dx: int, dy: int) -> "OCRLine":
        """잘라 읽은 조각의 좌표를 화면 좌표로 옮긴다."""
        return replace(
            self, box=(self.x + dx, self.y + dy, self.w, self.h),
            unknown=tuple((x + dx, y + dy, w, h) for x, y, w, h in self.unknown),
            words=tuple((t, x + dx, y + dy, w, h) for t, x, y, w, h in self.words),
        )

    @property
    def x(self) -> int:
        return self.box[0]

    @property
    def y(self) -> int:
        return self.box[1]

    @property
    def w(self) -> int:
        return self.box[2]

    @property
    def h(self) -> int:
        return self.box[3]


def _run(coro):
    loop = getattr(_loop_local, "loop", None)
    if loop is None:
        loop = asyncio.new_event_loop()
        _loop_local.loop = loop
    return loop.run_until_complete(coro)


def _is_cjk(ch: str) -> bool:
    return "　" <= ch <= "鿿" or "＀" <= ch <= "￯"


# 숫자와 그 주변 기호. 이 사이에는 공백이 들어갈 일이 없다 ("+15%" 등).
_NUMERIC = set("0123456789+-.,%/:×")


def _join_words(words) -> str:
    """Windows OCR 은 숫자와 기호도 글자마다 '단어'로 끊는다. 간격을 봐서 이어 붙인다."""
    items = [(w.text, w.bounding_rect) for w in words if w.text]
    if not items:
        return ""
    line_h = max((r.height for _t, r in items), default=1.0) or 1.0
    threshold = max(2.0, line_h * 0.32)

    out = items[0][0]
    prev = items[0][1]
    for text, rect in items[1:]:
        gap = rect.x - (prev.x + prev.width)
        if (
            _is_cjk(out[-1]) or _is_cjk(text[0])
            or (out[-1] in _NUMERIC and text[0] in _NUMERIC)
            or gap < threshold
        ):
            out += text
        else:
            out += " " + text
        prev = rect
    return out


class WindowsOCR:
    name = "Windows OCR"
    needs_reread = True     # 전체를 한 번에 읽으면 줄을 건너뛴다 (engine._reread)

    def __init__(self):
        self.engine = None
        for tag in LANG_TAGS:
            self.engine = OcrEngine.try_create_from_language(Language(tag))
            if self.engine is not None:
                break
        if self.engine is None:
            raise RuntimeError("Windows 중국어(간체) 언어 팩이 없어 글자를 읽을 수 없습니다. "
                               "설정 → 시간 및 언어 → 언어 및 지역 → 언어 추가 → 中文(简体) 에서 '텍스트 인식'을 받고 다시 실행하세요.")

    def recognize(self, image: Image.Image, scale: float = 1.0) -> list[OCRLine]:
        work = image
        if scale != 1.0:
            work = image.resize((int(image.width * scale), int(image.height * scale)), Image.LANCZOS)
        raw = work.convert("RGBA").tobytes()
        # RGBA -> BGRA
        buf = bytearray(raw)
        buf[0::4], buf[2::4] = raw[2::4], raw[0::4]
        cbuf = CryptographicBuffer.create_from_byte_array(bytes(buf))
        bitmap = SoftwareBitmap.create_copy_from_buffer(
            cbuf, BitmapPixelFormat.BGRA8, work.width, work.height, BitmapAlphaMode.PREMULTIPLIED
        )
        result = _run(self.engine.recognize_async(bitmap))
        angle = result.text_angle or 0.0

        out: list[OCRLine] = []
        for line in result.lines:
            words = list(line.words)
            if abs(angle) >= MIN_ANGLE:
                words = [_unrotate(w, angle, work.width, work.height) for w in words]
            for words in _split_sizes(words):
                x0 = min(w.bounding_rect.x for w in words)
                y0 = min(w.bounding_rect.y for w in words)
                x1 = max(w.bounding_rect.x + w.bounding_rect.width for w in words)
                y1 = max(w.bounding_rect.y + w.bounding_rect.height for w in words)
                text = _join_words(words).strip()
                if text:
                    merged, unknown = _merge_halves(words)
                    out.append(OCRLine(
                        text=text,
                        box=(int(x0 / scale), int(y0 / scale),
                             int((x1 - x0) / scale), int((y1 - y0) / scale)),
                        masked=_join_words(merged).strip() if unknown else "",
                        unknown=tuple((int(r.x / scale), int(r.y / scale),
                                       int(r.width / scale), int(r.height / scale)) for r in unknown),
                        words=tuple((w.text, int(w.bounding_rect.x / scale), int(w.bounding_rect.y / scale),
                                     int(w.bounding_rect.width / scale), int(w.bounding_rect.height / scale))
                                    for w in words if w.text),
                    ))
        return out


TITLE_RATIO = 1.4   # 이만큼 큰 글자가 벌어져 붙어 있으면 다른 줄이다
MIN_ANGLE = 0.05    # OCR 이 이만큼 넘게 기울었다고 보면 글자 위치를 되돌린다 (도)


def _unrotate(word, angle: float, width: int, height: int):
    """OCR 이 기울었다고 본 화면에서 읽은 글자 위치를 원래 화면 좌표로 되돌린다.

    Windows OCR 은 글자가 기울었다고 판단하면(TextAngle) 화면을 그만큼 돌려서 읽고, 글자 상자도 돌린
    좌표로 준다. 터미널처럼 비스듬한 장식이 많은 화면에서는 같은 화면을 읽어도 1.9° 와 0° 가 번갈아 나와,
    번역문이 화면 끝에서 30~40px 씩 오락가락했다. 화면 가운데를 축으로 angle 만큼 돌리면 원래 자리에
    3px 안쪽으로 맞는다 (2026-11-04 실측).
    """
    r = word.bounding_rect
    rad = math.radians(angle)
    cx, cy = width / 2, height / 2
    x, y = r.x + r.width / 2 - cx, r.y + r.height / 2 - cy
    nx = x * math.cos(rad) - y * math.sin(rad) + cx
    ny = x * math.sin(rad) + y * math.cos(rad) + cy
    return _Word(word.text, _Rect(nx - r.width / 2, ny - r.height / 2, r.width, r.height))


def _is_ideograph(text: str) -> bool:
    return bool(text) and all("㐀" <= ch <= "鿿" for ch in text)


def _split_sizes(words) -> list[list]:
    """큰 제목과 본문이 한 줄로 잡힌 것을 나눈다.

    로딩 화면 설명은 왼쪽에 큰 제목('炎国')이 있고 그 오른쪽에 본문이 이어진다. OCR 은 제목을
    본문 둘째 줄의 앞머리로 읽어서, 본문이 문단으로 묶이지 못하고 번역문도 제목 자리까지 퍼진다.
    한자끼리 크기가 1.4배 넘게 다르고 사이가 벌어져 있으면 끊는다. '一' 나 문장 부호처럼 원래
    납작한 글자는 크기 비교에서 뺀다.
    """
    if len(words) < 2:
        return [words] if words else []
    heights = sorted(w.bounding_rect.height for w in words)
    floor = heights[len(heights) // 2] * 0.5
    out = [[words[0]]]
    for prev, word in zip(words, words[1:]):
        a, b = prev.bounding_rect, word.bounding_rect
        small, big = sorted((a.height, b.height))
        gap = b.x - (a.x + a.width)
        if (_is_ideograph(prev.text) and _is_ideograph(word.text) and small >= floor
                and big >= small * TITLE_RATIO and gap >= small * 0.6):
            out.append([word])
        else:
            out[-1].append(word)
    return out


class _Rect:
    def __init__(self, x, y, width, height):
        self.x, self.y, self.width, self.height = x, y, width, height


class _Word:
    def __init__(self, text, rect):
        self.text, self.bounding_rect = text, rect


def _halves(a, b, char_h: float) -> bool:
    """두 낱자가 한 글자의 반쪽씩인가. 둘 다 좁고, 합친 폭이 한 글자 폭이다."""
    if not (len(a.text) == len(b.text) == 1 and _is_ideograph(a.text) and _is_ideograph(b.text)):
        return False
    ra, rb = a.bounding_rect, b.bounding_rect
    narrow = min(ra.width, rb.width) < char_h * 0.6 and max(ra.width, rb.width) < char_h * 0.85
    return narrow and rb.x + rb.width - ra.x <= char_h * 1.15


def _merge_halves(words) -> tuple[list, list]:
    """넓은 한자가 반쪽 둘로 읽힌 자리를 '?' 하나로 바꾼다.

    OCR 은 '棘' 을 '束束', '叶' 를 '卩十', '佳' 를 '亻圭' 처럼 왼쪽·오른쪽으로 갈라 읽는다. 갈라진 두 조각은
    각각 글자 폭의 절반쯤이고 합치면 꼭 한 글자 폭이라 자리로 알아볼 수 있다. 무슨 글자인지는 모르므로
    '?' 로 두고, 이름 사전에서 맞는 것을 찾는다 (resolver).
    """
    heights = sorted(w.bounding_rect.height for w in words if len(w.text) == 1 and _is_ideograph(w.text))
    if len(heights) < 2:
        return list(words), []
    char_h = heights[len(heights) // 2]
    out, unknown, i = [], [], 0
    while i < len(words):
        a = words[i]
        b = words[i + 1] if i + 1 < len(words) else None
        if b is not None and _halves(a, b, char_h):
            ra, rb = a.bounding_rect, b.bounding_rect
            y0 = min(ra.y, rb.y)
            y1 = max(ra.y + ra.height, rb.y + rb.height)
            rect = _Rect(ra.x, y0, rb.x + rb.width - ra.x, y1 - y0)
            out.append(_Word("?", rect))
            unknown.append(rect)
            i += 2
        else:
            out.append(a)
            i += 1
    return out, unknown
