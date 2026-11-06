"""패널 색·글꼴·위젯 스타일. 명일방주 기본 화면처럼: 거의 검은 바탕, 숯색 판, 흰 글자, 얇은 선, 각진 모서리,
동작에는 게임의 키워드 파랑, 구역 표시에는 노랑, 제목 옆에는 작은 영문 이름표, 머리에는 사선 무늬."""
from __future__ import annotations

import ctypes
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

BG = "#0E0F11"          # 창 바탕
CARD = "#18191C"        # 판
FIELD = "#222428"       # 입력칸·목록
HOVER = "#2C2E33"
BORDER = "#2F3136"      # 얇은 선
LINE = "#55585E"        # 한 단계 밝은 선 (사선 무늬, 테두리 hover)
TEXT = "#F2F2F2"
MUTED = "#8B8F96"
CAPTION = "#62666D"     # 제목 옆 작은 영문 이름표
ACCENT = "#0098DC"      # 명일방주 키워드 파랑. 고른 탭 밑줄·초점·진행 막대
BUTTON = "#0080BE"      # 주 버튼. 흰 글자가 읽히게 ACCENT 보다 조금 어둡게
BUTTON_HOVER = "#0092D4"
BUTTON_PRESSED = "#006FA3"
ON_ACCENT = "#FFFFFF"
MARK = "#FFD13A"        # 명일방주 노랑. 구역 제목 앞 막대
SELECT = "#123A50"      # 목록에서 고른 줄
LINK = "#4FC1F2"
OK = "#4CC38A"
WARN = "#F2A43A"
ERROR = "#FF6B5B"
IDLE = "#5E636B"

# 번역이 어디서 왔는지 (최근 번역 목록의 점 색)
SOURCE_COLORS = {"official": OK, "pack": "#E8A65A", "user": LINK, "cache": "#9AA3AE"}

FAMILY = "Pretendard"   # 같이 배포한다 (config.FONT_DIR). 없으면 맑은 고딕
MONO = "Consolas"

# 구역 제목 옆에 붙는 영문 이름표. 영문판 게임의 이름을 따른다
CAPTIONS = {
    "화면": "DISPLAY", "기계 번역": "MACHINE TRANSLATION", "스토리 말투 맞춤": "STORY STYLE",
    "번역 데이터": "GAME DATA", "번역 묶음": "TRANSLATION PACK", "직접 고치기": "EDIT", "출처": "SOURCES",
    "진행": "PROGRESS", "이벤트": "EVENT", "통합전략": "INTEGRATED STRATEGIES", "오퍼레이터": "OPERATOR",
    "생명연산": "RECLAMATION ALGORITHM",
}

def _pick_family(root: tk.Misc) -> str:
    available = set(tkfont.families(root))
    for name in (FAMILY, "Malgun Gothic", "맑은 고딕", "Segoe UI", "Noto Sans KR"):
        if name in available:
            return name
    return "TkDefaultFont"


def _spaced(text: str) -> str:
    """영문 이름표는 게임처럼 자간을 벌린다 (Tk 는 자간 설정이 없어 얇은 공백을 끼운다)."""
    return " ".join(text.upper())


def dark_title_bar(window: tk.Misc) -> None:
    """Windows 10 20H1 이상에서 제목 표시줄을 어둡게 한다. 안 되면 그냥 둔다."""
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1)
        for attr in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE (20, 이전 빌드는 19)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except Exception:
        pass


def apply(root: tk.Misc) -> ttk.Style:
    """전체 위젯 스타일을 세팅하고 Style 객체를 돌려준다."""
    family = _pick_family(root)
    # 구역 제목·주 버튼은 SemiBold 가 있으면 그것으로 (굵게까지 걸면 GDI 가 한 번 더 두껍게 칠한다)
    semibold = f"{family} SemiBold" if f"{family} SemiBold" in set(tkfont.families(root)) else ""

    def strong(size: int) -> tuple:
        return (semibold, size) if semibold else (family, size, "bold")

    style = ttk.Style(root)
    try:
        style.theme_use("clam")  # 색을 마음대로 바꿀 수 있는 유일한 기본 테마
    except tk.TclError:
        pass

    base = (family, 10)
    small = (family, 9)
    section = strong(11)
    title = (family, 15, "bold")
    caption = (family, 8, "bold")

    # Tk 의 '이름 있는 폰트'도 같은 크기로 맞춘다. 안 맞추면 한글 조합 중인 글자만 크기가 달라진다
    for named in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont",
                  "TkCaptionFont", "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont"):
        try:
            tkfont.nametofont(named).configure(family=family, size=base[1])
        except tk.TclError:
            pass
    try:
        tkfont.nametofont("TkFixedFont").configure(family=MONO, size=small[1])
    except tk.TclError:
        pass

    root.configure(bg=BG)
    # '*Font' 를 옵션 데이터베이스에 넣으면 ttk 위젯에도 글꼴이 박혀 스타일 글꼴(제목·이름표 크기)을 덮는다.
    # Tk 기본 위젯은 위에서 맞춘 이름 있는 폰트를 쓰므로 넣지 않는다.
    # 콤보박스 펼침 목록은 ttk 스타일이 아니라 옵션 데이터베이스로만 바뀐다
    for key, value in (("background", FIELD), ("foreground", TEXT), ("selectBackground", BUTTON),
                       ("selectForeground", ON_ACCENT), ("font", base), ("borderWidth", 0),
                       ("highlightThickness", 1), ("highlightColor", LINE), ("highlightBackground", LINE)):
        root.option_add(f"*TCombobox*Listbox.{key}", value)

    style.configure(".", background=BG, foreground=TEXT, font=base, borderwidth=0,
                    focuscolor=CARD, troughcolor=FIELD, selectbackground=BUTTON,
                    selectforeground=ON_ACCENT, insertcolor=TEXT)

    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD)

    style.configure("TLabel", background=BG, foreground=TEXT, font=base)
    style.configure("Card.TLabel", background=CARD, foreground=TEXT, font=base)
    style.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=small)
    style.configure("MutedBg.TLabel", background=BG, foreground=MUTED, font=small)
    style.configure("Title.TLabel", background=CARD, foreground=TEXT, font=title)
    style.configure("Section.TLabel", background=CARD, foreground=TEXT, font=section)
    style.configure("Caption.TLabel", background=CARD, foreground=CAPTION, font=caption)
    style.configure("Error.TLabel", background=CARD, foreground=ERROR, font=small)

    # --- 버튼: 각진 직사각형 ---
    # clam 에서 테두리는 Button.border 가 그린다. lightcolor/darkcolor 를 배경과 같게 두어야
    # 입체 음영이 끼지 않는다.
    style.configure("TButton", background=FIELD, foreground=TEXT, font=base, padding=(12, 7),
                    relief="flat", borderwidth=1, bordercolor=BORDER, lightcolor=FIELD,
                    darkcolor=FIELD, anchor="center", focusthickness=0)
    style.map("TButton",
              background=[("pressed", BORDER), ("active", HOVER), ("disabled", CARD)],
              lightcolor=[("pressed", BORDER), ("active", HOVER)],
              darkcolor=[("pressed", BORDER), ("active", HOVER)],
              foreground=[("disabled", IDLE)],
              bordercolor=[("active", LINE), ("focus", BORDER)])

    style.configure("Accent.TButton", background=BUTTON, foreground=ON_ACCENT,
                    font=strong(10), padding=(14, 8), relief="flat", borderwidth=0,
                    focusthickness=0, lightcolor=BUTTON, darkcolor=BUTTON, bordercolor=BUTTON)
    style.map("Accent.TButton",
              background=[("disabled", HOVER), ("pressed", BUTTON_PRESSED), ("active", BUTTON_HOVER)],
              foreground=[("disabled", IDLE)],
              lightcolor=[("pressed", BUTTON_PRESSED), ("active", BUTTON_HOVER)],
              darkcolor=[("pressed", BUTTON_PRESSED), ("active", BUTTON_HOVER)],
              bordercolor=[("pressed", BUTTON_PRESSED), ("active", BUTTON_HOVER)])

    # 글자만 있는 보조 동작용 (판 위 / 창 바탕 위 두 가지)
    for name, bgc in (("Link.TButton", CARD), ("LinkBg.TButton", BG)):
        style.configure(name, background=bgc, foreground=LINK, font=small, padding=(0, 2),
                        borderwidth=0, relief="flat", focusthickness=0,
                        lightcolor=bgc, darkcolor=bgc, bordercolor=bgc)
        style.map(name, background=[("active", bgc), ("pressed", bgc)],
                  lightcolor=[("active", bgc)], darkcolor=[("active", bgc)],
                  foreground=[("active", "#9BDCF8")])

    scaling = 1.0
    try:
        scaling = max(1.0, float(root.tk.call("tk", "scaling")) / 1.3333)
    except Exception:
        pass

    # --- 체크박스 ---
    # clam 은 체크된 상태를 'X' 로 그린다. 켜짐이 아니라 취소처럼 읽히므로 직접 그린 그림으로 바꾼다
    style.configure("TCheckbutton", background=CARD, foreground=TEXT, font=base,
                    focuscolor=CARD, focusthickness=0, padding=(0, 3))
    style.map("TCheckbutton", background=[("active", CARD)], foreground=[("disabled", IDLE)])
    _install_check_indicator(root, style, "TCheckbutton", int(round(16 * scaling)))

    for name in ("TCombobox", "TSpinbox", "TEntry"):
        style.configure(name, fieldbackground=FIELD, background=FIELD, foreground=TEXT,
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                        arrowcolor=MUTED, insertcolor=TEXT, padding=(8, 5), borderwidth=1,
                        relief="flat", selectbackground=BUTTON, selectforeground=ON_ACCENT)
        style.map(name,
                  bordercolor=[("focus", ACCENT), ("hover", LINE)],
                  lightcolor=[("focus", ACCENT)], darkcolor=[("focus", ACCENT)],
                  fieldbackground=[("readonly", FIELD), ("disabled", CARD)],
                  foreground=[("readonly", TEXT), ("disabled", IDLE)],
                  selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", TEXT)],
                  arrowcolor=[("hover", TEXT)], background=[("active", HOVER)])

    # 손잡이는 게임 설정 화면처럼 흰 직사각형
    style.configure("Horizontal.TScale", background="#D9DADC", troughcolor=FIELD,
                    bordercolor=CARD, lightcolor="#D9DADC", darkcolor="#D9DADC", sliderthickness=14)
    style.map("Horizontal.TScale", background=[("active", TEXT)])
    style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=FIELD, bordercolor=CARD,
                    lightcolor=ACCENT, darkcolor=ACCENT, thickness=6)

    # --- 탭: 글자만 두고, 고른 탭은 흰 글자 + 아래 파란 막대 ---
    style.configure("TNotebook", background=BG, borderwidth=1, tabmargins=(0, 4, 0, 0),
                    bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD)
    style.configure("TNotebook.Tab", background=BG, foreground=MUTED, font=base,
                    padding=(16, 9), borderwidth=0)
    style.map("TNotebook.Tab",
              foreground=[("selected", TEXT), ("active", TEXT)],
              # clam 은 선택된 탭의 padding 을 바꾸고 expand 도 준다. 고를 때마다 탭이 흔들리지 않게 못박는다
              padding=[("selected", (16, 9)), ("!selected", (16, 9))],
              expand=[("selected", (0, 0, 0, 0)), ("!selected", (0, 0, 0, 0))])
    _install_tab_element(root, style, max(2, int(round(3 * scaling))))

    style.configure("TSeparator", background=BORDER)
    style.configure("Vertical.TScrollbar", background=FIELD, troughcolor=CARD, bordercolor=CARD,
                    arrowcolor=MUTED, lightcolor=FIELD, darkcolor=FIELD, borderwidth=0)
    style.map("Vertical.TScrollbar", background=[("active", HOVER)])
    style.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT,
                    borderwidth=0, rowheight=30, font=base)
    style.configure("Treeview.Heading", background=CARD, foreground=CAPTION, font=caption,
                    relief="flat", padding=(6, 6), borderwidth=0)
    style.map("Treeview", background=[("selected", SELECT)], foreground=[("selected", TEXT)])
    style.map("Treeview.Heading", background=[("active", CARD)])
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])   # 바깥 테두리 없애기
    return style


# 이미지가 가비지 컬렉션되면 위젯에서 사라지므로 모듈에 붙들어 둔다
_IMAGE_REFS: list = []
CHECK_GAP = 9  # 지시자와 글자 사이 간격 (이미지 안에 투명 여백으로 넣는다)


def _draw_check(px: int, fill: str, border: str, mark: str | None):
    """체크박스 지시자 한 장. 게임처럼 각진 네모. 4배로 그린 뒤 줄여서 가장자리를 부드럽게 만든다."""
    from PIL import Image, ImageDraw

    s = 4
    size = px * s
    img = Image.new("RGBA", (size + CHECK_GAP * s, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle([s // 2, s // 2, size - s // 2 - 1, size - s // 2 - 1], fill=fill, outline=border, width=max(2, s))
    if mark:
        d.line([(size * 0.25, size * 0.52), (size * 0.42, size * 0.70), (size * 0.76, size * 0.31)],
               fill=mark, width=max(3, int(size * 0.12)), joint="curve")
    return img.resize((px + CHECK_GAP, px), Image.LANCZOS)


def _install_check_indicator(root: tk.Misc, style: ttk.Style, style_name: str, px: int) -> None:
    try:
        from PIL import ImageTk
    except Exception:
        return
    element = f"ArkCheck{px}.indicator"
    off = ImageTk.PhotoImage(_draw_check(px, FIELD, LINE, None), master=root)
    on = ImageTk.PhotoImage(_draw_check(px, BUTTON, BUTTON, ON_ACCENT), master=root)
    hover = ImageTk.PhotoImage(_draw_check(px, HOVER, "#80848A", None), master=root)
    disabled = ImageTk.PhotoImage(_draw_check(px, CARD, BORDER, None), master=root)
    _IMAGE_REFS.extend([off, on, hover, disabled])
    try:
        style.element_create(element, "image", off, ("disabled", disabled), ("selected", on),
                             ("active", hover), border=0, sticky="")
    except tk.TclError:
        pass  # 같은 이름으로 이미 만들어져 있으면 그대로 쓴다
    style.layout(style_name, [
        ("Checkbutton.padding", {"sticky": "nswe", "children": [
            (element, {"side": "left", "sticky": ""}),
            ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                ("Checkbutton.label", {"sticky": "nswe"})]}),
        ]}),
    ])


def _install_tab_element(root: tk.Misc, style: ttk.Style, bar: int) -> None:
    """탭 바탕을 그림으로 바꾼다. 고른 탭만 판 색에 아래 파란 막대. 가로로만 늘어나게 아래쪽 막대는 테두리 안에 둔다."""
    try:
        from PIL import Image, ImageDraw, ImageTk
    except Exception:
        return
    h = bar + 8

    def tile(fill: str, line: str | None):
        img = Image.new("RGB", (8, h), fill)
        if line:
            ImageDraw.Draw(img).rectangle([0, h - bar, 7, h - 1], fill=line)
        return ImageTk.PhotoImage(img, master=root)

    normal, hover, selected = tile(BG, None), tile("#15161A", None), tile(CARD, ACCENT)
    _IMAGE_REFS.extend([normal, hover, selected])
    element = f"ArkTab{bar}.background"
    try:
        style.element_create(element, "image", normal, ("selected", selected), ("active", hover),
                             border=(2, 2, 2, bar + 1), sticky="nswe")
    except tk.TclError:
        pass
    style.layout("TNotebook.Tab", [
        (element, {"sticky": "nswe", "children": [
            ("Notebook.padding", {"side": "top", "sticky": "nswe", "children": [
                ("Notebook.label", {"side": "top", "sticky": ""})]})]})])


def dot(root: tk.Misc, color: str, px: int = 10):
    """색 네모 하나 (상태 표시, 번역 출처 표시용). 게임의 상태 표시처럼 각지게."""
    from PIL import Image, ImageDraw, ImageTk

    s = 4
    img = Image.new("RGBA", (px * s, px * s), (0, 0, 0, 0))
    pad = int(px * s * 0.18)
    ImageDraw.Draw(img).rectangle([pad, pad, px * s - pad - 1, px * s - pad - 1], fill=color)
    photo = ImageTk.PhotoImage(img.resize((px, px), Image.LANCZOS), master=root)
    _IMAGE_REFS.append(photo)
    return photo


def scrollable(parent: tk.Misc, bg: str = CARD) -> tuple[ttk.Frame, ttk.Frame]:
    """세로 스크롤이 되는 영역. (바깥 프레임, 내용을 넣을 안쪽 프레임)."""
    outer = ttk.Frame(parent, style="Card.TFrame" if bg == CARD else "TFrame")
    canvas = tk.Canvas(outer, bg=bg, highlightthickness=0, bd=0, takefocus=0)
    vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    inner = ttk.Frame(canvas, style="Card.TFrame" if bg == CARD else "TFrame")
    window = canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=vsb.set)
    canvas.pack(side="left", fill="both", expand=True)

    def _resize_region(_e=None):
        canvas.configure(scrollregion=canvas.bbox("all"))
        # 내용이 다 보이면 스크롤바를 숨긴다
        needs = canvas.bbox("all") and canvas.bbox("all")[3] > canvas.winfo_height()
        if needs and not vsb.winfo_ismapped():
            vsb.pack(side="right", fill="y")
        elif not needs and vsb.winfo_ismapped():
            vsb.pack_forget()

    def _resize_width(event):
        canvas.itemconfigure(window, width=event.width)
        _resize_region()

    inner.bind("<Configure>", _resize_region)
    canvas.bind("<Configure>", _resize_width)

    def _wheel(event):
        node = canvas.winfo_containing(event.x_root, event.y_root)
        while node is not None:
            if node is canvas:
                if canvas.bbox("all") and canvas.bbox("all")[3] > canvas.winfo_height():
                    canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
                return
            node = getattr(node, "master", None)

    canvas.bind_all("<MouseWheel>", _wheel, add="+")
    return outer, inner


def card(parent: tk.Misc, **kw) -> tk.Frame:
    """얇은 테두리를 두른 판."""
    return tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightcolor=BORDER,
                    highlightthickness=1, bd=0, **kw)


def stripes(parent: tk.Misc, height: int = 6, bg: str = CARD) -> tk.Canvas:
    """판 머리의 사선 무늬 띠. 폭이 바뀔 때마다 다시 그린다."""
    canvas = tk.Canvas(parent, height=height, width=1, bg=bg, highlightthickness=0, bd=0, takefocus=0)

    def draw(event):
        canvas.delete("all")
        for x in range(-height, event.width + height, 7):
            canvas.create_line(x, height, x + height, 0, fill=LINE, width=2)

    canvas.bind("<Configure>", draw)
    return canvas


def card_title(parent: tk.Misc, text: str, caption: str = "") -> tk.Frame:
    """판 제목: 왼쪽 파란 막대 + 굵은 제목 (+ 아래 영문 이름표)."""
    box = tk.Frame(parent, bg=CARD)
    tk.Frame(box, bg=ACCENT, width=4).pack(side="left", fill="y", padx=(0, 10))
    words = tk.Frame(box, bg=CARD)
    words.pack(side="left")
    ttk.Label(words, text=text, style="Title.TLabel").pack(anchor="w")
    if caption:
        ttk.Label(words, text=_spaced(caption), style="Caption.TLabel").pack(anchor="w", pady=(1, 0))
    return box


def section(parent: tk.Misc, text: str, caption: str | None = None) -> tk.Frame:
    """구역 제목: 노란 막대 + 제목 + 작은 영문 이름표 (CAPTIONS 에서 찾는다)."""
    if caption is None:     # '1  이벤트' 처럼 번호가 붙은 제목은 마지막 낱말로 찾는다
        caption = CAPTIONS.get(text) or (CAPTIONS.get(text.split()[-1], "") if text.strip() else "")
    box = tk.Frame(parent, bg=CARD)
    tk.Frame(box, bg=MARK, width=3, height=15).pack(side="left", padx=(0, 8))
    ttk.Label(box, text=text, style="Section.TLabel").pack(side="left")
    if caption:
        ttk.Label(box, text=_spaced(caption), style="Caption.TLabel").pack(side="left", padx=(10, 0), pady=(3, 0))
    return box
