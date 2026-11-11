"""화면을 지켜보다가 글자가 바뀌면 읽고 번역해서 오버레이로 넘기는 작업 스레드.

매 주기마다 하는 일은 가볍다: 캡처하고 성기게 뽑은 밝기로 이전과 비교.
OCR 은 화면이 잠깐 멈췄을 때만, 번역은 글자가 달라졌을 때만 돈다.

보이던 번역문이 낡으면(대사를 넘기면) OCR 을 기다리지 않고 바로 지운다.
오버레이는 캡처에 찍히지 않으므로 그 자리의 원본 픽셀을 그대로 비교할 수 있다.
"""
from __future__ import annotations

import ctypes
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from ctypes import wintypes

import numpy as np
from PIL import Image

from . import capture, config, glyph, layout, recruit, skillchip, slots, style, textnorm as tn, winutil
from . import errlog, ppocr
from . import ink as ink_mod
from .ocr import WindowsOCR

STRIP_MARGIN = 36
SAMPLE_STEP = 6          # 변화 감지는 6픽셀마다 한 점만 본다
CELL_DELTA = 16          # 이만큼 밝기가 달라진 점만 바뀐 점으로 센다
MIN_CHANGED_CELLS = 6    # 글자 하나가 바뀌면 약 10점이 바뀐다
STALE_DELTA = 24
STALE_RATIO = 0.16       # 블록 자리의 점이 이 비율 이상 바뀌면 그 번역문은 낡은 것이다
MAX_BACKOFF = 8.0

# 전투 화면 오른쪽 아래의 '剩余可放置角色：4'(배치 가능 인원). 전투에서만 보인다
BATTLE_MARK = "可放置角色"
BATTLE_ROI = (0.76, 0.72, 1.0, 0.84)   # 화면 비율 (x0, y0, x1, y1). 2560x1440 에서 (2169,1111) 근처
BATTLE_CHECK = 2.5                     # 전투 중에는 이 간격으로 그 구석만 읽어 끝났는지 본다
SHORT_LINE = 3                          # Windows OCR 이 못 읽는 짧은 대사의 최대 글자 수
RECHECK_DELAY = 0.25
ROOM_REACH = 240                        # 한 줄짜리 번역문이 넓어질 수 있는 바탕을 이만큼 옆까지 잰다
TINY_TEXT = 21                          # 2560x1440 에서 이보다 낮은 글자 줄은 장식·파티클의 헛것이다
WIDER = 1.15                            # 같은 자리를 이만큼 넓게 읽었으면 글자를 더 많이 읽은 것이다
REREAD_OVERLAP = 0.08                   # 화면을 넷으로 나눠 다시 읽을 때 조각끼리 겹치는 비율
REREAD_SAME = 0.8                       # 처음 읽은 글자가 지난번 나눠 읽은 화면과 이만큼 같으면 다시 나눠 읽지 않는다
EDGE = 3                                # 조각 가장자리에서 이만큼 안쪽까지 닿은 줄은 잘린 것으로 본다
STORY_KEEP = 500                        # 스토리 기록 탭에 남길 대사 수
STORY_SAME = 6                          # 최근 이만큼의 대사와 같으면 같은 대사를 다시 읽은 것이다
GAME_CLASS = "UnityWndClass"
GAME_TITLES = ("明日方舟", "Arknights")


@dataclass
class Shown:
    text: str
    via: str
    zh: str
    box: tuple[int, int, int, int]
    rows: int
    line_h: int
    ink_h: int
    bg: str
    fg: str
    plate: object | None
    cells: tuple[int, int, int, int]
    thumb: np.ndarray
    align: str = ""       # "left" 면 대사처럼 왼쪽에 붙여 쓴다
    predicted: bool = False   # OCR 이 아니라 대본에서 채웠다. 픽셀이 바뀌어 낡을 때까지 둔다
    room: tuple | None = None  # 글자 둘레 바탕(이름판·버튼)의 가로 범위. 번역문은 이 안에서만 넓힌다
    ink: object | None = None  # 원문 글자 획 (ink.Ink). 배경이 움직여도 글자가 그대로인지 이것으로 본다
    src: str = ""              # OCR 이 읽은 글자 그대로
    # 헛것을 떼거나 놓친 글자를 덮기 전 OCR 이 읽은 폭. 같은 글자를 '더 많이 읽었는지' 견줄 때 쓴다
    # (상자를 '。开始探索' → '开始探索' 로 줄였더니 '然开始探索' 가 더 넓어 보여 헛것 번역이 이겼다)
    read_w: int = 0
    icon: object | None = None  # 원문의 화폐 아이콘을 잘라 낸 그림. 번역문의 '▲' 자리에 붙인다

    def as_item(self) -> dict:
        return {"text": self.text, "box": self.box, "rows": self.rows, "line_h": self.line_h,
                "ink_h": self.ink_h, "bg": self.bg, "fg": self.fg, "plate": self.plate,
                "align": self.align, "room": self.room, "icon": self.icon}


@dataclass
class Status:
    state: str = "게임 찾는 중"
    ocr_ms: int = 0
    ocr_name: str = ""
    ocr_note: str = ""            # PaddleOCR 준비 상태·못 쓰는 까닭
    blocks: int = 0
    shown: int = 0
    recent: list = field(default_factory=list)
    # 지나간 스토리 대사 (패널의 '스토리' 탭): (앞에서 잘린 수, [StoryLine]). 엔진 스레드가 앞을 자를 때는 둘을 한꺼번에
    # 새 튜플로 바꿔 끼운다. 따로 두면 패널이 자르는 도중에 읽어 엉뚱한 줄을 그리거나 IndexError 가 났다
    story_log: tuple = (0, [])


@dataclass
class StoryLine:
    zh: str           # 게임 원문
    ko: str           # 띄운 번역
    via: str
    speaker: str      # 화자 (한국어 이름이 있으면 그것)
    title: str        # 스토리 파일 이름 (바뀌면 탭에 구분 줄을 넣는다)
    src: str          # OCR 이 읽은 글자


GAME_EXES = ("arknights.exe", "明日方舟.exe")


def find_game(own: set[int] | None = None, extra_titles=()) -> int:
    titles = tuple(GAME_TITLES) + tuple(t for t in extra_titles if t)
    hwnd = winutil.user32.FindWindowExW(None, None, GAME_CLASS, None)
    while hwnd:
        title = winutil.window_title(hwnd)
        if any(t in title for t in titles):
            return hwnd
        hwnd = winutil.user32.FindWindowExW(None, hwnd, GAME_CLASS, None)
    # 대체 탐색: 창 클래스가 다르거나(다른 렌더러·실행기) 제목이 조금 다른 경우.
    # 게임 실행 파일 이름이거나, 제목에 게임 이름이 든 눈에 보이는 창을 쓴다. 우리 창은 뺀다
    best = 0
    try:
        for w in winutil.list_windows(own or set()):
            exe = w["exe"].lower()
            if exe in GAME_EXES or any(t in w["title"] for t in titles):
                if "translator" in exe or "번역" in w["title"]:
                    continue
                if exe in GAME_EXES:
                    return w["hwnd"]      # 실행 파일이 확실하면 바로
                best = best or w["hwnd"]
    except Exception:
        pass
    return best


def game_visible(hwnd: int, rect, own: set[int]) -> bool:
    """게임 화면이 온전히 눈에 보이는가. 아니면 캡처하지 않는다 (다른 창이 찍히므로)."""
    if winutil.user32.GetForegroundWindow() == hwnd:
        return True    # 게임을 하는 중이다. 게임 위에 띄워 둔 항상 위 창은 사용자의 선택이다
    return winutil.covering_window(hwnd, rect, own) is None


class Engine(threading.Thread):
    def __init__(self, cfg: dict, resolver, post, own_windows):
        super().__init__(name="engine", daemon=True)
        self.cfg = cfg
        self.resolver = resolver
        self.post = post                 # post(items 또는 None): UI 스레드에 표시를 넘긴다
        self.own_windows = own_windows   # 우리 창 핸들 집합을 돌려주는 함수
        self.status = Status()
        self.paused = threading.Event()
        self.force = threading.Event()
        self.mt_ready = threading.Event()
        self.closing = threading.Event()

        self._region = None
        self._recheck_at = 0.0
        self.battle = False              # 전투 중이라 번역을 쉬는 중
        self._battle_miss = 0
        self.idle = threading.Event()    # 일시정지 상태로 손을 놓았다 (문제 화면 저장이 기다린다)
        self._dialog = None              # 마지막으로 확인된 대사 블록 (짧은 대사를 대신 띄울 자리)
        self._dialog_cells = None
        self.short_note = ""             # 짧은 대사를 채웠거나 못 채운 이유 (문제 화면 기록용)
        self._shown: dict[str, Shown] = {}
        # 공개모집 화면이면 ★4 이상 확정 태그 조합을 표시한다 (중국 서버 모집 목록). 모집 표가 없는 DB 면 꺼진다
        self._recruit = recruit.Recruiter(resolver.db, self._ko)
        self._blocks: list = []          # 지난번 인식 결과 (번역이 도착하면 다시 쓴다)
        self._frame: Image.Image | None = None
        self._small: np.ndarray | None = None
        self._seen: set[str] = set()     # 지난번에 본 글자들
        self._story_file = -2            # 스토리 기록 탭: 마지막 대사의 스토리 파일과 그 이름
        self._story_title = ""
        self._reread_keys: set[str] = set()   # 마지막으로 나눠 읽은 화면의 글자들
        self._plates = ThreadPoolExecutor(max_workers=4, thread_name_prefix="plate")
        self._paddle = None              # 준비를 마친 PaddleOCR (ppocr.py)
        self._paddle_thread = None
        self._ocr_used = ""              # 지난번에 쓴 인식기. 바뀌면 띄운 번역문을 비우고 새로 읽는다
        resolver.on_ready = self.mt_ready.set

    def stop(self) -> None:
        self.closing.set()

    def reset(self) -> None:
        """표시 설정이 바뀌었을 때 다음 주기에 처음부터 다시 그리게 한다."""
        self._shown.clear()
        self.force.set()

    def current_region(self):
        return self._region

    # ---------------------------------------------------------------- 메인 루프
    def run(self) -> None:
        try:
            win = WindowsOCR()
        except Exception as exc:
            errlog.write("Windows OCR 시작 실패", True)
            self.status.state = str(exc)
            self.resolver.log(str(exc))
            return
        hwnd = 0
        next_find = 0.0
        region = None
        prev = None
        dirty = True
        dirty_since = last_change = time.time()
        gap = 0.0
        noise = 0.0
        last_ocr = 0.0
        next_reload = 0.0
        next_battle_check = 0.0

        while not self.closing.is_set():
            started = time.time()
            interval = float(self.cfg.get("interval_ms", 150)) / 1000.0
            try:
                if started >= next_reload:
                    next_reload = started + 2.0
                    if self.resolver.reload_files():
                        self._shown.clear()
                        self.post({})
                        dirty, dirty_since = True, started

                if started >= next_find or not winutil.window_alive(hwnd):
                    hwnd = find_game(self.own_windows(), [self.cfg.get("game_title", "")])
                    next_find = started + 3.0
                if not hwnd:
                    self.status.state = "게임을 기다리는 중"
                    self._drop_all()
                    self.closing.wait(1.0)
                    continue

                rect = winutil.window_rect(hwnd, "client")
                own = self.own_windows()
                if not rect or not game_visible(hwnd, rect, own):
                    self.status.state = "게임이 가려져 있음"
                    self.post(None)
                    prev = None
                    self.closing.wait(0.3)
                    continue

                if self.paused.is_set():
                    self.status.state = "일시정지"
                    self.idle.set()
                    self.closing.wait(0.1)
                    continue
                self.idle.clear()

                if rect != region:
                    region = self._region = rect
                    self._shown.clear()
                    self.post({})
                    prev = None

                frame, small = _grab(rect)

                if self.battle and not self.force.is_set():
                    # 전투 중에는 화면 전체를 읽지 않는다. 구석만 가끔 보고 전투가 끝났는지 확인한다
                    self.status.state = "전투 중 · 번역 쉬는 중"
                    self._drop_stale(small, frame)     # '지금 번역' 으로 띄운 글자가 낡으면 지운다
                    self._battle_mt(region)
                    if started >= next_battle_check:
                        next_battle_check = started + BATTLE_CHECK
                        # 스킬 연출이 잠깐 가릴 수 있어 두 번 연속 안 보여야 끝난 것으로 본다
                        ocr = self._pick_ocr(win, frame)
                        self._battle_miss = 0 if self._battle_mark(ocr, frame) else self._battle_miss + 1
                        if self._battle_miss >= 2:
                            self.battle = False
                            self._small = None      # 전투가 끝난 화면을 처음부터 읽는다
                            dirty, dirty_since, prev, gap = True, started, None, 0.0
                    spent = time.time() - started
                    self.closing.wait(max(0.0, interval * 2 - spent))
                    continue

                if self._drop_stale(small, frame):
                    gap = 0.0   # 사라진 자리에 무엇이 왔는지 바로 읽는다

                if prev is not None and prev.shape == small.shape:
                    changed = float((np.abs(small - prev) > CELL_DELTA).mean()) * 100.0
                else:
                    changed = 100.0
                prev = small

                now = time.time()
                # 움직이는 배경은 쉬지 않고 칸을 바꾼다. 그 화면의 평소 움직임보다 큰 변화만
                # '글자가 찍히는 중'으로 본다. 정지 화면이면 기준이 낮아 짧은 대사도 잡힌다
                noise += (min(changed, 1.5) - noise) * 0.08
                threshold = max(float(self.cfg.get("change_ratio", 0.03)), noise * 2.2)
                if changed >= threshold:
                    last_change = now
                    gap = 0.0

                # 대사가 몇 글자씩 찍히면 한 주기의 변화는 기준에 못 미칠 수 있다.
                # 그래서 직전 프레임뿐 아니라 마지막으로 읽은 화면과도 비교해 쌓인 변화를 잡는다
                if not dirty and self._differs_from_read(small):
                    dirty, dirty_since = True, now

                settled = now - last_change >= float(self.cfg.get("settle_ms", 280)) / 1000.0
                due = now - last_ocr >= gap
                # 배경이 쉬지 않고 움직여 멈추지 않는 화면을 위한 안전장치
                limit = float(self.cfg.get("max_wait_ms", 2500)) / 1000.0 + gap
                overdue = dirty and now - max(dirty_since, last_ocr) >= limit
                forced = self.force.is_set()

                if forced or (dirty and settled and due) or overdue:
                    self.force.clear()
                    self.status.state = "번역 중"
                    same = self._ocr_pass(self._pick_ocr(win, frame), frame, small, region, forced, forced)
                    last_ocr = time.time()
                    dirty = False
                    next_battle_check = last_ocr + BATTLE_CHECK
                    # 읽어도 글자가 그대로면 간격을 늘려, 전투 중에 OCR 로 CPU 를 쓰지 않는다
                    gap = min(MAX_BACKOFF, max(0.4, gap * 2)) if same else 0.0
                elif self._recheck_at and now >= self._recheck_at:
                    self._recheck_at = 0.0
                    # 화면이 그대로면 다시 읽어도 같은 글자다. OCR 없이 확인된 것으로 친다
                    if not self._differs_from_read(small):
                        self._apply(self._blocks, self._frame, self._small, region, True)
                        self.post(self._items())
                elif self.mt_ready.is_set():
                    self.mt_ready.clear()
                    # 번역을 기다리는 동안 대사를 넘겼을 수 있다. 자리가 그대로인 블록만 쓴다
                    alive = [b for b in self._blocks if self._still_there(b, small)]
                    self._apply(alive, self._frame, self._small, region, True)
                    self.post(self._items())
                self.status.state = "번역 중"
            except Exception as exc:
                errlog.write("화면 읽기·번역 중 오류", True)
                self.status.state = f"오류: {exc}"
                self.resolver.log(f"오류: {type(exc).__name__}: {exc}")
                self.closing.wait(1.0)

            spent = time.time() - started
            if spent < interval:
                self.closing.wait(interval - spent)

    def _differs_from_read(self, small: np.ndarray) -> bool:
        """마지막으로 읽은 화면과 달라졌는가. 글자 하나쯤의 변화도 잡는다."""
        if self._small is None or self._small.shape != small.shape:
            return True
        return int((np.abs(small - self._small) > CELL_DELTA).sum()) >= MIN_CHANGED_CELLS

    # ---------------------------------------------------------------- 낡은 글자
    def _still_there(self, block, small: np.ndarray) -> bool:
        if self._small is None or self._small.shape != small.shape:
            return False
        (x0, y0, x1, y1), then = _cells_of(self._small, block.box)
        now = small[y0:y1, x0:x1]
        return now.size > 0 and float((np.abs(now - then) > STALE_DELTA).mean()) <= STALE_RATIO

    def _battle_mt(self, region) -> None:
        """전투 중 '지금 번역'(Ctrl+Alt+T)으로 읽은 글 가운데 기계 번역을 기다리던 것이 오면 띄운다.

        기계 번역 결과는 평소 흐름(run 의 mt_ready)에서만 띄웠는데, 전투 중에는 그 흐름을 건너뛰어 공식·묶음 번역이 없는
        글은 전투 중에 끝내 안 떴다 (2026-11-08 사용자: '전투 중이면 무조건 안되고 있음'). 전투 화면은 쉬지 않고 움직여
        픽셀로 '그대로인가' 를 보면 거의 다 빠진다. 읽은 블록을 다 넘기고, 글자가 사라졌으면 다음 주기에
        _drop_stale 이 글자 획으로 보고 지운다."""
        if not self.mt_ready.is_set() or not self._blocks:
            return
        self.mt_ready.clear()
        self._apply(self._blocks, self._frame, self._small, region, True)
        self.post(self._items())

    def _drop_all(self) -> None:
        if self._shown:
            self._shown.clear()
        self.post({})

    def _drop_stale(self, small: np.ndarray, frame) -> bool:
        """원문 글자가 바뀐 번역문을 지운다. 지운 것이 있으면 True.

        자리의 픽셀이 바뀌었으면 글자 획만 다시 본다. 뒤 배경만 움직였으면 그대로 두고, 화면이 기울어
        글자가 옮겨 갔으면 번역문도 옮긴다. 글자가 정말 바뀌었거나 사라졌을 때만 지운다.
        """
        stale, moved = [], False
        g = None
        for item_id, shown in self._shown.items():
            x0, y0, x1, y1 = shown.cells
            now_cells = small[y0:y1, x0:x1]
            if now_cells.shape == shown.thumb.shape and now_cells.size and \
                    float((np.abs(now_cells - shown.thumb) > STALE_DELTA).mean()) <= STALE_RATIO:
                continue
            if shown.ink is None:
                stale.append(item_id)
                continue
            if g is None:
                g = ink_mod.green(frame)
            offset = ink_mod.locate(g, shown.box, shown.ink)
            if offset is None:
                stale.append(item_id)
                continue
            if offset != (0, 0):
                self._move(shown, *offset)
                moved = True
            # 지금 픽셀을 새 기준으로 삼는다. 배경이 계속 움직이면 다음 주기에 다시 글자 획만 본다
            shown.cells, shown.thumb = _cells_of(small, shown.box)
        for item_id in stale:
            del self._shown[item_id]
        if stale or moved:
            self.post(self._items())
        return bool(stale)

    @staticmethod
    def _move(shown: Shown, dx: int, dy: int) -> None:
        x, y, w, h = shown.box
        shown.box = (x + dx, y + dy, w, h)
        if shown.room:
            shown.room = (shown.room[0] + dx, shown.room[1] + dx)

    # ------------------------------------------------------------ 읽고 번역하기
    def _ocr_pass(self, ocr, frame, small, region, confirmed: bool, full: bool = False) -> bool:
        """화면을 읽는다. 지난번과 글자가 같으면 True.

        전체 화면은 0.4~0.5초 걸리지만 대사창 띠만 읽으면 0.1초 안쪽이다. 축소해서 읽으면
        더 빠르지만 줄의 23% 가 달라져서 쓰지 않는다. 대신 바뀐 세로 구간만 읽는다.
        """
        started = time.perf_counter()
        keep: list = []
        strip = None if full else self._changed_strip(small, frame.height)
        if strip:
            y0, y1 = strip
            lines = [
                l.shifted(0, y0) for l in ocr.recognize(frame.crop((0, y0, frame.width, y1)))
            ]
            keep = [b for b in self._blocks if b.box[1] + b.box[3] < y0 or b.box[1] > y1]
        else:
            lines = ocr.recognize(frame)
        lines = layout.split_chips([l for l in lines if l.text.strip()], frame)
        lines = layout.fill_gaps(lines, self.resolver.db.glob_keys)
        styles = [style.analyze(frame, l.box, l.h) for l in lines]
        fresh_blocks = [b for b in layout.group_lines(lines, styles, image=frame) if tn.has_cjk(b.text)]
        fresh_blocks += self._one_char_names(frame, keep + fresh_blocks)
        blocks = keep + fresh_blocks
        self.status.ocr_ms = int((time.perf_counter() - started) * 1000)
        self.status.ocr_name = ocr.name
        self.status.blocks = len(blocks)

        # 전투가 시작되면 번역을 쉰다. '지금 번역' 으로 직접 부른 때는 보여 준다
        if (not full and self.cfg.get("battle_pause", True)
                and any(BATTLE_MARK in tn.key(b.text) for b in blocks)):
            self.battle, self._battle_miss = True, 0
            self._shown.clear()
            self._blocks, self._seen, self._dialog = [], set(), None
            self._frame, self._small = frame, small
            self.post({})
            return True

        keys = {tn.key(b.text) for b in blocks}
        # 코스트처럼 숫자만 도는 전투 HUD 는 같은 화면으로 친다
        same = {_no_digits(k) for k in keys} == {_no_digits(k) for k in self._seen}
        self._frame, self._small, self._blocks = frame, small, blocks
        self._apply(blocks, frame, small, region, confirmed)
        self._seen = keys
        self._fill_short_line(frame, small, fresh_blocks, strip)
        self.post(self._items())
        if strip is None and ocr.needs_reread and self._needs_reread(keys, full):
            self._reread(ocr, frame, small, region, confirmed, lines)
        return same

    # ------------------------------------------------------------ 글자 인식기 고르기
    def _pick_ocr(self, win, frame):
        """설정(ocr_engine)에 맞는 글자 인식기. PaddleOCR 은 뒤에서 준비가 끝날 때까지 Windows OCR 로 읽는다.

        auto 는 GPU(DirectML·CUDA)를 잡았고 화면 한 장 추정이 ppocr.SLOW_LIMIT 초 안쪽일 때만 PaddleOCR 을 쓴다.
        paddle 은 CPU 라도 쓴다 (한 장 3~4초). 모델이나 onnxruntime 이 없으면 늘 Windows OCR.
        """
        ocr = self._choose_ocr(win, frame)
        if ocr.name != self._ocr_used:
            if self._ocr_used:
                # PaddleOCR 을 준비하는 동안 Windows OCR 이 읽은 헛것('章收1品0《0对0')은 글자 획이 그대로라 계속
                # 남는다. 인식기가 바뀌면 띄운 것을 비우고 처음부터 읽는다 (이 함수는 엔진 스레드에서만 부른다)
                self._shown.clear()
                self._blocks, self._seen, self._reread_keys = [], set(), set()
                self.post({})
            self._ocr_used = ocr.name
        return ocr

    def _choose_ocr(self, win, frame):
        mode = self.cfg.get("ocr_engine", "auto")
        if mode == "windows":
            return win
        paddle = self._paddle
        if paddle is not None and (mode == "paddle" or paddle.usable):
            return paddle
        if self._paddle_thread is None:
            if not ppocr.available(config.MODEL_DIR):
                self.status.ocr_note = "PaddleOCR 모델이나 onnxruntime 이 없어 Windows OCR 을 씁니다"
                self._paddle_thread = False
            else:
                self._paddle_thread = threading.Thread(target=self._load_paddle, args=(frame.width, frame.height),
                                                       name="paddle", daemon=True)
                self._paddle_thread.start()
        return win

    def _load_paddle(self, width: int, height: int) -> None:
        self.status.ocr_note = "PaddleOCR 준비 중 (그동안 Windows OCR)"
        try:
            meta = self.resolver.db.meta
            paddle = ppocr.PaddleOCR(config.MODEL_DIR, meta.get("charset", ""), rare=meta.get("rare", ""))
            paddle.usable = False
            if paddle.backend == "CPU" and self.cfg.get("ocr_engine", "auto") != "paddle":
                self.status.ocr_note = "GPU 를 못 찾아 Windows OCR 을 씁니다"
            else:
                seconds = paddle.warm(width, height)
                paddle.usable = paddle.backend != "CPU" and seconds <= ppocr.SLOW_LIMIT
                self.status.ocr_note = (f"{paddle.name} · 한 장 약 {seconds:.1f}초" if paddle.usable or
                                        self.cfg.get("ocr_engine") == "paddle"
                                        else f"PaddleOCR 이 느려(한 장 약 {seconds:.1f}초) Windows OCR 을 씁니다")
            self._paddle = paddle
            self.resolver.log(self.status.ocr_note)
            self.force.set()    # 준비되면 지금 화면을 새 인식기로 다시 읽는다
        except Exception as exc:  # 모델·런타임 문제는 Windows OCR 로 버틴다
            errlog.write("PaddleOCR 준비 실패", True)
            self.status.ocr_note = f"PaddleOCR 을 켜지 못해 Windows OCR 을 씁니다: {exc}"
            self.resolver.log(self.status.ocr_note)

    def _needs_reread(self, keys: set[str], forced: bool) -> bool:
        """이 화면을 이미 나눠 읽었으면 다시 하지 않는다. 움직이는 화면에서 매번 두 번씩 읽지 않게."""
        keys = {_no_digits(k) for k in keys}    # 코스트처럼 숫자만 도는 글자는 같은 화면으로 친다
        done = self._reread_keys
        if not forced and done and len(keys & done) >= REREAD_SAME * max(1, len(keys)):
            return False
        self._reread_keys = keys
        return True

    def _reread(self, ocr, frame, small, region, confirmed: bool, lines) -> None:
        """화면을 넷으로 나눠 다시 읽고, 처음에 아무것도 못 읽은 자리의 줄만 더한다.

        전체 화면을 한 번에 읽으면 Windows OCR 이 줄 몇 개를 그냥 건너뛴다 (통합전략 모집 카드의 '八幡海铃',
        '崖心', '空构', 메인 화면의 '基建'). 같은 크기 그대로 잘라 읽으면 읽힌다 (키우면 오히려 못 읽는다).
        처음 읽은 번역은 이미 띄웠으므로 이 0.4~0.5초를 기다리게 하지 않는다.
        """
        extra = quarter_lines(ocr, frame, lines)
        if not extra:
            return
        extra = layout.fill_gaps(layout.split_chips(extra, frame), self.resolver.db.glob_keys)
        styles = [style.analyze(frame, l.box, l.h) for l in extra]
        more = [b for b in layout.group_lines(extra, styles, image=frame) if tn.has_cjk(b.text)]
        more += self._one_char_names(frame, self._blocks + more)
        if not more:
            return
        self._blocks = self._blocks + more
        self.status.blocks = len(self._blocks)
        self._apply(self._blocks, frame, small, region, confirmed)
        self.post(self._items())

    def _one_char_names(self, frame, blocks) -> list:
        """OCR 이 놓친 한 글자 이름('宴')을 오퍼레이터 카드 격자에서 찾아 블록으로 만든다."""
        out = []
        for ch, box in slots.one_char_names(frame, blocks, self.resolver.names):
            st = style.analyze(frame, box, box[3])
            out.append(layout.Block(text=ch, box=box, line_h=box[3], ink_h=st.ink_h or box[3], style=st))
        return out

    def _battle_mark(self, ocr, frame) -> bool:
        x0, y0, x1, y1 = BATTLE_ROI
        w, h = frame.width, frame.height
        crop = frame.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))
        return any(BATTLE_MARK in tn.key(line.text) for line in ocr.recognize(crop))

    # ------------------------------------------------------------- 짧은 대사
    def _fill_short_line(self, frame, small, fresh_blocks, strip) -> None:
        """OCR 이 못 읽는 한두 글자 대사('嗯。', '好。')를 대본에서 채운다.

        스토리를 따라가는 중에 대사 자리가 바뀌고 거기 글자가 생겼는데 OCR 이 아무것도 못 읽었고,
        대본상 다음 줄이 짧은 대사라면 그 줄이다. 대사를 아주 빨리 넘기면 엇나갈 수 있다.
        """
        dialog = self._dialog
        if dialog is None or self.resolver.db.file_id < 0:
            self.short_note = "따라가는 스토리 없음"
            return
        box = dialog["box"]
        x, y, w, h = box
        if strip and not (strip[0] <= y and y + h <= strip[1]):
            self.short_note = "이번에 대사 자리를 다시 읽지 않음"
            return
        read = [b for b in fresh_blocks if _overlaps(b.box, box)]
        read_boxes = {b.box for b in read}
        if any(v.box in read_boxes for v in self._shown.values() if not v.predicted):
            self.short_note = "대사 자리에서 읽은 글자의 번역이 떠 있음"
            return
        # OCR 은 '喂！？' 를 '日畏| 7' 처럼 엉뚱하게 읽기도 한다. 짧은 헛것만 읽혔으면 대본에서 채우고,
        # 긴 글자가 읽혔으면 짧은 대사가 아니다
        if sum(len(tn.key(b.text)) for b in read) > SHORT_LINE + 3:
            self.short_note = f"대사 자리에서 긴 글자가 읽힘: {read[0].text}"
            return
        x0, y0, x1, y1 = self._dialog_cells
        then, now = dialog["thumb"], small[y0:y1, x0:x1]
        if now.shape != then.shape or float((np.abs(now - then) > STALE_DELTA).mean()) <= STALE_RATIO:
            self.short_note = "대사 자리가 그대로"
            return
        line_h = dialog["line_h"]
        probe = (x, y, max(line_h * 3, 1), max(h // max(1, dialog["rows"]), line_h))
        if not _has_ink(frame, probe, dialog["fg"]):
            self.short_note = "대사창이 비어 있음"
            return
        nxt = self.resolver.db.next_line()
        if not nxt or nxt[4] != "say" or not tn.has_cjk(nxt[2]) or len(nxt[1]) > SHORT_LINE:
            self.short_note = f"다음 줄이 짧은 대사가 아님: {nxt[2] if nxt else None}"
            return
        res = self.resolver.resolve(nxt[2])
        if not res.text:
            self.short_note = f"다음 줄 번역을 기다리는 중: {nxt[2]}"
            return
        st = dialog["style"]
        plate = None
        if self.cfg.get("box_style", "blend") == "blend" and st is not None:
            plate = style.make_plate(frame, probe, st.dark_text, line_h)
        cells, thumb = _cells_of(small, probe)
        self._shown[f"{nxt[1]}@{x // 16},{y // 16}"] = Shown(
            text=res.text, via=res.via, zh=nxt[2], box=probe, rows=1, line_h=line_h,
            ink_h=dialog["ink_h"], bg=dialog["bg"], fg=dialog["fg"], plate=plate,
            cells=cells, thumb=thumb, align="left", predicted=True,
        )
        self._dialog_cells, self._dialog["thumb"] = _cells_of(small, box)
        self._remember(nxt[2], res)
        self.short_note = f"대본에서 채움: {nxt[2]}"
        self.status.shown = len(self._shown)

    def _changed_strip(self, small: np.ndarray, height: int) -> tuple[int, int] | None:
        """지난번 인식 이후 바뀐 세로 구간. 화면의 절반을 넘으면 None (전부 다시 읽는다)."""
        if self._small is None or self._small.shape != small.shape:
            return None
        rows = np.nonzero((np.abs(small - self._small) > CELL_DELTA).any(axis=1))[0]
        if rows.size == 0:
            return None
        y0 = int(rows[0]) * SAMPLE_STEP - STRIP_MARGIN
        y1 = (int(rows[-1]) + 1) * SAMPLE_STEP + STRIP_MARGIN
        # 구간에 걸친 블록은 잘리면 안 되므로 그 블록이 다 들어오도록 넓힌다
        for block in sorted(self._blocks, key=lambda b: b.box[1]):
            top, bottom = block.box[1], block.box[1] + block.box[3]
            if top <= y1 and bottom >= y0:
                y0, y1 = min(y0, top - STRIP_MARGIN), max(y1, bottom + STRIP_MARGIN)
        y0, y1 = max(0, y0), min(height, y1)
        return (y0, y1) if y1 - y0 <= height * 0.5 else None

    def _apply(self, blocks, frame, small, region, confirmed: bool) -> None:
        if frame is None:
            return
        # 이보다 작은 글자는 장식 영문·파티클을 읽은 헛것이었다 ('以堡1趾跹' → '보 1 발가락').
        # 게임 데이터에서 확인되는 글자만 쓰고 번역기로는 보내지 않는다
        tiny = TINY_TEXT * frame.height / 1440
        # 공개모집 화면이면 OCR 이 못 읽은 태그 칸('治疗' → '冫口了')을 글자 모양으로 채운다
        blocks = self._recruit.fix_blocks(frame, blocks)
        # 스킬 화면의 초록 SP 회복 칸('自动回复')은 OCR 이 못 읽는다. 옆 '自动触发' 칸 자리로 찾아 글자 모양으로 읽는다
        blocks = skillchip.fix_blocks(frame, blocks)
        # 설명이 숫자 중간에서 두 블록으로 갈린 것('…缚地22' / '秒，随后…')을 이어 공식 글·묶음 틀과 맞춘다
        blocks = join_split_texts(blocks, self.resolver.known_text)
        wanted: dict[str, tuple] = {}
        reads: dict[str, int] = {}      # 상자를 손보기 전 OCR 이 읽은 폭 (Shown.read_w)
        for block in blocks:
            k = tn.key(block.text)
            small_text = block.line_h < tiny
            # 움직이는 배경 위에서는 같은 글자도 '行动力一', '了动力' 처럼 매번 다르게 읽힌다.
            # DB 에 없는 글자는 두 번 연속 같게 읽혔을 때만 번역기로 보낸다
            allow_mt = (confirmed or k in self._seen) and not small_text
            res = self.resolver.resolve(block.text, allow_mt=allow_mt, masked=block.masked,
                                        pick=_picker(frame, block))
            # 작은 헛것은 예전에 번역해 둔 것(번역캐시)이 있어도 쓰지 않는다
            # 작은 한 글자는 게임 데이터에 있어도 아이콘을 읽은 것이다 (PaddleOCR 이 사람 아이콘을 15px '人' 으로 읽었다)
            if res.via == "skip" or (small_text and (res.via not in ("official", "pack", "user")
                                                     or tn.cjk_count(k) <= 1)):
                continue
            if res.via == "pending" and not allow_mt:
                self._recheck_at = time.time() + RECHECK_DELAY
            if res.text:
                read_w = block.box[2]
                if res.trim != (0, 0):
                    block = _trim_box(block, res.trim, frame)
                if res.missed != (0, 0):
                    block = _cover_missed(block, res.missed)
                x, y, w, h = block.box
                wanted[f"{k}@{x // 16},{y // 16}"] = (block, res)
                reads[f"{k}@{x // 16},{y // 16}"] = read_w
            if res.story and len(k) > SHORT_LINE:
                self._note_dialog(block, small)
            self._remember(block.text, res)
            if res.story and res.text:
                self._note_story(block.text, res)

        # 떠 있는 번역문 가운데 원문 글자 획이 그대로인 것 (화면이 옮겨 갔으면 그만큼)
        g = ink_mod.green(frame)
        steady = {}
        for item_id, shown in self._shown.items():
            if not shown.predicted and shown.ink is not None:
                offset = ink_mod.locate(g, shown.box, shown.ink)
                if offset is not None:
                    steady[item_id] = offset
        # 그대로인 글자를 이번에 다르게 읽었으면 ('开始探索' 옆 불꽃을 '0开始探索' 로, '保密等级' 를 '等级' 로)
        # 더 나은 번역일 때만 바꾼다. 안 그러면 파티클이 글자 옆을 지날 때마다 번역문이 바뀐다
        held = set()
        for item_id, (block, res) in list(wanted.items()):
            if item_id in self._shown:
                continue
            rivals = [i for i in steady if i not in wanted and _overlaps(self._shown[i].box, block.box)]
            # 여러 버튼을 한 줄로 이어 읽은 덩어리('000．干员寻访', 524x115)는 그대로인 번역문 여럿을 덮는다. 쓰지 않는다
            if rivals and (len(rivals) > 1 or not _better(block, res, self._shown[rivals[0]], reads[item_id])):
                del wanted[item_id]
                held.update(rivals)
        read = [block.box for block, _res in wanted.values()]
        for item_id in [i for i, v in self._shown.items() if i not in wanted and not v.predicted]:
            # OCR 이 이번에 못 읽었어도 ('采购中心' → '采00') 글자 획이 그대로면 둔다. 더 나은 번역과 겹치면 지운다
            if item_id in steady and (item_id in held or not any(_overlaps(self._shown[item_id].box, box)
                                                                for box in read)):
                self._move(self._shown[item_id], *steady[item_id])
                continue
            del self._shown[item_id]

        # 번역이 더 나은 것으로 바뀐 블록(기계 번역 → Gemini)과 자리가 조금 옮겨 간 블록도 새로 만든다
        fresh = {i: v for i, v in wanted.items()
                 if i not in self._shown or self._shown[i].text != v[1].text or self._shown[i].box != v[0].box}
        if not fresh:
            self.status.shown = len(self._shown)
            return

        def build(entry):
            block, res = entry
            st = block.style
            plate = room = None
            if st is not None:
                if self.cfg.get("box_style", "blend") == "blend":
                    plate = style.make_plate(frame, block.box, st.dark_text, block.line_h)
                if block.rows == 1:
                    room = style.bg_room(frame, block.box, st.bg, max(ROOM_REACH, block.box[2] * 2))
            return plate, room

        built = list(self._plates.map(build, fresh.values()))
        for (item_id, (block, res)), (plate, room) in zip(fresh.items(), built):
            st = block.style
            cells, thumb = _cells_of(small, block.box)
            self._shown[item_id] = Shown(
                text=res.text, via=res.via, zh=res.zh, src=block.text, box=block.box, rows=block.rows,
                line_h=block.line_h, ink_h=block.ink_h,
                bg=st.bg_hex if st else "#111318", fg=st.fg_hex if st else "#F2F4F8",
                plate=plate, cells=cells, thumb=thumb,
                # 대사는 왼쪽부터 쓰인다. 가운데로 옮기면 화자 이름 쪽으로 밀려난다
                align="left" if res.story else "", room=room,
                ink=ink_mod.capture(g, block.box, st.dark_text) if st else None,
                read_w=reads.get(item_id, block.box[2]),
                icon=icon_crop(frame, block) if ICON in res.text else None,
            )
        self.status.shown = len(self._shown)

    def _note_dialog(self, block, small) -> None:
        """대사가 찍히는 자리와 모양을 기억해 둔다. 짧은 대사를 대신 띄울 때 쓴다."""
        st = block.style
        self._dialog_cells, thumb = _cells_of(small, block.box)
        self._dialog = {
            "box": block.box, "rows": block.rows, "line_h": block.line_h, "ink_h": block.ink_h,
            "style": st, "thumb": thumb,
            "bg": st.bg_hex if st else "#111318", "fg": st.fg_hex if st else "#F2F4F8",
        }

    # ------------------------------------------------------------- 문제 화면
    def snapshot(self) -> dict:
        """지금 상태를 기록용으로 뽑는다. 일시정지된 동안 부른다."""
        path, idx = self.resolver.db.position()
        return {
            "frame": self._frame._image() if isinstance(self._frame, _LazyFrame) else self._frame,
            "region": self._region,
            "blocks": [(b.text, b.box, b.rows) for b in self._blocks],
            "shown": [(v.via, v.zh, v.text, v.box) for v in self._shown.values()],
            "story": (path, idx),
            "battle": self.battle,
            "short_note": self.short_note,
            "ocr_ms": self.status.ocr_ms,
            "ocr_name": self.status.ocr_name,
            "ocr_note": self.status.ocr_note,
        }

    def _items(self) -> dict:
        """오버레이에 넘길 항목: 떠 있는 번역문 + (공개모집 화면이면) 확정 조합 태그 테두리와 목록."""
        items = {k: v.as_item() for k, v in self._shown.items()}
        if self.cfg.get("recruit_hint", True):
            items.update(self._recruit.items([(v.src, v.box, v.room) for v in self._shown.values()
                                              if not v.predicted]))
        return items

    def _ko(self, zh: str) -> str:
        """공개모집 목록에 쓸 태그·오퍼레이터 이름의 한국어 (공식 번역 → 번역 묶음 → 원문)."""
        _known, ko = self.resolver.db.text_ko(zh)
        if ko:
            return tn.display_ko(ko)
        packed = self.resolver.pack.get(tn.key(zh))
        return packed[0] if packed else zh

    def _note_story(self, text: str, res) -> None:
        """스토리 기록 탭에 대사를 남긴다. 같은 대사는 화면에 떠 있는 동안 여러 번 읽히니 최근 줄과 같으면
        번역만 바꾼다 (기계 번역 → Gemini 다듬기)."""
        cut, lines = self.status.story_log
        zh = res.zh or text
        for old in lines[-STORY_SAME:]:
            if old.zh == zh:
                old.ko, old.via = res.text, res.via
                return
        db = self.resolver.db
        file_id, speaker_zh, speaker_ko = db.story_info()
        if file_id != self._story_file:
            self._story_file = file_id
            self._story_title = db.story_title(file_id) if file_id >= 0 else ""
        speaker = speaker_ko or (self.resolver.speaker_ko(speaker_zh) if speaker_zh else "")
        lines.append(StoryLine(zh, res.text, res.via, speaker, self._story_title, text))
        if len(lines) > STORY_KEEP + 100:
            self.status.story_log = (cut + 100, lines[100:])

    def _remember(self, text: str, res) -> None:
        if not res.text:
            return
        recent = self.status.recent
        # (출처, OCR 이 읽은 글자, 번역, 맞춘 게임 원문). 오역 제보는 게임 원문으로 한다 (묶음 TSV 의 원문과 같다)
        line = (res.via, text, res.text, res.zh or text)
        if line in recent[-40:]:
            return
        recent.append(line)
        del recent[:-80]


def _trim_box(block, trim: tuple[int, int], frame):
    """헛것이라 떼어 낸 앞뒤 글자를 상자에서도 뺀다. '0通过步数' 의 '0' 은 옆의 큰 숫자 '19' 라서 상자 높이가
    두 배가 되어 번역문만 크게 떴다. 한 줄짜리만 낱자 상자로 자르고, 글자 색·획 높이도 남은 자리에서 다시 잰다."""
    head, tail = trim
    if block.rows > 1:
        return _drop_rows(block, head, tail, frame)
    if len(block.members) != 1 or not block.members[0].words:
        return block
    words = block.members[0].words
    chars = [(i, j, len(t)) for i, (t, *_r) in enumerate(words) for j in range(len(t)) if not t[j].isspace()]
    if len(chars) != sum(not ch.isspace() for ch in block.text):
        return block    # 낱자와 줄 글자가 어긋나면(조각을 이어 붙인 줄) 그대로 둔다
    head = sum(not ch.isspace() for ch in block.text[:head])
    tail = sum(not ch.isspace() for ch in block.text[len(block.text) - tail:]) if tail else 0
    if head + tail >= len(chars):
        return block
    (wa, ja, na), (wb, jb, nb) = chars[head], chars[len(chars) - 1 - tail]
    _t, ax, _ay, aw, _ah = words[wa]
    _t, bx, _by, bw, _bh = words[wb]
    x0 = int(ax + aw * ja / na)
    x1 = int(round(bx + bw * (jb + 1) / nb))
    y0 = min(w[2] for w in words[wa:wb + 1])
    y1 = max(w[2] + w[4] for w in words[wa:wb + 1])
    box = (x0, y0, max(1, x1 - x0), max(1, y1 - y0))
    st = style.analyze(frame, box, box[3]) or block.style
    ink = getattr(st, "ink_h", 0) or box[3]
    return replace(block, box=box, line_h=box[3], ink_h=ink, style=st)


def _drop_rows(block, head: int, tail: int, frame):
    """여러 줄 블록에서 떼어 낸 앞뒤 글자가 줄 하나와 꼭 맞으면 그 줄을 상자에서 뺀다. 이름표 아래 영문 장식
    ('档案' / 'ARCHIVES')을 같이 읽은 블록이 영문 줄까지 덮어 지웠다."""
    def count(s: str) -> int:
        return sum(not ch.isspace() for ch in s)

    members = list(block.members)
    if len(members) != block.rows:
        return block
    h = count(block.text[:head])
    t = count(block.text[len(block.text) - tail:]) if tail else 0
    while members and h and h >= count(members[0].text):
        h -= count(members.pop(0).text)
    while members and t and t >= count(members[-1].text):
        t -= count(members.pop().text)
    if h or t or not members or len(members) == len(block.members):
        return block
    x0 = min(m.box[0] for m in members)
    y0 = min(m.box[1] for m in members)
    x1 = max(m.box[0] + m.box[2] for m in members)
    y1 = max(m.box[1] + m.box[3] for m in members)
    box = (x0, y0, x1 - x0, y1 - y0)
    line_h = max(m.box[3] for m in members)
    st = style.analyze(frame, box, line_h) or block.style
    return replace(block, text="".join(m.text for m in members), box=box, rows=len(members), members=members,
                   line_h=line_h, ink_h=getattr(st, "ink_h", 0) or line_h, style=st)


ICON = "▲"                  # 번역문의 화폐 아이콘 자리 (resolver.ICON)
ICON_READS = "▲△"           # OCR 이 그 아이콘을 읽은 글자
ICON_COLOR_MIN = 60.0       # 바탕과 이만큼 색이 다른 점을 아이콘으로 본다


def icon_crop(frame, block):
    """원문 줄에서 화폐 아이콘('▲' 로 읽힌 글자)의 자리를 잘라 낸 그림. 번역문의 '▲' 자리에 그대로 붙인다.
    OCR 의 낱자 상자는 가로 중심만 믿고, 줄 높이만 한 네모를 잘라 바탕색과 다른 점이 있는 데까지 좁힌다. 없으면 None."""
    for member in block.members:
        for word, x, y, w, h in getattr(member, "words", ()) or ():
            j = next((i for i, ch in enumerate(word) if ch in ICON_READS), -1)
            if j < 0:
                continue
            cx = x + w * (j + 0.5) / max(1, len(word))
            line = member.box
            side = int(line[3] * 1.3)
            x0, y0 = int(cx - side / 2), int(line[1] + line[3] / 2 - side / 2)
            x0, y0 = max(0, x0), max(0, y0)
            x1, y1 = min(frame.width, x0 + side), min(frame.height, y0 + side)
            if x1 - x0 < 4 or y1 - y0 < 4:
                return None
            arr = np.asarray(frame.crop((x0, y0, x1, y1)).convert("RGB"), dtype=np.float32)
            edge = np.concatenate([arr[0], arr[-1], arr[:, 0], arr[:, -1]])
            ink = np.sqrt(((arr - np.median(edge, axis=0)) ** 2).sum(axis=2)) > ICON_COLOR_MIN
            # 옆 글자('以')의 획이 네모 끝에 걸리면 같이 잘려 아이콘 옆에 점이 붙었다. 가운데에서 이어진 칸까지만
            ca, cb = _run(ink.any(axis=0), int(cx) - x0)
            ra, rb = _run(ink[:, ca:cb].any(axis=1), ink.shape[0] // 2)
            if cb - ca < 3 or rb - ra < 3:
                return None
            pad = 2
            return frame.crop((x0 + max(0, ca - pad), y0 + max(0, ra - pad),
                               x0 + min(x1 - x0, cb + pad), y0 + min(y1 - y0, rb + pad)))
    return None


def _run(filled, center: int, gap: int = 2) -> tuple[int, int]:
    """center 에서 양쪽으로, 빈 칸이 gap 개 넘게 이어지기 전까지 (시작, 끝+1)."""
    n = len(filled)
    center = min(max(0, center), n - 1)
    lo = hi = center
    empty = 0
    while lo > 0 and empty <= gap:
        lo -= 1
        empty = 0 if filled[lo] else empty + 1
    lo += empty
    empty = 0
    while hi < n - 1 and empty <= gap:
        hi += 1
        empty = 0 if filled[hi] else empty + 1
    hi -= empty
    while lo < hi and not filled[lo]:
        lo += 1
    while hi > lo and not filled[hi]:
        hi -= 1
    return lo, hi + 1


def quarter_lines(ocr, frame, lines) -> list:
    """화면을 넷으로 나눠 다시 읽은 줄 가운데 처음 읽기(lines)에 없던 자리의 줄 (Engine._reread)."""
    W, H = frame.width, frame.height
    found = [l.box for l in lines]
    extra = []
    for x0, y0, x1, y1 in _quarters(W, H):
        for l in ocr.recognize(frame.crop((x0, y0, x1, y1))):
            if not l.text.strip():
                continue
            # 조각 경계에 걸려 잘린 줄은 버린다 (처음 읽기나 옆 조각에 온전히 있다)
            if ((x0 > 0 and l.x <= EDGE) or (x1 < W and l.x + l.w >= x1 - x0 - EDGE)
                    or (y0 > 0 and l.y <= EDGE) or (y1 < H and l.y + l.h >= y1 - y0 - EDGE)):
                continue
            box = (l.x + x0, l.y + y0, l.w, l.h)
            if any(_overlaps(box, b) for b in found):
                continue
            found.append(box)
            extra.append(l.shifted(x0, y0))
    return extra


def frame_blocks(ocr, frame, resolver, recruiter) -> list:
    """화면 한 장을 엔진과 같은 순서로 번역할 블록까지 만든다 (도구·시험용. 전체 화면을 한 번에 읽는 경우만).
    tools/regress.py, tools/rerender.py 가 쓴다. 엔진의 차례를 바꾸면 여기도 같이 바꾼다."""
    from types import SimpleNamespace

    lines = [l for l in ocr.recognize(frame) if l.text.strip()]
    if ocr.needs_reread:
        lines += quarter_lines(ocr, frame, lines)
    lines = layout.fill_gaps(layout.split_chips(lines, frame), resolver.db.glob_keys)
    styles = [style.analyze(frame, l.box, l.h) for l in lines]
    blocks = [b for b in layout.group_lines(lines, styles, image=frame) if tn.has_cjk(b.text)]
    blocks += Engine._one_char_names(SimpleNamespace(resolver=resolver), frame, blocks)
    blocks = recruiter.fix_blocks(frame, blocks)
    blocks = skillchip.fix_blocks(frame, blocks)
    return join_split_texts(blocks, resolver.known_text)


JOIN_GAP = 1.6      # 이을 두 블록의 세로 틈 상한 (글자 높이 배수)
JOIN_LEFT = 0.6     # 두 블록 왼쪽 끝 차이 상한 (글자 높이 배수)
JOIN_MAX = 4        # 한 번에 이을 블록 수 상한


_NUMBER_TAIL = re.compile(r"\s*\d+(?:\.\d+)?\s*%?\s*[秒次倍层点个格名人天回]?\s*[。.，,）)]?\s*")


def _number_tail(text: str) -> bool:
    """'2秒' '15%' '3次' 처럼 숫자와 단위 하나뿐인 조각 (윗줄 설명이 줄바꿈으로 갈린 끝)."""
    return bool(_NUMBER_TAIL.fullmatch(text))


def join_split_texts(blocks: list, known) -> list:
    """위아래로 붙은 블록들이 저마다는 번역이 없는데 이으면 공식 글이나 묶음 틀과 맞으면 하나로 잇는다.

    스킬 설명 '攻击范围扩大…飞行敌人缚地22' 과 '秒，随后攻击间隔…' 이 줄 간격 때문에 두 블록으로 갈려, 설명 전체의
    숫자 틀과 맞지 않고 반쪽씩 기계 번역됐다. 같은 설명이 세 블록(첫 줄 / 둘째 줄 / 나머지)으로 갈리기도 했다.
    known(글) → 번역이 정해진 글인가 (Resolver.known_text).
    """
    order = sorted(range(len(blocks)), key=lambda i: (blocks[i].box[1], blocks[i].box[0]))
    merged: dict[int, object] = {}
    gone: set[int] = set()
    for pos, i in enumerate(order):
        if i in gone or not tn.has_cjk(blocks[i].text) or known(blocks[i].text):
            continue
        chain = [blocks[i]]
        used = []
        for j in order[pos + 1:]:
            if len(chain) >= JOIN_MAX:
                break
            if j in gone:
                continue
            a, b = chain[-1], blocks[j]
            ax, ay, aw, ah = a.box
            bx, by = b.box[:2]
            h = max(1, a.line_h)
            gap = by - (ay + ah)
            if gap > h * JOIN_GAP:
                break
            if gap < -h * 0.3 or abs(bx - chain[0].box[0]) > h * JOIN_LEFT or not tn.has_cjk(b.text):
                continue
            # 따로 번역이 있는 블록은 잇지 않는다. 다만 '2秒' 같은 숫자+단위 꼬리는 그 자체로 공식 번역('2초')이 있어도
            # 윗줄 설명의 끝이다 ('…受到停顿效果，持续' + '2秒', 2026-11-08 화면). 안 이으면 설명의 숫자 수가 모자라 틀에서 빠졌다
            if abs(b.line_h - a.line_h) > max(a.line_h, b.line_h) * 0.25 or (known(b.text) and not _number_tail(b.text)):
                continue
            chain.append(b)
            used.append(j)
            text = chain[0].text
            for part in chain[1:]:
                text = layout._join(text, part.text)
            if not known(text):
                continue
            x0 = min(c.box[0] for c in chain)
            y0 = min(c.box[1] for c in chain)
            x1 = max(c.box[0] + c.box[2] for c in chain)
            y1 = max(c.box[1] + c.box[3] for c in chain)
            merged[i] = replace(chain[0], text=text, box=(x0, y0, x1 - x0, y1 - y0),
                                rows=sum(c.rows for c in chain),
                                members=[m for c in chain for m in c.members], masked="", unknown=())
            gone.update(used)
            break
    if not merged:
        return blocks
    return [merged.get(i, blk) for i, blk in enumerate(blocks) if i not in gone]


def _cover_missed(block, missed: tuple[int, int]):
    """OCR 이 놓친 이름 앞뒤 글자 자리까지 덮는다. 안 그러면 번역문 옆에 '克' 같은 글자가 남는다.

    읽은 글자 앞쪽에는 아이콘 헛것('/')이 붙기도 해서, 앞 글자를 놓쳤으면 오른쪽 끝을 기준으로 잰다.
    """
    before, after = missed
    x, y, w, h = block.box
    step = round(h * 1.08)               # 글자 하나의 가로 간격 (글자 높이 + 자간)
    seen = sum("㐀" <= ch <= "鿿" for ch in block.text)
    x0 = min(x, x + w - (seen + before) * step) if before else x
    x1 = max(x + w, x0 + (before + seen + after) * step) if after else x + w
    return replace(block, box=(max(0, x0), y, x1 - max(0, x0), h))


def _picker(frame, block):
    """반쪽으로 갈라 읽은 글자('?')를 화면 글자 모양으로 고르는 함수. resolver 가 이름 후보를 줄 때 쓴다."""
    if not block.unknown or block.style is None:
        return None
    dark = block.style.dark_text

    def pick(found: list[str], holes: list[int]) -> str | None:
        if len(holes) != len(block.unknown):
            return None
        chosen = []
        for i, (x, y, w, h) in zip(holes, block.unknown):
            ch = glyph.best_char(frame.crop((x - 3, y - 3, x + w + 3, y + h + 3)), [k[i] for k in found], dark)
            if ch is None:
                return None
            chosen.append((i, ch))
        left = [k for k in found if all(k[i] == ch for i, ch in chosen)]
        return left[0] if len(left) == 1 else None

    return pick


def _quarters(w: int, h: int) -> list[tuple[int, int, int, int]]:
    """화면을 2x2 로 조금씩 겹치게 나눈 조각 (x0, y0, x1, y1)."""
    out = []
    for j in range(2):
        for i in range(2):
            out.append((max(0, int(i * w / 2 - w * REREAD_OVERLAP)), max(0, int(j * h / 2 - h * REREAD_OVERLAP)),
                        min(w, int((i + 1) * w / 2 + w * REREAD_OVERLAP)),
                        min(h, int((j + 1) * h / 2 + h * REREAD_OVERLAP))))
    return out


def _overlaps(a, b) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def _has_ink(frame, box, fg_hex: str) -> bool:
    """box 안에 대사 글자색 픽셀이 있는가. 빈 대사창과 글자가 찍힌 대사창을 가른다."""
    x, y, w, h = box
    crop = np.asarray(frame.crop((x, y, x + w, y + h)).convert("RGB"), dtype=np.int16)
    if crop.size == 0:
        return False
    fg = np.array([int(fg_hex[i:i + 2], 16) for i in (1, 3, 5)], dtype=np.int16)
    close = (np.abs(crop - fg).sum(axis=2) < 90).mean()
    return close > 0.01


def _no_digits(key: str) -> str:
    return "".join(ch for ch in key if not ch.isdigit())


# 번역 출처의 믿을 만한 정도
_RANK = {"user": 3, "official": 2, "pack": 2, "cache": 1, "mt": 1}


def _better(block, res, old: Shown, read_w: int) -> bool:
    """같은 자리의 글자 획이 그대로일 때, 이번에 읽은 번역이 떠 있는 것보다 나은가.

    출처가 더 믿을 만하면 낫다 (기계 번역 '0부터 탐험하기' < 공식 '탐험 시작하기'). 같으면 글자를 눈에 띄게 더 많이
    읽었을 때만 ('等级' < '保密等级'). 숫자만 다르면 숫자가 바뀐 것이다 ('12天' → '11天').
    폭은 상자를 손보기 전 OCR 이 읽은 폭끼리 견준다 (read_w).
    """
    h, old_h = block.box[3], old.box[3]
    if not old_h / 1.5 <= h <= old_h * 1.5:
        return False    # 크기가 다르면 같은 글자를 달리 읽은 것이 아니다 (덩어리로 이어 읽은 것)
    new_k, old_k = tn.key(block.text), tn.key(old.src)
    if new_k != old_k and _no_digits(new_k) == _no_digits(old_k):
        return True
    new, now = _RANK.get(res.via, 0), _RANK.get(old.via, 0)
    if new != now:
        return new > now
    return read_w > (old.read_w or old.box[2]) * WIDER


def _grab(rect) -> tuple[Image.Image, np.ndarray]:
    x, y, w, h = rect
    raw = capture._sct().grab({"left": x, "top": y, "width": w, "height": h})
    arr = np.frombuffer(raw.raw, dtype=np.uint8).reshape(raw.height, raw.width, 4)
    small = arr[::SAMPLE_STEP, ::SAMPLE_STEP, 1].astype(np.int16)
    return _LazyFrame(arr), small


class _LazyFrame:
    """OCR 을 안 하는 주기에는 RGB 변환을 건너뛰도록 필요할 때만 이미지를 만든다."""

    def __init__(self, arr: np.ndarray):
        self._arr = arr
        self._img = None

    def _image(self) -> Image.Image:
        if self._img is None:
            self._img = Image.fromarray(self._arr[:, :, [2, 1, 0]])
        return self._img

    def __getattr__(self, name):
        return getattr(self._image(), name)


def _cells_of(small: np.ndarray, box) -> tuple[tuple[int, int, int, int], np.ndarray]:
    x, y, w, h = box
    x0, y0 = max(0, x // SAMPLE_STEP), max(0, y // SAMPLE_STEP)
    x1 = min(small.shape[1], (x + w) // SAMPLE_STEP + 1)
    y1 = min(small.shape[0], (y + h) // SAMPLE_STEP + 1)
    return (x0, y0, x1, y1), small[y0:y1, x0:x1].copy()
