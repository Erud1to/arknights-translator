"""제어 패널. 게임 화면을 가리지 않게 보조 모니터가 있으면 그쪽에 띄운다."""
from __future__ import annotations

import ctypes
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import font as tkfont
from tkinter import messagebox, ttk

import requests

from . import capture, config, dbfetch, debug_report, errlog, hotkeys, mt, theme, version, winutil
from . import pack as pack_mod
from . import textnorm as tn
from . import engine as engine_mod
from .engine import Engine
from .overlay import Overlay
from .resolver import Resolver

COMMITS_URL = "https://api.github.com/repos/ArknightsAssets/ArknightsGamedata/commits?per_page=50"
# 번역에 쓰는 서버. 대만·영어·일본 서버만 바뀐 커밋은 받아도 달라지는 게 없다
USED_SERVERS = {"cn", "kr"}
SERVER_NAMES = {"cn": "중국", "kr": "한국"}


def servers_changed(commits: list[dict], revision: str) -> set[str]:
    """지금 DB 를 만든 커밋 이후, 번역에 쓰는 서버가 바뀐 커밋이 있으면 그 서버들.

    커밋 메시지가 'Arknights update cn,en,jp,kr,tw' 꼴이다. 꼴이 다르거나 지금 DB 의 커밋을
    목록에서 못 찾으면(오래돼서) 무엇이 바뀌었는지 모르므로 둘 다 바뀐 것으로 친다.
    """
    found: set[str] = set()
    for commit in commits:
        if commit.get("sha") == revision:
            return found
        message = (commit.get("commit") or {}).get("message", "")
        match = re.match(r"Arknights update ([a-z,\s]+)$", message.strip())
        if not match:
            return set(USED_SERVERS)
        found |= {s.strip() for s in match.group(1).split(",")} & USED_SERVERS
    return set(USED_SERVERS)

STYLE_LABELS = {"blend": "원문 지우고 덮기", "fill": "상자로 덮기", "outline": "자막 (원문 비침)"}
OCR_LABELS = {"auto": "자동 (GPU 있으면 PaddleOCR)", "windows": "Windows OCR", "paddle": "PaddleOCR (GPU 없어도)"}
ENGINE_LABELS = {"google": "구글 번역 (키 불필요)", "papago": "Papago (키 필요)", "deepl": "DeepL (키 필요)"}
# 키를 받는 곳. DeepL 의 무료 키 가입은 이제 'API Developer'(총 100만 자, 초기화 없음)로 이어진다
KEY_LINKS = {
    "deepl": (("내 키 보기", "https://www.deepl.com/your-account/keys"),
              ("무료 키 가입", "https://www.deepl.com/pro-api")),
}
VIA_LABELS = {"official": "공식", "pack": "번역 묶음", "user": "내 용어", "cache": "저장됨", "mt": "기계 번역"}
PEEK_POLL_MS = 40           # 원문 보기 단축키를 뗐는지 보는 간격
PEEK_MARGIN = 0.15          # 키 반복으로 뗐는지 알아낼 때, 반복이 늦게 와도 기다려 주는 여유 (초)
PEEK_REPEAT_MIN = 0.12      # 반복 사이를 이것보다 짧게는 보지 않는다 (초)
FIX_ORIGIN = "직접 고침"     # 패널의 '고치기' 로 내 용어집에 적은 줄의 출처 칸


class App:
    def __init__(self):
        self.cfg = config.load()
        self.root = tk.Tk()
        self.root.title("명일방주 번역 오버레이")
        # 버튼·타이머 안에서 난 오류. exe 판은 콘솔이 없어 기록 파일에만 남는다
        self.root.report_callback_exception = self._on_ui_error
        theme.apply(self.root)
        theme.dark_title_bar(self.root)
        self.root.minsize(440, 600)
        self._place_panel()

        self.log_lines: queue.Queue = queue.Queue()
        self.updates: queue.Queue = queue.Queue()
        self.alerts: queue.Queue = queue.Queue()
        self._quota_win = None
        self.resolver = Resolver(self.cfg, config.DATA_DIR, log=self.log_lines.put)
        self.resolver.alert = lambda kind, info: self.alerts.put((kind, info))
        self.overlay = Overlay(self.root, self.cfg)
        self.root_hwnd = winutil.get_hwnd(self.root)
        self.engine = Engine(self.cfg, self.resolver, self.updates.put, self._own_windows)
        self._scale_job = None
        self._last_recent = 0

        self._build_ui()
        self.hotkeys = hotkeys.HotkeyManager()
        keys = self.cfg["hotkeys"]
        self.hotkeys.register("translate", keys["translate"], self.engine.force.set)
        self.hotkeys.register("toggle", keys["toggle"], lambda: self.root.after(0, self.toggle))
        self.hotkeys.register("capture", keys["capture"], lambda: self.root.after(0, self.save_problem))
        self.hotkeys.register("quit", keys["quit"], lambda: self.root.after(0, self.quit))
        self._peek_vk = 0           # 원문 보기 단축키를 누르고 있는 동안 그 키 (0 이면 안 누름)
        self._peek_since = 0.0      # 이번에 처음 눌린 때 (time.monotonic)
        self._peek_last = 0.0       # 마지막으로 단축키가 들어온 때. 단축키 스레드가 적는다
        self._peek_keystate = True  # 키 상태를 읽을 수 있나. 못 읽으면 키 반복이 끊길 때를 뗀 것으로 본다
        self.hotkeys.register("peek", keys["peek"], self._peek_hit, repeat=True)
        self.hotkeys.start()
        for line in self.hotkeys.failed:
            self.log_lines.put(f"단축키 등록 실패: {line}")

        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.root.bind("<Configure>", self._remember_geometry)
        self.engine.start()
        threading.Thread(target=self._check_revision, daemon=True).start()
        self._pump()
        self._refresh_panel()

    # ---------------------------------------------------------------- 창 배치
    def _on_ui_error(self, kind, value, tb) -> None:
        errlog.write("화면 처리 오류", (kind, value, tb))
        try:
            self.msg_var.set(f"오류가 났습니다 ({kind.__name__}). data\\{errlog.FILE} 에 적어 두었습니다.")
        except (AttributeError, tk.TclError):
            pass

    def _place_panel(self) -> None:
        geo = self.cfg.get("panel_geometry")
        if geo:
            self.root.geometry(geo)
            return
        vx, vy, vw, vh = winutil.virtual_screen()
        sw = self.root.winfo_screenwidth()
        if vw > sw:
            # 게임을 가리지 않게 보조 모니터로 보낸다
            x = vx + 60 if vx < 0 else sw + 60
            self.root.geometry(f"480x760+{x}+{max(vy + 80, 40)}")
        else:
            self.root.geometry(f"480x760+{max(0, sw - 520)}+60")

    def _remember_geometry(self, event) -> None:
        if event.widget is self.root:
            self.cfg["panel_geometry"] = self.root.geometry()

    def _own_windows(self) -> set[int]:
        return {self.root_hwnd, self.overlay.hwnd}

    # ------------------------------------------------------------------- 화면
    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True, padx=12, pady=(12, 8))

        # --- 머리: 이름, 상태 점, 통계, 큰 버튼 ---
        head = theme.card(outer)
        head.pack(fill="x")
        theme.stripes(head).pack(fill="x")
        top = ttk.Frame(head, style="Card.TFrame")
        top.pack(fill="x", padx=16, pady=(12, 0))
        theme.card_title(top, "명일방주 번역", "Arknights Translator").pack(side="left")
        self.state_var = tk.StringVar(value="시작하는 중")
        self._dots = {name: theme.dot(self.root, color) for name, color in
                      (("run", theme.OK), ("rest", theme.WARN), ("idle", theme.IDLE), ("error", theme.ERROR))}
        self.state_label = ttk.Label(top, textvariable=self.state_var, style="Card.TLabel",
                                     image=self._dots["idle"], compound="left")
        self.state_label.pack(side="right")
        self.stat_var = tk.StringVar()
        ttk.Label(head, textvariable=self.stat_var, style="Muted.TLabel").pack(anchor="w", padx=16, pady=(4, 0))
        row = ttk.Frame(head, style="Card.TFrame")
        row.pack(fill="x", padx=16, pady=(12, 14))
        self.toggle_btn = ttk.Button(row, text="일시정지", style="Accent.TButton", command=self.toggle)
        self.toggle_btn.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="지금 번역", command=self.engine.force.set).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="문제 화면 저장", command=self.save_problem).pack(side="left", padx=(8, 0))
        # 알릴 것이 있을 때만 나타난다
        self.msg_var = tk.StringVar()
        self.msg_label = ttk.Label(head, textvariable=self.msg_var, style="Error.TLabel", wraplength=420)
        self.msg_var.trace_add("write", lambda *_a: self._show_msg())

        # --- 탭 ---
        tabs = ttk.Notebook(outer)
        self.tabs = tabs
        tabs.pack(fill="both", expand=True, pady=(10, 0))
        recent = ttk.Frame(tabs, style="Card.TFrame")
        story = ttk.Frame(tabs, style="Card.TFrame")
        settings_outer, settings = theme.scrollable(tabs)
        data = ttk.Frame(tabs, style="Card.TFrame")
        tabs.add(recent, text="최근 번역")
        tabs.add(story, text="스토리")
        tabs.add(settings_outer, text="설정")
        tabs.add(data, text="데이터")
        self.settings_tab = settings_outer
        self._build_recent(recent)
        self._build_story(story)
        self._build_settings(settings)
        self._build_data(data)

        keys = self.cfg["hotkeys"]
        ttk.Label(outer, text=_hotkey_hint(keys), style="MutedBg.TLabel",
                  wraplength=450).pack(anchor="w", pady=(8, 0))

    def _show_msg(self) -> None:
        if self.msg_var.get():
            self.msg_label.pack(anchor="w", padx=16, pady=(0, 12))
        else:
            self.msg_label.pack_forget()

    def _build_recent(self, parent) -> None:
        self.tree = ttk.Treeview(parent, columns=("ko",), show="tree headings", height=3,
                                 selectmode="browse")
        self.tree.heading("#0", text="출처", anchor="w")
        self.tree.heading("ko", text="번역", anchor="w")
        self.tree.column("#0", width=92, stretch=False)
        self.tree.column("ko", width=300)
        self.tree.pack(fill="both", expand=True, padx=10, pady=(8, 0))
        self.tree.bind("<<TreeviewSelect>>", self._on_select_recent)
        self._source_dots = {via: theme.dot(self.root, color, 9) for via, color in theme.SOURCE_COLORS.items()}
        self._recent_src: dict[str, tuple] = {}     # 줄 → (출처, 읽은 글자, 번역, 게임 원문)
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x", padx=14, pady=(6, 10))
        # 번역이 이상하면 줄을 골라 제보한다. 번역 저장소의 이슈 쓰기 화면을 칸을 채워 열고, 제출은 사용자가 한다
        self.report_btn = ttk.Button(row, text="오역 제보 ↗", style="Link.TButton", command=self._report_recent,
                                     state="disabled")
        self.report_btn.pack(side="right", anchor="n")
        # 이 PC 에서 바로 바꾼다 (내 용어집에 적는다)
        self.fix_btn = ttk.Button(row, text="고치기", style="Link.TButton", state="disabled",
                                  command=lambda: self._fix_line(self._picked_recent()))
        self.fix_btn.pack(side="right", anchor="n")
        self.detail_var = tk.StringVar(value="줄을 고르면 원문이 여기 보입니다. 번역이 이상하면 '고치기' 나 '오역 제보'.")
        ttk.Label(row, textvariable=self.detail_var, style="Muted.TLabel",
                  wraplength=330, justify="left").pack(side="left", anchor="w", fill="x", expand=True)

    def _build_story(self, parent) -> None:
        """지나간 스토리 대사. 대사가 빨리 넘어가도 여기서 다시 읽는다. 줄을 누르면 원문을 보고 고치거나 제보한다."""
        family = tkfont.nametofont("TkDefaultFont").actual("family")
        box = ttk.Frame(parent, style="Card.TFrame")
        box.pack(fill="both", expand=True, padx=10, pady=(8, 0))
        text = tk.Text(box, wrap="word", bg=theme.FIELD, fg=theme.TEXT, relief="flat", borderwidth=0,
                       highlightthickness=0, padx=10, pady=8, font=(family, 11), cursor="arrow",
                       spacing1=3, spacing3=3, insertwidth=0, takefocus=0, exportselection=False, height=8)
        bar = ttk.Scrollbar(box, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=bar.set, state="disabled")
        bar.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        text.tag_configure("title", foreground=theme.MUTED, font=(family, 9), spacing1=12, spacing3=4)
        text.tag_configure("speaker", foreground=theme.MARK)
        text.tag_configure("picked", background=theme.SELECT)
        text.bind("<Button-1>", self._on_story_click)
        self.story_text = text
        self._story_drawn = 0           # 그린 대사 수 (앞에서 잘린 것까지 센 번호)
        self._story_rows: dict[int, object] = {}   # 번호 → engine.StoryLine (그린 것)
        self._story_ko: dict[int, str] = {}        # 번호 → 그릴 때의 번역 (나중에 다듬어지면 다시 그린다)
        self._story_title = None
        self._story_pick: int | None = None
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x", padx=14, pady=(6, 10))
        self.story_report_btn = ttk.Button(row, text="오역 제보 ↗", style="Link.TButton", state="disabled",
                                           command=lambda: self._report(self._picked_story(), self.story_detail))
        self.story_report_btn.pack(side="right", anchor="n")
        self.story_fix_btn = ttk.Button(row, text="고치기", style="Link.TButton", state="disabled",
                                        command=lambda: self._fix_line(self._picked_story()))
        self.story_fix_btn.pack(side="right", anchor="n")
        self.story_detail = tk.StringVar(value="지나간 대사가 여기 쌓입니다. 줄을 누르면 원문이 보입니다.")
        ttk.Label(row, textvariable=self.story_detail, style="Muted.TLabel",
                  wraplength=300, justify="left").pack(side="left", anchor="w", fill="x", expand=True)

    def _refresh_story(self) -> None:
        st = self.engine.status
        cut, lines = st.story_log
        lines = list(lines)     # 엔진 스레드가 덧붙이는 중에도 이번에 그릴 줄 수가 바뀌지 않게
        total = cut + len(lines)
        text = self.story_text
        at_end = text.yview()[1] > 0.98
        text.configure(state="normal")
        # 화면에 떠 있는 동안 번역이 더 나은 것으로 바뀐 대사 (기계 번역 → Gemini 다듬기)
        for n in [n for n in self._story_ko if n >= self._story_drawn - engine_mod.STORY_SAME]:
            line = self._story_rows[n]
            if line.ko != self._story_ko[n]:
                ranges = text.tag_ranges(f"ko{n}")
                if ranges:
                    text.delete(ranges[0], ranges[1])
                    text.insert(ranges[0], line.ko, (f"L{n}", f"ko{n}"))
                self._story_ko[n] = line.ko
        for n in range(max(self._story_drawn, cut), total):
            line = lines[n - cut]
            if line.title != self._story_title:
                self._story_title = line.title
                if line.title:
                    text.insert("end", f"{line.title}\n", ("title",))
            tag = f"L{n}"
            if line.speaker:
                text.insert("end", f"{line.speaker}  ", ("speaker", tag))
            text.insert("end", line.ko, (tag, f"ko{n}"))
            text.insert("end", "\n", (tag,))
            self._story_rows[n] = line
            self._story_ko[n] = line.ko
        self._story_drawn = total
        # 오래된 대사는 지운다 (엔진도 STORY_KEEP 줄만 둔다)
        old = sorted(self._story_rows)[:-engine_mod.STORY_KEEP]
        if old:
            end = text.tag_ranges(f"L{old[-1]}")
            if end:
                text.delete("1.0", end[1])
            for n in old:
                self._story_rows.pop(n, None)
                self._story_ko.pop(n, None)
            if self._story_pick in old:
                self._story_pick = None
        text.configure(state="disabled")
        if at_end:
            text.see("end")

    def _on_story_click(self, event) -> str:
        text = self.story_text
        tags = [t for t in text.tag_names(text.index(f"@{event.x},{event.y}")) if t.startswith("L")]
        text.tag_remove("picked", "1.0", "end")
        n = int(tags[0][1:]) if tags else None
        line = self._story_rows.get(n) if n is not None else None
        self._story_pick = n if line else None
        state = "normal" if line else "disabled"
        self.story_fix_btn.configure(state=state)
        self.story_report_btn.configure(state=state)
        if line:
            first, last = text.tag_ranges(f"L{n}")[:2]
            text.tag_add("picked", first, last)
            self.story_detail.set(f"원문: {line.zh}   ({VIA_LABELS.get(line.via, line.via)})")
        return "break"      # 글자 선택·커서를 만들지 않는다

    def _picked_story(self):
        line = self._story_rows.get(self._story_pick) if self._story_pick is not None else None
        return (line.via, line.src, line.ko, line.zh) if line else None

    def _picked_recent(self):
        sel = self.tree.selection()
        return self._recent_src.get(sel[0]) if sel else None

    def _fix_line(self, entry) -> None:
        """고른 줄의 번역을 이 PC 에서 바로 바꾼다. 내 용어집(data/용어집.md)에 적어 공식 번역·묶음보다 먼저 쓴다."""
        if not entry:
            return
        via, src, ko, zh = entry
        win = tk.Toplevel(self.root)
        win.title("번역 고치기")
        win.configure(bg=theme.BG)
        win.transient(self.root)
        box = theme.card(win)
        box.pack(fill="both", expand=True, padx=12, pady=12)
        theme.card_title(box, "번역 고치기").pack(anchor="w", padx=18, pady=(16, 8))
        ttk.Label(box, text=f"원문 ({VIA_LABELS.get(via, via)})", style="Muted.TLabel").pack(anchor="w", padx=18)
        ttk.Label(box, text=zh, style="Card.TLabel", wraplength=420, justify="left").pack(anchor="w", padx=18,
                                                                                          pady=(2, 10))
        ttk.Label(box, text="번역", style="Muted.TLabel").pack(anchor="w", padx=18)
        family = tkfont.nametofont("TkDefaultFont").actual("family")
        field = tk.Text(box, width=48, height=4, wrap="word", bg=theme.FIELD, fg=theme.TEXT, relief="flat",
                        insertbackground=theme.TEXT, highlightthickness=1, highlightbackground=theme.BORDER,
                        highlightcolor=theme.ACCENT, padx=8, pady=6, font=(family, 11))
        field.insert("1.0", ko)
        field.pack(fill="x", padx=18, pady=(2, 6))
        ttk.Label(box, text="이 PC 에서만 바뀝니다 (data\\용어집.md). 모두에게 고쳐지길 바라면 '오역 제보' 로 보내 주세요.",
                  style="Muted.TLabel", wraplength=420, justify="left").pack(anchor="w", padx=18)
        row = ttk.Frame(box, style="Card.TFrame")
        row.pack(fill="x", padx=18, pady=(14, 16))

        def save(_event=None):
            new = field.get("1.0", "end").strip()
            if "\n" not in ko:
                new = " ".join(new.split())     # 한 줄 번역에 Enter 로 들어간 줄바꿈은 뺀다
            win.destroy()
            if new and new != ko:
                self._save_fix(src, zh, new)
            return "break"

        ttk.Button(row, text="저장 (Ctrl+Enter)", style="Accent.TButton", command=save).pack(side="left")
        ttk.Button(row, text="취소", command=win.destroy).pack(side="right")
        field.bind("<Control-Return>", save)
        win.bind("<Escape>", lambda _e: win.destroy())
        theme.dark_title_bar(win)
        win.update_idletasks()
        win.geometry(f"+{self.root.winfo_rootx() + 20}+{self.root.winfo_rooty() + 120}")
        field.focus_set()
        field.tag_add("sel", "1.0", "end")

    def _save_fix(self, src: str, zh: str, new: str) -> None:
        store = self.resolver.user
        pairs = [(zh, new)]
        if src and tn.key(src) != tn.key(zh):
            pairs.append((src, new))     # 화면에서 읽은 글자가 게임 원문과 조금 달라도 맞게
        mine = {store.get(tn.key(x))[1] for x, _ko in pairs if store.get(tn.key(x))}
        try:
            store.replace(pairs, FIX_ORIGIN, mine | {FIX_ORIGIN})
        except OSError as exc:
            self.msg_var.set(f"용어집에 쓰지 못했습니다: {exc}")
            return
        # 떠 있는 탭의 글도 바꾼다
        for iid, (_via, s, _ko, z) in list(self._recent_src.items()):
            if tn.key(z) == tn.key(zh):
                self._recent_src[iid] = ("user", s, new, z)
                self.tree.item(iid, text=f" {VIA_LABELS.get('user', 'user')}",
                               image=self._source_dots.get("user", ""), values=(new,))
        text = self.story_text
        text.configure(state="normal")
        for n, line in self._story_rows.items():
            if tn.key(line.zh) == tn.key(zh):
                line.ko, line.via = new, "user"
                ranges = text.tag_ranges(f"ko{n}")
                if ranges:
                    text.delete(ranges[0], ranges[1])
                    text.insert(ranges[0], new, (f"L{n}", f"ko{n}"))
                self._story_ko[n] = new
        text.configure(state="disabled")
        self.msg_var.set("고쳤습니다. 내 용어집(data\\용어집.md)에 적었고 화면에도 바로 반영합니다.")
        self.engine.force.set()

    def _build_settings(self, grid) -> None:
        grid.columnconfigure(1, weight=1)
        pad = {"padx": (16, 16)}
        r = 0

        def head(text):
            nonlocal r
            theme.section(grid, text).grid(row=r, column=0, columnspan=2, sticky="w", padx=16,
                                           pady=(14 if r else 12, 4))
            r += 1

        def labeled(text, widget):
            nonlocal r
            ttk.Label(grid, text=text, style="Card.TLabel").grid(row=r, column=0, sticky="w", padx=(16, 0), pady=4)
            widget.grid(row=r, column=1, sticky="ew", padx=(12, 16), pady=4)
            r += 1

        head("화면")
        self.style_var = tk.StringVar(value=STYLE_LABELS[self.cfg["box_style"]])
        box = ttk.Combobox(grid, textvariable=self.style_var, state="readonly", values=list(STYLE_LABELS.values()))
        box.bind("<<ComboboxSelected>>", self._on_style)
        labeled("표시 방식", box)
        self.scale_var = tk.DoubleVar(value=self.cfg["font_scale"])
        labeled("글자 크기", ttk.Scale(grid, from_=0.7, to=1.3, variable=self.scale_var, command=self._on_scale))
        # 한글이 들어 있는 글꼴만 고르게 한다. 고르는 즉시 화면의 번역문을 다시 그린다
        family = self.cfg.get("font_family") or config.FONT_FAMILY
        if family == "Malgun Gothic" and "맑은 고딕" in tkfont.families(self.root):
            family = "맑은 고딕"    # 같은 글꼴이다. 목록에는 한국어 이름으로 나온다
        self.font_var = tk.StringVar(value=family)
        box = ttk.Combobox(grid, textvariable=self.font_var, state="readonly", height=16,
                           values=_korean_fonts(self.root, self.font_var.get()))
        box.bind("<<ComboboxSelected>>", self._on_font)
        labeled("글꼴", box)
        self.font_preview = ttk.Label(grid, text="미리보기  오퍼레이터 · 탐험 시작 · 12일", style="Card.TLabel")
        self.font_preview.grid(row=r, column=1, sticky="w", padx=(12, 16), pady=(0, 4))
        r += 1
        self._show_font_preview()
        self.battle_var = tk.BooleanVar(value=bool(self.cfg.get("battle_pause", True)))
        ttk.Checkbutton(grid, text="전투 중에는 번역 쉬기", variable=self.battle_var,
                        command=self._on_battle).grid(row=r, column=0, columnspan=2, sticky="w", **pad, pady=(4, 0))
        r += 1
        self.recruit_var = tk.BooleanVar(value=bool(self.cfg.get("recruit_hint", True)))
        ttk.Checkbutton(grid, text="공개모집에서 ★4 이상 확정 태그 조합 표시 (9시간 기준)", variable=self.recruit_var,
                        command=self._on_recruit).grid(row=r, column=0, columnspan=2, sticky="w", **pad, pady=(2, 0))
        r += 1

        head("글자 인식")
        self.ocr_var = tk.StringVar(value=OCR_LABELS.get(self.cfg.get("ocr_engine", "auto"), OCR_LABELS["auto"]))
        box = ttk.Combobox(grid, textvariable=self.ocr_var, state="readonly", values=list(OCR_LABELS.values()))
        box.bind("<<ComboboxSelected>>", self._on_ocr)
        labeled("OCR", box)
        # 지금 쓰는 인식기와 PaddleOCR 준비 상태 (패널 갱신 주기가 채운다)
        self.ocr_state = tk.StringVar()
        ttk.Label(grid, textvariable=self.ocr_state, style="Muted.TLabel", wraplength=380,
                  justify="left").grid(row=r, column=0, columnspan=2, sticky="w", **pad, pady=(0, 2))
        r += 1

        head("기계 번역")
        # 공식 번역·번역 묶음에 없는 글만 여기로 온다 (화면 글자의 15% 안팎: 숫자가 끼워진 글, 그림 속 글, 묶음 갱신 전 새 이벤트)
        ttk.Label(grid, text="공식 번역과 번역 묶음에 없는 글만 번역합니다. 구글은 키 없이 됩니다. "
                             "DeepL·Papago 는 자기 키가 있을 때만 고르세요.",
                  style="Muted.TLabel", wraplength=380, justify="left").grid(row=r, column=0, columnspan=2,
                                                                             sticky="w", **pad, pady=(0, 4))
        r += 1
        self.engine_var = tk.StringVar(value=ENGINE_LABELS[self.cfg["engine"]])
        box = ttk.Combobox(grid, textvariable=self.engine_var, state="readonly", values=list(ENGINE_LABELS.values()))
        box.bind("<<ComboboxSelected>>", self._on_engine)
        labeled("번역기", box)
        # 고른 번역기에 맞는 키 칸. 구글은 키가 없어 안내만 나온다
        self.key_frame = ttk.Frame(grid, style="Card.TFrame")
        self.key_frame.grid(row=r, column=0, columnspan=2, sticky="ew", **pad)
        self.key_frame.columnconfigure(1, weight=1)
        r += 1
        self.key_status = tk.StringVar()
        self._key_job = None
        self._key_entries: dict[str, ttk.Entry] = {}
        self._build_key_fields()

    def _build_data(self, parent) -> None:
        self.data_var = tk.StringVar(value="데이터 정보를 읽는 중…")
        theme.section(parent, "번역 데이터").pack(anchor="w", padx=16, pady=(14, 4))
        ttk.Label(parent, textvariable=self.data_var, style="Card.TLabel",
                  wraplength=400, justify="left").pack(anchor="w", padx=16)
        ttk.Button(parent, text="공식 데이터 갱신", style="Accent.TButton",
                   command=self.update_data).pack(anchor="w", padx=16, pady=(10, 0))
        ttk.Label(parent, text=("배포자가 만들어 올린 최신 공식 번역 데이터를 받습니다 (약 120MB, 1~2분)."
                                if dbfetch.base_url(self.cfg) else
                                "중국·한국 서버 게임 데이터를 새로 받아 번역 DB 를 다시 만듭니다. 게임을 안 할 때 누르세요 (약 10분)."),
                  style="Muted.TLabel", wraplength=400).pack(anchor="w", padx=16, pady=(6, 0))

        theme.section(parent, "번역 묶음").pack(anchor="w", padx=16, pady=(18, 4))
        self.pack_var = tk.StringVar()
        ttk.Label(parent, textvariable=self.pack_var, style="Card.TLabel",
                  wraplength=400, justify="left").pack(anchor="w", padx=16)
        self.pack_btn = ttk.Button(parent, text="번역 묶음 받기", command=self._fetch_pack)
        self.pack_btn.pack(anchor="w", padx=16, pady=(8, 0))
        ttk.Label(parent, text="공식 번역이 아직 없는 글을 배포자가 미리 번역해 둔 것입니다. 받아 두면 번역기 키 없이도 "
                               "대부분의 화면이 바로 번역됩니다.",
                  style="Muted.TLabel", wraplength=400).pack(anchor="w", padx=16, pady=(6, 0))
        self._show_pack()
        if self.cfg.get("pack_url"):
            # 창이 뜬 뒤에 새 판이 있는지만 본다 (받기는 누를 때)
            self.root.after(3000, lambda: threading.Thread(target=self._check_pack, daemon=True).start())

        theme.section(parent, "직접 고치기").pack(anchor="w", padx=16, pady=(18, 6))
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(anchor="w", padx=16)
        ttk.Button(row, text="번역캐시 열기", command=lambda: self._open("번역캐시.md")).pack(side="left")
        ttk.Button(row, text="용어집 열기", command=lambda: self._open("용어집.md")).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="문제 화면 폴더", command=self._open_debug).pack(side="left", padx=(8, 0))
        ttk.Label(parent, text="번역캐시의 번역문을 고쳐 저장하면 게임 중에도 2초 안에 바뀝니다. "
                               "용어집에 적은 이름은 공식 번역보다 먼저 쓰입니다.",
                  style="Muted.TLabel", wraplength=400).pack(anchor="w", padx=16, pady=(6, 0))

        theme.section(parent, "프로그램").pack(anchor="w", padx=16, pady=(18, 4))
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(anchor="w", padx=16, fill="x")
        self.app_var = tk.StringVar(value=f"판 {version.VERSION}")
        ttk.Label(row, textvariable=self.app_var, style="Card.TLabel").pack(side="left")
        # 새 판이 있을 때만 보인다
        self.app_btn = ttk.Button(row, text="새 판 받기 ↗", style="Link.TButton")
        ttk.Button(row, text="오류 기록", style="Link.TButton", command=self._open_errors).pack(side="right")
        repo = self.cfg.get("app_repo") or version.REPO
        if repo:
            self.root.after(4000, lambda: threading.Thread(target=self._check_app, args=(repo,),
                                                           daemon=True).start())

        theme.section(parent, "출처").pack(anchor="w", padx=16, pady=(18, 4))
        ttk.Label(parent, text="공식 번역: GitHub ArknightsAssets/ArknightsGamedata\n"
                               "번역 묶음: 배포자가 기계 번역해 올린 것 (오역은 그 저장소로 알려 주세요)",
                  style="Muted.TLabel", justify="left", wraplength=400).pack(anchor="w", padx=16, pady=(0, 14))

    def _check_app(self, repo: str) -> None:
        found = version.latest(repo)
        if found:
            self.root.after(0, self._show_app_update, *found)

    def _show_app_update(self, tag: str, url: str) -> None:
        self.app_var.set(f"판 {version.VERSION}  →  새 판 {tag} 이 나왔습니다")
        self.app_btn.configure(command=lambda: webbrowser.open(url))
        self.app_btn.pack(side="left", padx=(8, 0))
        self.msg_var.set(f"프로그램 새 판 {tag} 이 나왔습니다. 데이터 탭의 '새 판 받기' 에서 받을 수 있습니다.")

    def _on_style(self, _event=None) -> None:
        label = self.style_var.get()
        self.cfg["box_style"] = next(k for k, v in STYLE_LABELS.items() if v == label)
        config.save(self.cfg)
        self.overlay.restyle()
        self.engine.reset()

    def _on_font(self, _event=None) -> None:
        self.cfg["font_family"] = self.font_var.get()
        config.save(self.cfg)
        self._show_font_preview()
        self.overlay.restyle()
        self.engine.reset()

    def _show_font_preview(self) -> None:
        self._preview_font = tkfont.Font(root=self.root, family=self.font_var.get(), size=13)
        self.font_preview.configure(font=self._preview_font)

    def _on_scale(self, _value=None) -> None:
        self.cfg["font_scale"] = round(float(self.scale_var.get()), 2)
        # 끌고 있는 동안 주기마다 다시 그리지 않고, 멈췄을 때 한 번만 적용한다
        if self._scale_job:
            self.root.after_cancel(self._scale_job)
        self._scale_job = self.root.after(250, self._apply_scale)

    def _apply_scale(self) -> None:
        self._scale_job = None
        config.save(self.cfg)
        self.overlay.restyle()
        self.engine.reset()

    def _on_engine(self, _event=None) -> None:
        label = self.engine_var.get()
        self.cfg["engine"] = next(k for k, v in ENGINE_LABELS.items() if v == label)
        config.save(self.cfg)
        self.resolver.set_engine(self.cfg)
        self._build_key_fields()

    def _build_key_fields(self) -> None:
        for child in self.key_frame.winfo_children():
            child.destroy()
        self._key_entries.clear()
        engine = self.cfg["engine"]
        fields = {"deepl": (("DeepL 키", "deepl_key"),),
                  "papago": (("Client ID", "papago_id"), ("Client Secret", "papago_secret"))}.get(engine, ())
        for row, (text, name) in enumerate(fields):
            ttk.Label(self.key_frame, text=text, style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=4)
            var = tk.StringVar(value=self.cfg.get(name, ""))
            var.trace_add("write", lambda *_a, n=name, v=var: self._on_key(n, v))
            entry = ttk.Entry(self.key_frame, textvariable=var, show="•")
            entry.grid(row=row, column=1, sticky="ew", padx=(12, 0), pady=4)
            self._key_entries[name] = entry
        if engine == "google":
            self.key_status.set("키 없이 씁니다. 많이 쓰면 잠시 막힐 수 있습니다.")
        ttk.Label(self.key_frame, textvariable=self.key_status, style="Muted.TLabel",
                  wraplength=380).grid(row=len(fields), column=0, columnspan=2, sticky="w", pady=(2, 0))
        row = len(fields) + 1
        chain = self.resolver.engine
        if chain.waiting:
            # 한도를 다 써서 멈춘 상태. 알림창을 닫았어도 여기서 고를 수 있다
            self.key_status.set("한도를 다 써서 기계 번역을 멈췄습니다. 새 키를 넣거나 구글 번역으로 계속하세요.")
            act = ttk.Frame(self.key_frame, style="Card.TFrame")
            act.grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 2))
            ttk.Button(act, text="구글 번역으로 계속", command=self._quota_continue).pack(side="left")
            ttk.Button(act, text="알림 다시 보기", style="Link.TButton",
                       command=lambda: self._show_quota_dialog(self.resolver.engine)).pack(side="left", padx=(10, 0))
            row += 1
        links = KEY_LINKS.get(engine)
        if links:
            self._link_row(self.key_frame, row, links)
        if fields and not chain.waiting:
            self._check_key()

    def _link_row(self, parent, row: int, links) -> None:
        """키를 받는 사이트로 가는 링크. 기본 브라우저로 연다."""
        frame = ttk.Frame(parent, style="Card.TFrame")
        frame.grid(row=row, column=0, columnspan=2, sticky="w")
        for text, url in links:
            ttk.Button(frame, text=f"{text} ↗", style="Link.TButton",
                       command=lambda u=url: webbrowser.open(u)).pack(side="left", padx=(0, 12))

    def _on_key(self, name: str, var: tk.StringVar) -> None:
        self.cfg[name] = var.get().strip()
        # 붙여 넣는 동안 매 글자마다 확인하지 않고, 입력이 멈추면 한 번 저장·확인한다
        if self._key_job:
            self.root.after_cancel(self._key_job)
        self._key_job = self.root.after(700, self._apply_key)

    def _apply_key(self) -> None:
        self._key_job = None
        config.save(self.cfg)
        self.resolver.set_engine(self.cfg)
        self._check_key()

    def _check_key(self) -> None:
        engine = self.cfg["engine"]
        if engine == "deepl" and not self.cfg.get("deepl_key"):
            self.key_status.set("키를 넣기 전까지는 구글 번역을 씁니다.")
        elif engine == "papago" and not (self.cfg.get("papago_id") and self.cfg.get("papago_secret")):
            self.key_status.set("키를 넣기 전까지는 구글 번역을 씁니다.")
        elif engine == "deepl":
            self.key_status.set("키 확인 중…")
            threading.Thread(target=self._check_deepl, args=(self.cfg["deepl_key"],), daemon=True).start()
        else:
            self.key_status.set("Papago 는 첫 번역 때 키를 확인합니다. 막히면 구글 번역으로 넘어갑니다.")

    def _check_deepl(self, key: str) -> None:
        """남은 한도를 물어본다. 키가 틀렸으면 여기서 바로 알 수 있다."""
        stop = ""
        try:
            used, limit = mt.DeepL(key).usage()
            left = max(0, limit - used)
            text = f"키 확인됨 · 남은 한도 {left / 10000:.1f}만 자 / {limit / 10000:.0f}만 자"
            if left == 0:
                text = f"한도를 다 썼습니다 ({used:,} / {limit:,}자)"
                stop = "quota"
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            stop = mt.STOP_REASONS.get(code, "") if code in (401, 403) else ""
            text = ("키가 맞지 않습니다. 구글 번역을 씁니다." if stop
                    else f"DeepL 이 응답하지 않습니다 ({code}). 막히면 구글 번역으로 넘어갑니다.")
        except requests.RequestException:
            text = "DeepL 에 연결하지 못했습니다. 막히면 구글 번역으로 넘어갑니다."
        if key != self.cfg.get("deepl_key"):
            return    # 확인하는 사이에 키를 또 바꿨으면 버린다
        chain = self.resolver.engine
        if stop == "quota":
            # 다 쓴 키다. 첫 문장에서 실패할 때까지 기다리지 않고 바로 묻는다
            chain.quota_hit((used, limit))
            return
        if stop:
            # 틀린 키다. 첫 문장에서 한 번 더 실패할 때까지 기다리지 않고 바로 구글로 넘긴다
            chain.stopped = stop
        self.root.after(0, self.key_status.set, text)

    def _open(self, name: str) -> None:
        """번역캐시·용어집·오류 기록을 메모장으로 연다. .md 는 PC 마다 연결된 프로그램이 달라(7-Zip 으로 열려
        '압축파일로 열 수 없음' 이 뜬 PC 가 있었다) 연결이 없으면 os.startfile 이 오류를 낸다. 메모장은 늘 있다."""
        path = os.path.join(config.DATA_DIR, name)
        if not os.path.exists(path):
            self.msg_var.set(f"{name} 이 아직 없습니다.")
            return
        notepad = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "notepad.exe")
        try:
            subprocess.Popen([notepad if os.path.exists(notepad) else "notepad.exe", path])
        except OSError as exc:
            self.msg_var.set(f"{name} 을 열지 못했습니다: {exc}")

    def _open_errors(self) -> None:
        if os.path.exists(os.path.join(config.DATA_DIR, errlog.FILE)):
            self._open(errlog.FILE)
        else:
            self.msg_var.set("아직 오류 기록이 없습니다.")

    # -------------------------------------------------------------- 번역 묶음
    def _show_pack(self, note: str = "") -> None:
        store = self.resolver.pack
        if store.version:
            text = f"받은 번역 묶음: {store.version} · {len(store):,}줄"
        else:
            text = "아직 받은 번역 묶음이 없습니다."
        if not self.cfg.get("pack_url"):
            text += "\n배포처가 정해지지 않아 받을 수 없습니다."
            self.pack_btn.configure(state="disabled")
        self.pack_var.set(text + (f"\n{note}" if note else ""))

    def _check_pack(self) -> None:
        """배포처에 더 새 판이 있는지 본다. 내려받지는 않는다."""
        try:
            remote = pack_mod.remote_version(self.cfg["pack_url"])
        except (OSError, ValueError):
            return
        if remote and remote != self.resolver.pack.version:
            self.root.after(0, self._show_pack, f"새 번역 묶음이 있습니다 ({remote}). 받기를 누르세요.")

    def _fetch_pack(self) -> None:
        self.pack_btn.configure(state="disabled")
        self.pack_var.set("번역 묶음 받는 중…")
        threading.Thread(target=self._fetch_pack_worker, daemon=True).start()

    def _fetch_pack_worker(self) -> None:
        try:
            version, changed = pack_mod.fetch(self.cfg["pack_url"], os.path.join(config.DATA_DIR, "pack"))
            note = f"{len(changed)}개 파일을 새로 받았습니다." if changed else "이미 최신입니다."
        except (OSError, ValueError) as exc:
            note = f"받지 못했습니다: {exc}"
        else:
            self.resolver.reload_pack()
            self.engine.reset()     # 떠 있던 번역문도 묶음 번역으로 다시 그린다
        self.root.after(0, self._finish_pack, note)

    def _finish_pack(self, note: str) -> None:
        self.pack_btn.configure(state="normal")
        self._show_pack(note)

    def update_data(self) -> None:
        base = dbfetch.base_url(self.cfg)
        if base:
            # 배포자가 만든 DB 를 받는다 (Git·10분 빌드 없음)
            self.data_var.set("올라간 공식 데이터를 확인하는 중…")
            threading.Thread(target=self._check_db, args=(base, True), daemon=True).start()
            return
        if not messagebox.askyesno(
            "공식 데이터 갱신",
            "한국 서버에 새로 들어온 공식 번역을 받아 번역 DB 를 다시 만듭니다.\n"
            "약 500MB 를 내려받고 10분쯤 걸립니다. 끝나면 임시 파일은 지워집니다.\n\n"
            "진행할까요?", parent=self.root):
            return
        run_updater()

    # ---------------------------------------------------------------- 게임 연동
    def _on_ocr(self, _event=None) -> None:
        choice = {v: k for k, v in OCR_LABELS.items()}[self.ocr_var.get()]
        self.cfg["ocr_engine"] = choice
        config.save(self.cfg)
        self.engine.force.set()     # 바로 새 인식기로 다시 읽는다

    def _on_recruit(self) -> None:
        self.cfg["recruit_hint"] = bool(self.recruit_var.get())
        config.save(self.cfg)
        self.engine.reset()     # 떠 있는 테두리·목록을 지우거나 다시 그린다

    def _on_battle(self) -> None:
        self.cfg["battle_pause"] = bool(self.battle_var.get())
        config.save(self.cfg)
        if not self.cfg["battle_pause"] and self.engine.battle:
            self.engine.battle = False
            self.engine.force.set()

    # ------------------------------------------------------------ 한도 소진 알림
    def _show_quota_dialog(self, chain) -> None:
        """DeepL 한도를 다 썼을 때. 구글 번역으로 바로 넘기지 않고 사용자에게 묻는다."""
        if self._quota_win is not None and self._quota_win.winfo_exists():
            self._quota_win.lift()
            return
        used, limit = chain.usage or (0, 0)
        name = chain.primary.label if chain.primary else "번역기"
        if limit >= 1_000_000:
            body = (f"지금 키는 {name} API Developer 요금제(총 {limit // 10000:,}만 자)라 다 쓰면 초기화되지 "
                    "않습니다.\n새 키를 발급받아 넣으면 계속 DeepL 로 번역합니다.")
        elif limit:
            body = (f"이번 기간의 무료 한도({limit // 10000:,}만 자)를 다 썼습니다. 다음 기간에 다시 찹니다.\n"
                    "그 전에 계속 쓰려면 새 키를 넣거나 구글 번역을 고르세요.")
        else:
            body = f"{name} 한도를 다 썼습니다. 새 키를 넣거나 구글 번역을 고르세요."

        win = tk.Toplevel(self.root)
        self._quota_win = win
        win.title(f"{name} 한도 소진")
        win.configure(bg=theme.BG)
        win.resizable(False, False)
        win.attributes("-topmost", True)   # 전체 화면 게임 위에도 보이게
        box = theme.card(win)
        box.pack(fill="both", expand=True, padx=12, pady=12)
        theme.card_title(box, f"{name} 한도를 다 썼습니다").pack(anchor="w", padx=20, pady=(18, 8))
        ttk.Label(box, text=body, style="Card.TLabel", wraplength=440, justify="left").pack(anchor="w", padx=20)
        if limit:
            ttk.Label(box, text=f"사용 {used:,} / {limit:,}자", style="Muted.TLabel").pack(anchor="w", padx=20, pady=(8, 0))
        ttk.Label(box, text="고르기 전까지 기계 번역을 멈춥니다. 공식 번역·번역 묶음과 이미 번역해 둔 문장은 그대로 보입니다.",
                  style="Muted.TLabel", wraplength=440).pack(anchor="w", padx=20, pady=(4, 0))
        row = ttk.Frame(box, style="Card.TFrame")
        row.pack(fill="x", padx=20, pady=(18, 18))
        ttk.Button(row, text="새 키 발급받기 ↗", style="Accent.TButton",
                   command=self._quota_new_key).pack(side="left")
        ttk.Button(row, text="구글 번역으로 계속", command=self._quota_continue).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="나중에", command=win.destroy).pack(side="right")
        theme.dark_title_bar(win)
        self._center(win)
        win.focus_force()
        self._build_key_fields()

    def _center(self, win) -> None:
        """게임 화면 한가운데(없으면 패널 옆)에 놓는다."""
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        region = self.engine.current_region()
        if region:
            x, y, rw, rh = region
        else:
            x, y, rw, rh = (self.root.winfo_rootx(), self.root.winfo_rooty(),
                            self.root.winfo_width(), self.root.winfo_height())
        win.geometry(f"+{x + max(0, (rw - w) // 2)}+{y + max(0, (rh - h) // 3)}")

    def _close_quota(self) -> None:
        if self._quota_win is not None and self._quota_win.winfo_exists():
            self._quota_win.destroy()
        self._quota_win = None

    def _quota_new_key(self) -> None:
        self._close_quota()
        webbrowser.open(KEY_LINKS["deepl"][1][1])
        self.tabs.select(self.settings_tab)
        self._build_key_fields()
        entry = self._key_entries.get("deepl_key")
        if entry is not None:
            entry.focus_set()
            entry.select_range(0, "end")
        self.key_status.set("새 키를 붙여 넣으면 바로 DeepL 로 다시 번역합니다.")
        self.root.deiconify()
        self.root.lift()

    def _quota_continue(self) -> None:
        self._close_quota()
        self.resolver.engine.continue_with_fallback()
        self._build_key_fields()
        self.key_status.set("한도를 다 써서 구글 번역을 씁니다. 새 키를 넣으면 DeepL 로 돌아갑니다.")

    def _pump_alerts(self) -> None:
        try:
            while True:
                kind, info = self.alerts.get_nowait()
                if kind == "quota" and info is self.resolver.engine:
                    self._show_quota_dialog(info)
        except queue.Empty:
            pass

    # ------------------------------------------------------------- 최근 번역
    def _on_select_recent(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel or sel[0] not in self._recent_src:
            return
        _via, _src, _ko, zh = self._recent_src[sel[0]]
        self.detail_var.set(f"원문: {zh}")
        self.report_btn.configure(state="normal")
        self.fix_btn.configure(state="normal")

    def _report_recent(self) -> None:
        self._report(self._picked_recent(), self.detail_var)

    def _report(self, entry, detail: tk.StringVar) -> None:
        """고른 줄의 오역 제보. 번역 저장소 이슈 쓰기 화면을 브라우저로 연다 (보내는 건 사용자)."""
        if not entry:
            return
        via, src, ko, zh = entry
        meta = self.resolver.db.meta
        url = pack_mod.report_url(self.cfg.get("pack_url", ""), {
            "zh": zh, "ko": ko, "source": VIA_LABELS.get(via, via),
            "version": f"번역 묶음 {self.resolver.pack.version or '없음'} · 공식 데이터 {meta.get('built_at', '?')}",
            "ocr": src if tn.key(src) != tn.key(zh) else "",
        })
        if url is None:
            detail.set("제보받을 번역 저장소가 아직 정해지지 않았습니다. 이상한 번역은 data\\용어집.md 에 적으면 "
                                "이 PC 에서는 바로 바뀝니다.")
            return
        webbrowser.open(url)
        detail.set("브라우저에서 제보 화면을 열었습니다. 고칠 번역을 적고 'Submit new issue' 를 누르면 보내집니다 "
                            "(GitHub 로그인 필요).")

    def _open_debug(self) -> None:
        folder = os.path.join(config.ROOT, "debug")
        os.makedirs(folder, exist_ok=True)
        os.startfile(folder)

    # ------------------------------------------------------------- 문제 화면
    def save_problem(self) -> None:
        """지금 화면과 인식·번역 내역을 debug 폴더에 남긴다. 번역이 이상할 때 원인을 짚는 용도."""
        region = self.engine.current_region()
        if not region:
            self.msg_var.set("게임 화면을 찾지 못해 저장하지 않았습니다.")
            return
        was_paused = self.engine.paused.is_set()
        self.engine.paused.set()
        self.engine.idle.wait(2.0)    # 읽는 도중이면 끝날 때까지 기다린다
        try:
            hwnd = engine_mod.find_game()
            # 다른 창이 덮고 있으면 그 창이 찍힌다. 그런 화면은 남기지 않는다
            if not hwnd or not engine_mod.game_visible(hwnd, region, self._own_windows()):
                self.msg_var.set("게임이 다른 창에 가려져 있어 저장하지 않았습니다.")
                return
            snap = self.engine.snapshot()
            original = capture.grab(region)
            translated = None
            if self.overlay.win.winfo_ismapped():
                # 오버레이는 캡처에서 빠져 있다. 이 순간만 풀어 번역이 얹힌 화면을 찍는다
                winutil.exclude_from_capture(self.overlay.hwnd, False)
                try:
                    self.root.update()
                    time.sleep(0.15)
                    translated = capture.grab(region)
                finally:
                    winutil.exclude_from_capture(self.overlay.hwnd, True)
            folder = debug_report.write(config.ROOT, snap, original, translated,
                                        self.resolver, self.cfg, self.engine.status.recent)
            self.msg_var.set(f"문제 화면을 저장했습니다: {os.path.relpath(folder, config.ROOT)}")
        finally:
            if not was_paused:
                self.engine.paused.clear()

    def toggle(self) -> None:
        if self.engine.paused.is_set():
            self.engine.paused.clear()
            self.overlay.set_hidden(False)
            self.toggle_btn.configure(text="일시정지")
            self.engine.force.set()
        else:
            self.engine.paused.set()
            self.overlay.set_hidden(True)
            self.toggle_btn.configure(text="다시 시작")

    def _pump(self) -> None:
        """작업 스레드가 넘긴 표시 내용을 UI 스레드에서 그린다. 가장 최근 것만 쓴다."""
        latest = "none"
        try:
            while True:
                latest = self.updates.get_nowait()
        except queue.Empty:
            pass
        if latest is None:
            self.overlay.set_hidden(True)
        elif latest != "none":
            region = self.engine.current_region()
            if region:
                self.overlay.show(region, latest)
            if not self.engine.paused.is_set() and not self._peek_vk:
                self.overlay.set_hidden(False)
        self.root.after(30, self._pump)

    def _peek_hit(self) -> None:
        """(단축키 스레드) 원문 보기 단축키가 들어왔다. 누르고 있으면 키 반복마다 또 들어온다."""
        now = time.monotonic()
        self._peek_last = now
        if self._peek_vk:
            return
        parsed = hotkeys.parse(self.cfg["hotkeys"].get("peek", ""))
        if parsed:
            # 방금 눌렸으니 키는 눌려 있어야 한다. 아니라고 나오면 앞 창(게임)이 높은 권한이라 못 읽는 것
            down = hotkeys.key_down(parsed[1])
            self.root.after(0, lambda: self._peek_start(parsed[1], now, down))

    def _peek_start(self, vk: int, since: float, readable: bool) -> None:
        """원문 보기: 단축키를 누르고 있는 동안 번역문을 숨긴다. RegisterHotKey 는 눌렀을 때만 알려 주므로
        뗐는지는 키 상태를 짧게 물어 본다. 게임이 관리자 권한으로 돌면 윈도가 키 상태를 가려서, 그때는
        누르고 있는 동안 계속 들어오는 키 반복이 끊기는 것으로 뗀 때를 안다."""
        if self._peek_vk or self.engine.paused.is_set():
            return
        self._peek_vk = vk
        self._peek_since = since
        self._peek_keystate = readable
        self.overlay.set_hidden(True)
        self.root.after(PEEK_POLL_MS, self._peek_poll)

    def _peek_held(self) -> bool:
        if self._peek_keystate:
            return hotkeys.key_down(self._peek_vk)
        first, every = hotkeys.repeat_timing()
        repeating = self._peek_last > self._peek_since
        wait = max(every * 3, PEEK_REPEAT_MIN) if repeating else first + PEEK_MARGIN
        return time.monotonic() - self._peek_last < wait

    def _peek_poll(self) -> None:
        if self._peek_held():
            self.root.after(PEEK_POLL_MS, self._peek_poll)
            return
        self._peek_vk = 0
        if not self.engine.paused.is_set():
            self.overlay.set_hidden(False)

    def _refresh_panel(self) -> None:
        st = self.engine.status
        s = self.resolver.stats
        self.state_var.set(f" {st.state}")
        self.state_label.configure(image=self._dots[_state_kind(st.state, self.engine.paused.is_set())])
        self.stat_var.set(
            f"공식 {s['official']} · 묶음 {s['pack']} · 저장됨 {s['cache']} · 기계 번역 {s['mt']}회"
            f"   |   화면 글자 {st.shown}/{st.blocks} · {st.ocr_name or 'OCR'} {st.ocr_ms}ms"
        )
        self.ocr_state.set(f"지금: {st.ocr_name or '-'}" + (f"  ·  {st.ocr_note}" if st.ocr_note else ""))
        self._pump_alerts()
        try:
            while True:
                self.msg_var.set(self.log_lines.get_nowait())
        except queue.Empty:
            pass
        recent = st.recent
        if len(recent) != self._last_recent:
            self._last_recent = len(recent)
            self.tree.delete(*self.tree.get_children())
            self._recent_src.clear()
            self.report_btn.configure(state="disabled")
            self.fix_btn.configure(state="disabled")
            for line in reversed(recent[-30:]):
                via, _src, ko = line[:3]
                iid = self.tree.insert("", "end", text=f" {VIA_LABELS.get(via, via)}",
                                       image=self._source_dots.get(via, ""), values=(ko,))
                self._recent_src[iid] = (*line[:3], line[3] if len(line) > 3 else line[1])
        self._refresh_story()
        self.root.after(500, self._refresh_panel)

    def _db_text(self) -> str:
        meta = self.resolver.db.meta
        return f"만든 날 {meta.get('built_at', '?')}\n공식 번역 {int(meta.get('texts_with_ko', 0)):,}문장"

    def _check_db(self, base: str, ask: bool) -> None:
        info = dbfetch.remote_info(base)
        self.root.after(0, self._db_checked, base, info, ask)

    def _db_checked(self, base: str, info, ask: bool) -> None:
        """ask: 사용자가 '공식 데이터 갱신' 을 눌렀다 (받을지 묻는다). 아니면 켤 때 확인 (알리기만 한다)."""
        text = self._db_text()
        if info is None:
            self.data_var.set(text + ("\n올라간 공식 데이터를 확인하지 못했습니다 (인터넷 연결)." if ask else ""))
            return
        if not dbfetch.is_newer(info, self.resolver.db.meta):
            self.data_var.set(text + ("\n이미 최신입니다." if ask else ""))
            return
        note = f"새 공식 데이터가 있습니다 (만든 날 {info.get('built_at', '?')}, 약 {dbfetch.size_mb(info)}MB)."
        if not ask:
            self.data_var.set(f"{text}\n{note} '공식 데이터 갱신' 을 누르면 받습니다.")
            return
        self.data_var.set(f"{text}\n{note}")
        if messagebox.askyesno("공식 데이터 갱신", f"{note}\n지금 받을까요? 다 받으면 번역기를 다시 켤 때 바뀝니다.",
                               parent=self.root):
            fetch_db_window(self.root, base, info, self._db_fetched)

    def _db_fetched(self, path, error) -> None:
        if error:
            self.msg_var.set(f"공식 데이터를 받지 못했습니다: {error}")
        else:
            self.data_var.set(self._db_text() + "\n새 공식 데이터를 받았습니다. 번역기를 다시 켜면 바뀝니다.")
            self.msg_var.set("새 공식 데이터를 받았습니다. 번역기를 다시 켜면 바뀝니다.")

    def _check_revision(self) -> None:
        meta = self.resolver.db.meta
        text = self._db_text()
        self.root.after(0, self.data_var.set, text)
        base = dbfetch.base_url(self.cfg)
        if base:
            # 배포자가 올린 DB 판과 견준다. 게임 데이터 저장소는 보지 않는다 (사용자가 직접 만들 일이 없다)
            self._check_db(base, False)
            return
        try:
            resp = requests.get(COMMITS_URL, headers={"Accept": "application/vnd.github+json"}, timeout=10)
            resp.raise_for_status()
            servers = servers_changed(resp.json(), meta.get("revision", ""))
        except (requests.RequestException, ValueError, KeyError, TypeError):
            return
        if servers:
            names = "·".join(SERVER_NAMES[s] for s in sorted(servers))
            self.root.after(0, self.data_var.set, f"{text}\n{names} 서버에 새 데이터가 있습니다. 갱신하면 반영됩니다.")

    def quit(self) -> None:
        config.save(self.cfg)
        self.engine.stop()
        self.hotkeys.stop()
        self.resolver.stop()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


# 한글이 들어 있는 글꼴 이름. Windows 는 한국어 글꼴을 한국어 이름('맑은 고딕')으로 알려 준다
_KOREAN_FONT = re.compile(r"[가-힣]|KR\b|Korean|Nanum|Hangul|Pretendard|Spoqa|Gmarket|Malgun|Gulim|Dotum|Batang|"
                          r"Gungsuh|Gothic A1", re.I)


def _korean_fonts(root, current: str) -> list[str]:
    """설치된(그리고 같이 배포한) 글꼴 가운데 한글 글꼴. 지금 쓰는 글꼴과 기본 글꼴(Pretendard, 맑은 고딕)을 맨 앞에 둔다."""
    names = sorted({f for f in tkfont.families(root) if _KOREAN_FONT.search(f) and not f.startswith("@")})
    front = [f for f in (current, config.FONT_FAMILY, "맑은 고딕") if f in names] or [current]
    return list(dict.fromkeys(front + names))


def _combo_label(combo: str) -> str:
    """'ctrl+alt+h' -> 'Ctrl+Alt+H'."""
    return "+".join(p.capitalize() if len(p) > 1 else p.upper() for p in combo.split("+"))


def _hotkey_hint(keys: dict) -> str:
    """맨 아래 단축키 안내. 앞부분이 모두 같으면(Ctrl+Alt) 한 번만 적어 짧게 한다."""
    items = (("toggle", "켜기/끄기"), ("translate", "지금 번역"), ("peek", "원문(누르는 동안)"),
             ("capture", "문제 저장"), ("quit", "종료"))
    items = tuple(item for item in items if keys.get(item[0]))
    labels = [_combo_label(keys[k]) for k, _ in items]
    heads = {label.rsplit("+", 1)[0] for label in labels if "+" in label}
    if len(heads) == 1 and all("+" in label for label in labels):
        head = heads.pop()
        return f"단축키 {head} +  " + "   ".join(f"{l.rsplit('+', 1)[1]} {t}" for l, (_, t) in zip(labels, items))
    return "   ".join(f"{l} {t}" for l, (_, t) in zip(labels, items))


def _state_kind(state: str, paused: bool) -> str:
    """상태 글자에 맞는 점 색."""
    if state.startswith("오류"):
        return "error"
    if paused or state.startswith("전투") or state.startswith("일시정지"):
        return "rest"
    if state.startswith("번역"):
        return "run"
    return "idle"


ERROR_ALREADY_EXISTS = 183
_instance_mutex = None    # 프로세스가 끝날 때까지 쥐고 있어야 한다


def acquire_single_instance() -> bool:
    """두 개가 뜨면 오버레이가 겹치고 번역 캐시를 동시에 쓴다."""
    global _instance_mutex
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _instance_mutex = kernel32.CreateMutexW(None, False, "ArknightsTranslatorOverlay")
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def main() -> None:
    if not acquire_single_instance():
        sys.exit(0)
    errlog.setup(config.DATA_DIR)
    winutil.enable_dpi_awareness()
    winutil.load_private_fonts(config.FONT_DIR)     # 같이 배포하는 Pretendard. Tk 를 만들기 전에

    pending = os.path.join(config.DATA_DIR, "official.sqlite.new")
    target = os.path.join(config.DATA_DIR, "official.sqlite")
    if os.path.exists(pending):
        try:
            os.replace(pending, target)
        except PermissionError:
            # 다른 프로그램(묶음 도구 등)이 DB 를 열고 있으면 바꿀 수 없다. 이번에는 예전 DB 로 켜고 다음에 바꾼다
            pass
    if not os.path.exists(target):
        root = tk.Tk()
        root.withdraw()
        if first_db(root, target):
            root.destroy()
            App().run()
            return
        if dbfetch.base_url(config.load()):
            root.destroy()
            return
        if messagebox.askyesno(
                "데이터 없음",
                "공식 번역 데이터가 아직 없습니다. 지금 받을까요?\n\n"
                "GitHub 의 게임 데이터(약 500MB)를 받아 번역 DB 를 만들고, 10분쯤 걸립니다. "
                "Git for Windows 가 설치되어 있어야 합니다.\n다 되면 이 프로그램을 다시 켜 주세요.", parent=root):
            run_updater()
        return
    App().run()


def fetch_db_window(master, base: str, info: dict, on_done) -> None:
    """공식 DB 받기 창 (진행 막대). 다 되면 on_done(.new 경로 또는 None, 오류 글 또는 None)."""
    win = tk.Toplevel(master)
    win.title("공식 번역 데이터 받기")
    win.configure(bg=theme.BG)
    win.resizable(False, False)
    win.attributes("-topmost", True)
    box = theme.card(win)
    box.pack(fill="both", expand=True, padx=12, pady=12)
    theme.card_title(box, "공식 번역 데이터 받기").pack(anchor="w", padx=18, pady=(16, 8))
    var = tk.StringVar(value="연결하는 중…")
    ttk.Label(box, textvariable=var, style="Card.TLabel").pack(anchor="w", padx=18)
    bar = ttk.Progressbar(box, length=380, maximum=1000)
    bar.pack(padx=18, pady=(8, 6))
    state = {"progress": (0, 0, ""), "path": None, "error": None, "stop": False}

    def work():
        try:
            state["path"] = dbfetch.download(base, info, config.DATA_DIR,
                                             progress=lambda d, t, stage: state.__setitem__("progress", (d, t, stage)),
                                             cancelled=lambda: state["stop"])
        except dbfetch.FetchError as exc:
            state["error"] = str(exc)
        except Exception as exc:     # 예상 못 한 오류도 창이 멈춘 채로 남지 않게
            errlog.write("공식 데이터 받기 오류", True)
            state["error"] = f"{type(exc).__name__}: {exc}"

    def stop():
        state["stop"] = True
        var.set("멈추는 중…")

    ttk.Button(box, text="멈추기", command=stop).pack(anchor="e", padx=18, pady=(4, 16))
    win.protocol("WM_DELETE_WINDOW", stop)
    theme.dark_title_bar(win)
    win.update_idletasks()
    w, h = win.winfo_reqwidth(), win.winfo_reqheight()
    win.geometry(f"+{(win.winfo_screenwidth() - w) // 2}+{(win.winfo_screenheight() - h) // 3}")
    threading.Thread(target=work, daemon=True, name="db-fetch").start()

    def poll():
        done, total, stage = state["progress"]
        if total:
            bar["value"] = 1000 * done / total
            var.set(f"{stage}  {done / 1e6:,.0f} / {total / 1e6:,.0f}MB")
        if state["path"] or state["error"]:
            win.destroy()
            on_done(state["path"], state["error"])
            return
        win.after(100, poll)

    poll()


def first_db(root, target: str) -> bool:
    """처음 켤 때 공식 DB 가 없으면 배포자가 올린 것을 받는다. 받아서 자리에 놓았으면 True."""
    theme.apply(root)
    base = dbfetch.base_url(config.load())
    if not base:
        return False
    info = dbfetch.remote_info(base)
    if info is None:
        messagebox.showerror("공식 번역 데이터", "공식 번역 데이터를 받을 곳에 연결하지 못했습니다.\n"
                             "인터넷 연결을 확인하고 다시 켜 주세요.", parent=root)
        return False
    if not messagebox.askyesno("공식 번역 데이터 받기",
                               f"처음 한 번 공식 번역 데이터(약 {dbfetch.size_mb(info)}MB)를 받습니다. 1~2분쯤 걸립니다.\n"
                               "받을까요?", parent=root):
        return False
    result = {}

    def done(path, error):
        result.update(path=path, error=error)
        root.quit()

    fetch_db_window(root, base, info, done)
    root.mainloop()
    if result.get("path"):
        os.replace(result["path"], target)
        return True
    if result.get("error") and result["error"] != "멈춤":
        messagebox.showerror("공식 번역 데이터", f"받지 못했습니다: {result['error']}\n다시 켜면 처음부터 다시 받습니다.",
                             parent=root)
    return False


def run_updater() -> None:
    """공식 데이터 갱신을 새 콘솔 창에서 돌린다. exe 판은 UpdateData.exe, 스크립트 판은 update_data.bat."""
    if config.FROZEN:
        subprocess.Popen([os.path.join(config.ROOT, config.UPDATER_EXE)], cwd=config.ROOT,
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
    else:
        bat = os.path.join(config.ROOT, "update_data.bat")
        subprocess.Popen(["cmd", "/c", "start", "", bat], cwd=config.ROOT)
