"""화면 글자 하나를 어떤 번역으로 보여줄지 정한다.

  1. 사용자 용어집 (내가 정한 것이 언제나 먼저)
  2. 공식 번역 DB
  3. 번역 묶음 (배포자가 미리 번역해 나눠 준 것, pack.py)
  4. 번역 캐시 (.md)
  5. 기계 번역 — 뒤에서 돌리고, 끝나면 캐시에 적은 뒤 화면을 갱신한다

1~4 는 네트워크가 없으므로 ms 단위로 끝난다.
"""
from __future__ import annotations

import queue
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, replace

from . import errlog
from . import mt as mt_mod
from . import textnorm as tn
from .memory import CACHE_HEADER, GLOSSARY_HEADER, MarkdownStore
from .names import NameIndex, load_names
from .official import OfficialDB
from .pack import PackStore
from .stylist import DEFAULT_MODELS

RETRY_AFTER = 30.0
MIN_INTERVAL = 0.12     # 번역 서버를 몰아치지 않는 최소 간격(초)
LOOKAHEAD_LINES = 15    # 공식 번역 없는 스토리에서 미리 번역해 둘 줄 수
URGENT, BACKGROUND = 0, 1
# 앞뒤 글자를 놓친 짧은 화면 글자를 채워 보는 길이. 세 글자보다 짧으면 맞는 글이 너무 많다
COMPLETE_MIN, COMPLETE_MAX = 3, 8
# 한 글자를 잘못 읽은 화면 글을 찾아보는 길이. 다섯 글자 이하는 한 자만 다른 진짜 글('限定寻访'·'限时寻访')이 흔하다
ONE_OFF_MIN, ONE_OFF_MAX = 6, 12
LABEL_MAX = 12          # 값 앞의 글을 따로 기계 번역할 때 글 길이 상한 (그보다 길면 문장째 번역한다)
# 문단이 화면 가장자리에서 잘려 일부만 읽힌 글을 공식·묶음의 긴 글에서 찾는다. 열두 글자는 되어야 맞는 글이 하나로 좁혀지고,
# 읽은 글이 그 글의 이만큼은 되어야 번역 전체를 보여 줘도 어색하지 않다 (적으면 조각만 기계 번역하는 편이 낫다)
WITHIN_MIN, WITHIN_RATIO = 12, 0.4
# 여러 공식 항목이 한 블록으로 이어 읽힌 긴 글(모듈 스토리 일기 여러 편, 대사와 지문)을 문장 2~5개씩 묶어 하나씩 공식 번역과 맞춘다
SENT_MIN, SENT_SPAN = 40, 5
_SENT_SPLIT = re.compile(r"[^。！？!?]*[。！？!?]+[”’」』）)]*|[^。！？!?]+$")
WITHIN_TPL_RATIO = 0.15   # 숫자 틀을 찾을 때는 더 적게 보여도 된다 (보이는 절만 잘라 쓴다. 절이 안 맞으면 WITHIN_RATIO 를 다시 본다)
WITHIN_CUTS = ((0, 0), (1, 0), (0, 1), (1, 1), (2, 0), (0, 2), (2, 2))   # 잘린 끝에서 헛읽은 글자를 이만큼 떼어 본다
WITHIN_CACHE = 512


@dataclass
class Resolved:
    text: str | None     # None 이면 아직 보여줄 번역이 없다
    via: str             # user / official / cache / mt / pending / skip
    zh: str = ""
    story: bool = False  # 지금 읽고 있는 스토리의 대사로 확인되었다
    missed: tuple = (0, 0)   # 이름 앞뒤로 OCR 이 놓친 글자 수 ('克莱门莎' 를 '莱门莎' 로 읽었으면 (1, 0))
    # 헛것이라 떼어 낸 화면 글자의 앞뒤 글자 수 ('0通过步数' → (1, 0)). engine 이 상자를 남은 글자로 줄인다.
    # 안 그러면 옆의 큰 숫자 '19' 까지 상자에 들어가 번역문이 커진다
    trim: tuple = (0, 0)


_NOISE_HEAD = re.compile(r"^(?=[^㐀-鿿]*[^\w\s])[^㐀-鿿A-Za-z]{1,4}(?=[㐀-鿿])")
# 카드 테두리·장식선을 읽은 '一' 다음에 따옴표·숫자·기호가 오면 헛것이다
# ('一“011零件箱' → '1. “011 부품 상자”', '一：，1、0一' → '1:, 1, 0-')
_STROKE_HEAD = re.compile(r"^[一丨丿丶]+(?=[\"“”'‘’「」『』《〈<(（\[【0-9０-９A-Za-z：:!！?？\-—_.．·•])")
# 여는 따옴표 앞 세 글자 이내의 장식 헛것 ('以0一“011零件箱'). 숫자나 선 글자가 섞이고 보통 한자는 하나 이하일 때만
_QUOTE_HEAD = re.compile(r"^([^“\"「『]{1,3})(?=[“\"「『])")
_NOISE_MARK = re.compile(r"[`^\\|~]")
_BRACKET_TAIL = re.compile(r"(.+?)\s*([（(][^（()）]+[）)])")
_STROKES = set("一丨丿乙亅乚二十口囗冂凵卜丶")   # OCR 이 선이나 작은 아이콘을 읽으면 나오는 글자
_UNIT_HEAD = re.compile(r"(?:天|小时|分钟|秒)")
_ICON_TAIL = _STROKES | set("三")                   # 뒤에 붙는 아이콘을 읽은 글자 (필터 ≡ → 三)
_TAG_SEP = re.compile(r"[\[\]【】．·|/]+")           # 나란한 꼬리표 사이에 OCR 이 읽어 넣는 구분 기호
_COUNTDOWN_TAIL = re.compile(r"(?<=[㐀-鿿])(?=\d+\s*(?:天|小时|分钟|秒)$)")   # 글자 바로 뒤에 붙은 남은 시간
_ICON_ZERO = re.compile(r"^0(?=\d+\s*(?:天|小时|分钟|秒)$)")   # 시계 아이콘을 읽은 '0' ('056秒' → '56秒')
_PUNCT = " ，。！？、：；…—·,.!?:;\"'“”‘’《》〈〉<>()（）"
# 이름 끝 '-α/β/γ' 를 영문으로 읽은 것 (통합전략 유물 '林间夜话-α')
_GREEK = re.compile(r"(?<=[㐀-鿿][-－])([aABbyY])(?![A-Za-z])")
_GREEK_OF = {"a": "α", "A": "α", "B": "β", "b": "β", "y": "γ", "Y": "γ"}
_PLAYER_NAME = re.compile(r"您\s*Dr\.\s*[^\s，。！？、“”]{1,8}?\s*(?=使用|于|在|，|已|见证|的)")
_SENTENCE_MARK = re.compile(r"[。！？!?…]")         # 대사·문장 표시 (이름 그대로가 아니다)
_COUNTERS = set("个名次件位层级张份")
# 통합전략 화폐(오리지늄 각뿔) 아이콘. OCR 은 '▲'(PaddleOCR)·'△'·'人△'(Windows OCR)로 읽는다. 번역문에는 '▲' 로 남기고
# 오버레이가 그 자리에 화면의 아이콘을 그대로 잘라 붙인다 (engine._icon_crop, overlay)
ICON = "▲"
_ICON_READ = re.compile(r"(?:人\s*)?[▲△](?=\s*\d)")
# '是否以▲10的价格出售血橙？' (상점에서 물건 팔기). 한국 서버 조각 '的价格出售' → '가격으로 판매하시겠습니까?'
# '是否消耗▲12购买林间夜话-a？' (상점에서 사기). OCR 이 '-a' 를 '一a' 로 읽기도 한다
_BUY = re.compile(r"是否消耗\s*(?:人\s*)?[▲△]?\s*(?P<n>\d+)\s*购买\s*(?P<item>[^？?]+?)\s*[？?]?")
_ITEM_SUFFIX = re.compile(r"[一—－]\s*(?=[a-zA-Zα-ω]$)")
_SELL = re.compile(r"是否以\s*(?:人\s*)?[▲△]?\s*(?P<n>\d+)\s*的价格出售\s*(?P<item>[^？?]+?)\s*[？?]?")
_PIECE_END = set("。！？%）)」”")                    # 한 블록을 두 묶음 줄로 갈라 볼 자리 (_pack_pair)               # 숫자 바로 뒤에 오는 세는 말
# 숫자+시간 단위뿐인 남은 시간. 공식 글 '≤ 8小时'(> 8시간) 과 키가 같아 직접 옮긴다
_COUNTDOWN_ONLY = re.compile(r"(\d+)\s*(天|小时|分钟|秒)")
_UNITS = {"天": "일", "小时": "시간", "分钟": "분", "秒": "초"}
_JUNK_TAIL = set("`^\\|~@")                      # 글 뒤에 붙은 아이콘·테두리 헛것 ('提升^', '剩余20 @')
# 대사의 말줄임표 '……'·줄표 '——' 를 OCR 이 '一一' 로 읽는다 ('唔咕咕一一好，好累啊一一果然连续值班')
_DASHES = re.compile(r"(?<=[㐀-鿿，。！？、])一一+(?=[㐀-鿿，。！？、．.]|$)")
# 글 뒤에 붙은 값 ('剩余15', '库存更新倒计时：25D 08：36：30'). 값은 숫자·영문 단위·시각 기호만
_VALUE_CHARS = r"[0-9０-９A-Za-z：:．.\s/%+\-]"
_LABEL_VALUE = re.compile(rf"(?P<label>.*[㐀-鿿])\s*(?P<sep>[：:])?\s*(?P<value>{_VALUE_CHARS}*[0-9０-９]{_VALUE_CHARS}*)")
# 스테이지 번호와 이름 ('国16·16示我以': 메인 화면의 '当前 16-16 示我以真'. '国' 은 '当前' 칸, 뒤는 그림에 가렸다)
# 그림에 반쯤 가린 끝 글자는 영문 한 자로 읽힌다 ('当前16-16示我以J', PaddleOCR)
# 이름표 뒤의 영문 장식 글자 (대문자 3자 이상)
# 이름표 둘레의 영문 장식을 섞어 읽은 것 ('EVO lufiol视相博物馆LYUISION演化'). 3자 이상 이어진 로마자 덩어리
_LATIN_RUN = re.compile(r"[A-Za-z@][A-Za-z0-9@&'.\s]{2,}")
LATIN_PIECE_MAX = 12    # 붙어 읽힌 이름표 둘을 가를 때 볼 한자 덩어리 길이 상한
_EN_CAPTION = re.compile(r"(?P<zh>[^A-Za-z]*[㐀-鿿][^A-Za-z]*?)[\s\-·]*(?P<en>[A-Z][A-Z0-9 &'.]{2,})")
_STAGE_LINE = re.compile(r"(?P<head>\D{0,2}?)\d?(?P<a>\d{1,2})\s*[-·．.—]\s*(?P<b>\d{1,2})\s*(?P<name>[㐀-鿿]{2,12})[A-Za-z]?")


def _strip_noise_head(text: str) -> str:
    """앞에 붙은 아이콘·선 오인식을 뗀다 ('！0选择助战' → '选择助战', '-0一“011零件箱' → '011零件箱').
    기호와 선 '一' 가 번갈아 붙기도 해서 더 뗄 것이 없을 때까지 되풀이한다."""
    for _ in range(4):
        stripped = _STROKE_HEAD.sub("", _strip_symbol_head(text))
        m = _QUOTE_HEAD.match(stripped)
        if m:
            head = m.group(1)
            plain = [ch for ch in head if "㐀" <= ch <= "鿿" and ch not in _STROKES]
            if len(plain) <= 1 and any(ch.isdigit() or ch in _STROKES for ch in head):
                stripped = stripped[m.end():]
        if stripped == text:
            break
        text = stripped
    return text


def _strip_symbol_head(text: str) -> str:
    """두 자리 넘는 숫자는 진짜 숫자라 남긴다. 터미널의 남은 기간 '．12天' 을 '天' 으로 줄이면 '일' 만 뜬다."""
    m = _NOISE_HEAD.match(text)
    if not m:
        return text
    # 앞머리 끝의 숫자가 두 자리 넘거나 바로 뒤에 시간 단위가 오면 남은 기간이다 ('．3天' → '3天')
    digits = re.search(r"\d+$", m.group())
    if digits and (len(digits.group()) >= 2 or _UNIT_HEAD.match(text, m.end())):
        return text[digits.start():]
    return text[m.end():]


def worth_translating(text: str, charset: set[str]) -> bool:
    """기계 번역에 보낼 만한 글자인가. 움직이는 배경에서 나온 헛것을 걸러 낸다."""
    cjk = tn.cjk_count(text)
    if cjk < 2 or _NOISE_MARK.search(text):
        return False
    alnum = sum(1 for ch in text if ch.isalnum())
    if cjk / max(1, alnum) < 0.5:
        return False
    if charset:
        # 게임 데이터 전체에 한 번도 안 나오는 글자가 있으면 오인식이다 (氵, 扌 등)
        unknown = sum(1 for ch in text if "㐀" <= ch <= "鿿" and ch not in charset)
        if unknown:
            return False
    return True


class Resolver:
    def __init__(self, cfg: dict, data_dir: str, on_ready=None, log=None):
        from .glossary import Glossary

        self.cfg = cfg
        self.on_ready = on_ready or (lambda: None)
        self.log = log or (lambda msg: None)
        # 한도 소진처럼 사용자가 골라야 하는 일. 패널이 알림창으로 바꿔 끼운다
        self.alert = lambda kind, info: None
        self.db = OfficialDB(f"{data_dir}/official.sqlite")
        self.cache = MarkdownStore(f"{data_dir}/번역캐시.md", CACHE_HEADER)
        self.user = MarkdownStore(f"{data_dir}/용어집.md", GLOSSARY_HEADER)
        self.pack = PackStore(f"{data_dir}/pack")
        self.glossary = Glossary(self.db, self.user)
        # 오퍼레이터 이름 사전. 묶음·DB 에서 모으는 데 시간이 걸려 뒤에서 만든다
        self.names = NameIndex([])
        self._pack_terms: set[str] = set()     # 번역 묶음에서 가져온 용어 (한국 서버에 없는 이름)
        threading.Thread(target=self._load_names, name="names", daemon=True).start()
        self.charset = set(self.db.meta.get("charset", ""))
        self.rare = set(self.db.meta.get("rare", ""))     # 게임 글에 거의 안 나오는 한자 (아이콘 오인식 판단용)
        self.engine = mt_mod.create(cfg, self.log, self._quota)
        self.story_ai = None
        self.set_story_ai(cfg)
        self.stats = {"official": 0, "pack": 0, "cache": 0, "mt": 0, "user": 0, "failed": 0}
        self._index = None                                                 # 조각 색인 (cfg['matcher'] 가 'index'·'shadow' 일 때만 만든다)
        self._shadow_path = f"{data_dir}/색인비교기록.jsonl"
        self._shadow_seen: set[str] = set()
        self._shadow_q: queue.Queue = queue.Queue(maxsize=64)
        if cfg.get("matcher") in ("index", "shadow"):
            self._index = self._new_index()                                # 켜자마자 뒤에서 묶음 색인을 만든다
        self._sent_cache: dict[str, list[str] | None] = {}                 # 문장 묶음 맞추기 결과 (_by_sentences)
        self._within_cache: dict[str, tuple[str, str, str] | None] = {}    # 잘린 문단 찾기 결과 (_within)

        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = 0
        self._lock = threading.Lock()
        self._inflight: set[str] = set()
        self._failed_at: dict[str, float] = {}
        self._last_call = 0.0
        self._stop = False
        for i in range(2):
            threading.Thread(target=self._worker, name=f"mt{i}", daemon=True).start()
        if cfg.get("matcher") == "shadow":
            threading.Thread(target=self._shadow_worker, name="shadow", daemon=True).start()

    def stop(self) -> None:
        if self.story_ai is not None:
            self.story_ai.stop()
        self._stop = True
        self._queue.put((-1, 0, ""))
        self._queue.put((-1, 0, ""))

    def reload_pack(self) -> None:
        """새로 받은 번역 묶음을 읽고, 한국 서버에 없는 오퍼레이터 이름도 묶음 것으로 다시 만든다."""
        self.pack.load()
        self._within_cache.clear()
        self._sent_cache.clear()
        threading.Thread(target=self._load_names, name="names", daemon=True).start()

    def _load_names(self) -> None:
        pairs = load_names(self.db, self.pack)
        self.glossary.add_names(pairs)
        self.glossary.add_terms(self.db.rogue_names())
        cn = self.db.cn_names(self._pack_ko)
        self._pack_terms = {zh for zh, _ko in cn}
        self.glossary.add_terms(cn)
        self.names = NameIndex(pairs)

    def _pack_ko(self, k: str) -> str | None:
        found = self.pack.get(k)
        return tn.display_ko(found[0], self.cfg.get("nickname", "박사")) if found else None

    def _quota(self, chain) -> None:
        self.alert("quota", chain)

    def set_engine(self, cfg: dict) -> None:
        """번역기나 키를 바꾸면 새로 만든다. 한도 소진으로 멈췄던 번역기도 다시 시도한다."""
        self.engine = mt_mod.create(cfg, self.log, self._quota)
        self.set_story_ai(cfg)

    def set_story_ai(self, cfg: dict) -> None:
        """스토리 말투 맞춤(Gemini). 켜져 있고 키가 있을 때만 만든다."""
        if self.story_ai is not None:
            self.story_ai.stop()
        self.story_ai = None
        if cfg.get("gemini_style") and cfg.get("gemini_key"):
            from .story_ai import StoryTranslator
            self.story_ai = StoryTranslator(self, cfg["gemini_key"], cfg.get("gemini_models") or DEFAULT_MODELS)

    def reload_files(self) -> bool:
        """사용자가 .md 를 고쳤으면 다시 읽는다."""
        a = self.cache.refresh()
        b = self.user.refresh()
        return a or b

    # ------------------------------------------------------------------ 결정
    def resolve(self, text: str, allow_mt: bool = True, masked: str = "", pick=None) -> Resolved:
        """masked: 반쪽으로 갈라 읽은 글자를 '?' 로 바꾼 글자. pick(후보들, '?' 자리) 는 화면 글자
        모양으로 후보 하나를 고른다 (engine 이 준다)."""
        # 시계 아이콘을 '0' 으로 읽은 남은 시간 ('018秒' → '18秒')
        stripped = self._strip_odd(_ICON_ZERO.sub("", _strip_noise_head(text).strip()))
        # 기록 화면의 '您Dr.<플레이어 이름>使用…'. 한글 이름은 OCR 이 못 읽어 엉뚱한 한자('叫导')가 되고 기계 번역이
        # 'Dr. Jiaodao' 를 만든다. 게임 글의 'Dr.{@nickname}' 처럼 '博士' 로 바꿔 번역한다 (→ '박사님')
        stripped = _PLAYER_NAME.sub("您博士", stripped)
        k = tn.key(stripped)
        # 선·테두리·아이콘을 읽은 헛것 ('一。' 이 요일 '월' 로 번역되어 메인 화면에 떠 있었다).
        # 헛것을 떼고 글자 하나만 남았으면 그것도 헛것이다 (파티클 '0，与' → '와')
        if not k or (len(k) <= 2 and set(k) <= _STROKES) or (stripped != text and len(k) <= 1):
            return Resolved(None, "skip")
        # 한자가 선 모양 글자뿐이고 나머지는 숫자·기호면 아이콘을 읽은 헛것이다 (첸 스킬 아이콘 '：，1、0一')
        cjk = {ch for ch in k if "㐀" <= ch <= "鿿"}
        if cjk and cjk <= _STROKES and not re.search(r"[A-Za-z]", k):
            return Resolved(None, "skip")
        res = self._reconcile_numbers(stripped, self._decide(stripped, k, allow_mt, masked, pick))
        if res.text and res.trim == (0, 0) and res.missed == (0, 0) and res.zh:
            # 퍼지로 맞춘 글이 화면 글자의 앞뒤를 뺀 것이면 그만큼이 헛것이다 ('凰招募干员次数' → '招募干员次数')
            res = replace(res, trim=_affix_trim(stripped, _clean(res.zh)))
        at = text.find(stripped)
        if res.text and stripped != text and at >= 0:
            res = replace(res, trim=(res.trim[0] + at, res.trim[1] + len(text) - at - len(stripped)))
        return res

    def _reconcile_numbers(self, text: str, res: Resolved) -> Resolved:
        """화면 숫자가 게임 데이터 글의 숫자와 다르면 번역 속 옛 숫자를 화면 숫자로 바꾼다.

        스택에 따라 계속 변하는 값(위수협의 '+46')이나 OCR 이 숫자를 틀리게 읽은 글이 비슷한 공식 글에 맞으면, 번역에 데이터의
        숫자(+12)가 그대로 떠서 화면(+46)과 달랐다 (숫자만 바꾼 합성 150건 중 26% 가 옛 숫자). 두 글의 숫자 개수가 같고 값이
        다른 자리마다, 그 옛 숫자가 번역에 정확히 한 번 나올 때만 바꾼다 (모호하면 그대로 둔다). 숫자 틀('{')은 이미 채웠다."""
        if res.via not in ("official", "pack") or not res.text or not res.zh or "{" in res.zh or res.zh == text:
            return res
        zn = [n.lstrip("-") for n in tn.numbers_of(tn.strip_rich(res.zh).replace("\\n", " "))]
        sn = [n.lstrip("-") for n in tn.numbers_of(tn.strip_rich(text))]
        if not zn or len(zn) != len(sn) or zn == sn:
            return res
        out = res.text
        for old, new in zip(zn, sn):
            if old == new or zn.count(old) > 1:
                continue
            runs = list(re.finditer(r"(?<![\d.])" + re.escape(old) + r"(?![\d.])", out))
            if len(runs) == 1:
                out = out[:runs[0].start()] + new + out[runs[0].end():]
        return res if out == res.text else replace(res, text=out)

    def _decide(self, text: str, k: str, allow_mt: bool, masked: str, pick) -> Resolved:
        own = self.user.get(k)
        if own:
            self.stats["user"] += 1
            return Resolved(own[0], "user", text)

        # 한국 서버에 없는 오퍼레이터 이름 (번역 묶음 names.tsv). 이름만 적힌 화면 글이면 공식 글의 같은 글자 대사
        # ('国度' → "'국가'.")나 묶음의 대사 줄('嘉辛塔？' → '자신타?')보다 먼저 쓴다
        named = self.pack.name(k)
        if named and not _SENTENCE_MARK.search(text):
            self.stats["pack"] += 1
            return Resolved(named, "pack", text)

        clock = _countdown(text)
        if clock:
            self.stats["official"] += 1
            return Resolved(clock, "official", text)

        hit = _usable(self.db.lookup(text, fuzzy=False), k)
        if hit and hit.ko:
            return self._official(hit, text)
        if hit is None or hit.via == "template":
            # 한국 서버에 없는 숫자 틀 (스킬 설명 등): 번역 묶음의 틀을 화면 숫자로 채운다. 묶음 틀은 게임 글의 줄
            # (줄바꿈)마다 따로 있어, 화면에서 따로 묶인 '过载：…' 같은 단락도 맞는다. 못 채우면 화면 글자를 그대로
            # 다룬다 (DB 의 틀 원문을 기계 번역하면 '{atk_scale:0%}' 가 화면에 뜬다)
            filled = self._pack_template(text) or self._template_within(text) or self._template_split(text)
            if filled is not None:
                return filled
            hit = None

        # 문장 표에는 없어도 용어로 확인된 낱말일 수 있다 ('整合运动' 단독)
        bare = _clean(text).strip(_PUNCT)
        term = self.glossary.base.get(bare)
        if term:
            via = "pack" if bare in self._pack_terms else "official"
            self.stats[via] += 1
            return Resolved(term, via, text)

        # 아래 둘은 번역 캐시보다 먼저 본다. 예전에 기계 번역해 둔 헛것('囝较慢 → 아이가 좀 느리네요',
        # '束束刺 → 가시 뭉치')을 이긴다
        if hit is None:
            # 글자 앞뒤의 아이콘을 글자로 읽은 것 ('囝较慢', '一若叶睦', '职业三', '0选择助战')
            trimmed = (self._english_caption(text) or self._latin_noise(text) or self._without_icons(text)
                       or self._by_parts(text))
            if trimmed is not None:
                return trimmed
            # 게임 데이터에 없는 짧은 글자는 잘못 읽은 오퍼레이터 이름일 수 있다
            name = self._guess_name(text, masked, pick)
            if name is not None:
                return name
            # 선 글자가 섞인 두 글자는 아이콘·테두리를 읽은 헛것이다 (상점 화면 왼쪽 아래 '飞一' → '페이이')
            if len(k) <= 2 and set(k) & _STROKES and not self.pack.get(k):
                return Resolved(None, "skip")
            dashed = self._dashed(text) or self._greek(text)
            if dashed is not None:
                return dashed
            # 짧은 화면 글자의 앞뒤를 놓치고 가운데만 읽은 것 ('目标生命值' → '标生命'), 한 자를 잘못 읽은 것
            if self.cfg.get("matcher") == "index":
                # 조각 색인 (실험, 브랜치 index-matcher): 잘린 문단·이어 붙은 공식 항목을 한 방식으로 맞춘다
                whole = self._completed(text) or self._one_off(text) or self._indexed(text)
            else:
                whole = self._completed(text) or self._one_off(text) or self._by_sentences(text) or self._within(text)
                if self.cfg.get("matcher") == "shadow":
                    self._shadow_offer(text, whole)
            if whole is not None:
                return whole
            # 두 줄을 한 덩어리로 읽은 '职业需求（最多3项）' · 남은 시간이 앞에 붙은 '27：弓1：22联络中'
            # · 스테이지 번호 '16-16 示我以真' · 값이 붙은 '剩余15'
            joined = (self._sell_prompt(text) or self._bracket_tail(text) or self._stage_line(text)
                      or self._noisy_prefix(text) or self._label_value(text, allow_mt))
            if joined is not None:
                return joined

        zh = _clean(hit.zh) if hit else text
        packed = self._packed(zh, k, hit)
        if packed is not None:
            return packed
        cached = self.cache.get(tn.key(zh)) or self.cache.get(k)
        if cached:
            self._after_story_hit(hit)
            self.stats["cache"] += 1
            return Resolved(with_icon(text, tn.strip_rich(cached[0])), "cache", zh, _in_story(hit))

        if not hit:
            hit = _usable(self.db.lookup(text, fuzzy=True), k)
            if hit and hit.ko:
                return self._official(hit, text)
            if hit:
                zh = _clean(hit.zh)
                packed = self._packed(zh, "", hit)
                if packed is not None:
                    return packed
                cached = self.cache.get(tn.key(zh))
                if cached:
                    self._after_story_hit(hit)
                    self.stats["cache"] += 1
                    return Resolved(tn.strip_rich(cached[0]), "cache", zh, _in_story(hit))

        self._after_story_hit(hit)
        known = hit is not None   # 게임 데이터에서 확인된 원문은 OCR 오류가 없다
        if not known and not worth_translating(text, self.charset):
            return Resolved(None, "skip")
        if allow_mt or known:
            self.enqueue(zh, URGENT)
        return Resolved(None, "pending", zh, _in_story(hit))

    def _packed(self, zh: str, k: str, hit) -> Resolved | None:
        """번역 묶음에 있으면 그 번역. 번역캐시(내가 돌린 기계 번역)보다 먼저 본다."""
        found = self.pack.get(tn.key(zh)) or (self.pack.get(k) if k else None)
        if found is None:
            return None
        self._after_story_hit(hit)
        self.stats["pack"] += 1
        return Resolved(tn.display_ko(found[0], self.cfg.get("nickname", "박사")), "pack", zh, _in_story(hit))

    def _pack_template(self, text: str) -> Resolved | None:
        """번역 묶음의 숫자 틀 번역을 화면 글자의 숫자로 채운다. 틀이 없거나 숫자 개수가 틀과 다르면 None."""
        tk = tn.template_key(text)
        if tn.SLOT not in tk or tn.cjk_count(tk) < 3:
            return None
        found = self.pack.template(tk)
        if found is None:
            pair = self._pack_pair(text)
            if pair is not None:
                return pair
            found = self.pack.similar_template(tk)
            if found is None:
                return None
        filled = tn.fill_template(found[0], found[1], text)
        if filled is None:
            return None
        self.stats["pack"] += 1
        return Resolved(tn.display_ko(filled, self.cfg.get("nickname", "박사")), "pack", found[0])

    def speaker_ko(self, zh: str) -> str:
        """스토리 화자 이름의 한국어 (내 용어집 → 묶음 이름 → 공식 이름·용어). 없으면 원문 그대로."""
        k = tn.key(zh)
        own = self.user.get(k) if k else None
        if own:
            return own[0]
        return (self.pack.name(k) if k else None) or self.glossary.base.get(zh) or zh

    def known_text(self, text: str) -> bool:
        """번역이 정해진 글인가 (내 용어·이름·묶음·공식·숫자 틀). 갈린 블록을 이을지 볼 때 쓴다 (engine.join_split_texts).
        통계를 세지 않고 기계 번역도 부르지 않는다."""
        k = tn.key(text)
        if not k:
            return False
        if self.user.get(k) or self.pack.name(k) or self._pack_piece(text):
            return True
        hit = self.db.lookup(text, fuzzy=False)
        return bool(hit and hit.ko)

    def _pack_piece(self, text: str) -> str | None:
        """번역 묶음에 있는 줄 하나의 번역 (숫자 틀이면 화면 숫자로 채운 것)."""
        found = self.pack.get(tn.key(text))
        if found is not None:
            return found[0]
        tk = tn.template_key(text)
        tpl = (self.pack.template(tk) or self.pack.similar_template(tk)) if tn.SLOT in tk else None
        return tn.fill_template(tpl[0], tpl[1], text) if tpl else None

    def _pack_pair(self, text: str) -> Resolved | None:
        """두 줄을 한 덩어리로 읽은 것: 스킬 설명 끝줄 '…降低至15%' 과 그 아래 주황 줄 '该技能可随时主动关闭' 은 따로 번역해
        둔 묶음 줄인데, 화면에서 위아래로 붙어 한 블록이 됐다. 문장 끝(。！？%）) 뒤에서 한 번 갈라 두 쪽 다 묶음에 있으면 잇는다."""
        if len(text) < 12:
            return None
        for i in range(4, len(text) - 3):
            if text[i - 1] not in _PIECE_END or not ("㐀" <= text[i] <= "鿿"):
                continue
            left = self._pack_piece(text[:i])
            right = self._pack_piece(text[i:]) if left is not None else None
            if left is not None and right is not None:
                self.stats["pack"] += 1
                nick = self.cfg.get("nickname", "박사")
                return Resolved(f"{tn.display_ko(left, nick)} {tn.display_ko(right, nick)}", "pack", text)
        return None

    def _official(self, hit, text: str) -> Resolved:
        self.stats["official"] += 1
        ko = tn.display_ko(hit.ko, self.cfg.get("nickname", "박사"))
        # 문장 부호를 뺀 키로 찾으므로 '仓库' 가 대사 '仓库？' 의 번역을 얻을 수 있다.
        # 화면 글자에 없는 물음표는 떼어 낸다
        if ko.endswith("?") and tn.has_cjk(text) and text.rstrip()[-1:] not in ("?", "？", "…"):
            ko = ko[:-1].rstrip()
        # '分支' 가 버튼 '分支+' 의 번역 '세부분류+' 를 얻었을 때도 같다
        if ko.endswith("+") and tn.has_cjk(text) and text.rstrip()[-1:] not in ("+", "＋"):
            ko = ko[:-1].rstrip()
        return Resolved(ko, "official", hit.zh, _in_story(hit))

    def _by_parts(self, text: str) -> Resolved | None:
        """나란히 붙은 꼬리표 여럿을 한 줄로 읽은 것을 하나씩 공식 번역으로 옮겨 잇는다.

        스킬의 '自动回复' '手动触发' '⏱22秒' 꼬리표가 '自动回复]手动触发．22秒' 한 줄로 읽혀 기계 번역이
        '[자동 응답] 수동 실행' 이 됐다. 괄호·가운뎃점 같은 구분 기호로 나눈 조각이 모두 게임 데이터에 있을 때만 쓴다.
        """
        # '被动 ⏱56秒' 처럼 구분 기호 없이 붙은 남은 시간도 따로 뗀다. 시계 아이콘은 '0' 으로 읽혀 '被动056秒' 가 된다
        parts = [p for p in _TAG_SEP.split(_COUNTDOWN_TAIL.sub("|", text)) if tn.key(p)]
        if len(parts) < 2:
            return None
        parts = [_ICON_ZERO.sub("", p) for p in parts]
        out = []
        for part in parts:
            clock = _countdown(part)
            if clock:
                out.append(clock)
                continue
            hit = self.db.lookup(part, fuzzy=False)
            if hit and hit.ko and hit.file_id < 0:
                out.append(tn.display_ko(hit.ko, self.cfg.get("nickname", "박사")))
                continue
            # 한 자를 잘못 읽은 조각 ('下次赠这寻访．8小时' 의 '下次赠这寻访' = '下次赠送寻访')
            near = self._one_off(part)
            if near is None:
                return None
            out.append(near.text)
        self.stats["official"] += 1
        return Resolved(" · ".join(out), "official", text)

    def _strip_odd(self, text: str) -> str:
        """게임 글에 한 번도 안 나오는 한자(氵·冫·艹 같은 부수)가 섞인 기호 앞머리와, 뒤에 붙은 헛 기호를 뗀다.
        ("氵'以上干员获取概率提升^" → '以上干员获取概率提升', '氵、／日冕重烈' → '日冕重烈', '剩余20 @' → '剩余20').
        안 떼면 worth_translating 이 글 전체를 헛것으로 보고 번역하지 않았다."""
        def odd(ch: str) -> bool:
            return "㐀" <= ch <= "鿿" and bool(self.charset) and ch not in self.charset

        i = 0
        while i < min(len(text), 5) and (odd(text[i]) or not (text[i].isalnum() or text[i].isspace())):
            i += 1
        if i and any(odd(ch) for ch in text[:i]) and i < len(text) and "㐀" <= text[i] <= "鿿":
            text = text[i:]
        j = len(text)
        while j > 0 and (odd(text[j - 1]) or text[j - 1] in _JUNK_TAIL or text[j - 1].isspace()):
            j -= 1
        if j < len(text) and len(tn.key(text[:j])) >= 2:
            text = text[:j]
        return text

    def _dashed(self, text: str) -> Resolved | None:
        """대사의 '……'·'——' 를 '一一' 로 읽은 줄을 고쳐 다시 찾는다. 메인 화면 비서 대사
        '唔咕咕一一好，好累啊一一果然连续值班' 가 공식 번역 대신 기계 번역으로 떴다."""
        fixed = _DASHES.sub("……", text)
        if fixed == text:
            return None
        hit = _usable(self.db.lookup(fixed, fuzzy=True), tn.key(fixed))
        if hit is None:
            return None
        if hit.ko:
            return self._official(hit, text)
        return self._packed(_clean(hit.zh), "", hit)

    def _greek(self, text: str) -> Resolved | None:
        """그리스 문자를 영문으로 읽은 이름 ('林间夜话-a' → '林间夜话-α' 숲속의 야화 α). PaddleOCR 이 그렇게 읽는다."""
        fixed = _GREEK.sub(lambda m: _GREEK_OF[m.group(1)], text)
        if fixed == text:
            return None
        hit = _usable(self.db.lookup(fixed, fuzzy=False), tn.key(fixed))
        if hit is None:
            return None
        if hit.ko:
            return self._official(hit, text)
        return self._packed(_clean(hit.zh), "", hit)

    def _one_off(self, text: str) -> Resolved | None:
        """한 글자를 잘못 읽은 화면 글 ('下次赠这寻访' → '下次赠送寻访' 다음 보너스까지). 한 자리만 다른 화면 글이
        한 가지 번역으로만 있을 때 쓴다. 첫 글자는 바꿔 보지 않는다 (GLOB 이 색인을 못 타 느리다)."""
        k = tn.key(text)
        if not (ONE_OFF_MIN <= len(k) <= ONE_OFF_MAX) or not all("㐀" <= ch <= "鿿" for ch in k):
            return None
        found = {}
        for i in range(1, len(k)):
            for ck, zh in self.db.glob_keys(k[:i] + "?" + k[i + 1:], 4):
                hit = self._ui_hit(zh) if ck != k else None
                if hit is not None:
                    found.setdefault(hit.ko, hit)
        if len(found) != 1:
            return None
        return self._official(next(iter(found.values())), text)

    def _sell_prompt(self, text: str) -> Resolved | None:
        """'是否以▲10的价格出售血橙？' → '블러드 오렌지를 ▲10 가격으로 판매하시겠습니까?',
        '是否消耗▲12购买林间夜话-a？' → '숲속의 야화 α를 ▲12에 구매하시겠습니까?'. 물건 이름은 게임 데이터(공식·묶음)에
        있을 때만 쓴다. 통째로 기계 번역하면 아이콘을 빼먹고('블러드 오렌지를 10에 판매하시나요?') 말투도 매번 달랐다."""
        sell = _SELL.fullmatch(text.strip())
        m = sell or _BUY.fullmatch(text.strip())
        if not m:
            return None
        item = self.resolve(_ITEM_SUFFIX.sub("-", m.group("item")), allow_mt=False)
        if not item.text or item.via not in ("official", "pack", "user"):
            return None
        from .glossary import _fix_particle

        name = item.text.strip()
        obj = f"{name}{_fix_particle(name, '을', ' ')}"
        price = f"{ICON}{m.group('n')}"
        self.stats["official"] += 1
        ko = f"{obj} {price} 가격으로 판매하시겠습니까?" if sell else f"{obj} {price}에 구매하시겠습니까?"
        return Resolved(ko, "official", text)

    def _latin_noise(self, text: str) -> Resolved | None:
        """중국어 이름표 앞뒤·사이의 영문 장식을 섞어 읽은 줄 ('EvoluniO视相博物馆演化', 'Evolunon视相博物馆演化@LVUISIoN').
        PaddleOCR v6 인식 모델은 장식 영문까지 읽는다. 로마자 덩어리를 떼고 남은 한자 덩어리가 저마다 확인된 번역(공식·
        묶음·내 용어)이면 그 번역을 잇는다. 이름표 둘이 붙어 읽혔으면('视相博物馆演化') 둘 다 확인되는 자리에서 가른다."""
        if not _LATIN_RUN.search(text):
            return None
        pieces = [p.strip(" ·-—|") for p in _LATIN_RUN.split(text)]
        pieces = [p for p in pieces if p]
        if not pieces or len(pieces) > 3:
            return None
        parts, vias = [], set()
        for piece in pieces:
            found = self._known_parts(piece)
            if found is None:
                return None
            for ko, via in found:
                parts.append(ko)
                vias.add(via)
        head = text.find(pieces[0])
        tail = len(text) - (text.rfind(pieces[-1]) + len(pieces[-1]))
        via = "official" if vias == {"official"} else ("user" if "user" in vias else "pack")
        self.stats[via] += 1
        return Resolved(" ".join(parts), via, text, trim=(max(0, head), max(0, tail)))

    def _known_parts(self, piece: str) -> list[tuple[str, str]] | None:
        """한자 덩어리 하나의 확인된 번역 [(번역, 출처)]. 통째로 없으면 앞뒤 둘로 갈라 둘 다 있는 자리를 찾는다."""
        if tn.cjk_count(piece) < 2 or tn.cjk_count(piece) < len(tn.key(piece)) * 0.6:
            return None
        got = self.resolve(piece, allow_mt=False)
        if got.text and got.via in ("official", "pack", "user"):
            return [(got.text, got.via)]
        if len(piece) > LATIN_PIECE_MAX:
            return None
        for cut in range(2, len(piece) - 1):
            left, right = piece[:cut], piece[cut:]
            if not (self.known_text(left) and self.known_text(right)):
                continue
            a, b = self.resolve(left, allow_mt=False), self.resolve(right, allow_mt=False)
            if a.text and b.text and {a.via, b.via} <= {"official", "pack", "user"}:
                return [(a.text, a.via), (b.text, b.via)]
        return None

    def _english_caption(self, text: str) -> Resolved | None:
        """중국어 이름표 옆의 영문 장식 글자까지 한 줄로 읽은 것 ('档案ARCHIVES', '仓库 DEPOT'). 영문은 원래 화면에
        두는 글이라 떼고, 중국어만 확인된 번역(공식·묶음·내 용어)이 있을 때 그 자리만 덮는다."""
        m = _EN_CAPTION.fullmatch(text.strip())
        if not m:
            return None
        got = self.resolve(m.group("zh"), allow_mt=False)
        if not got.text or got.via not in ("official", "pack", "user"):
            return None
        tail = len(text) - text.find(m.group("zh")) - len(m.group("zh"))
        return replace(got, trim=(got.trim[0], got.trim[1] + tail))

    def _stage_line(self, text: str) -> Resolved | None:
        """스테이지 번호와 이름 ('国16·16示我以' → '16-16 진실의 계시'). 이름은 앞뒤를 놓쳤어도 채운다."""
        m = _STAGE_LINE.fullmatch(text.strip())
        if not m:
            return None
        name = m.group("name")
        hit = self._ui_hit(name)
        ko = tn.display_ko(hit.ko, self.cfg.get("nickname", "박사")) if hit else None
        if ko is None:
            whole = self._completed(name)
            ko = whole.text if whole else None
        if ko is None:
            return None
        self.stats["official"] += 1
        at = text.find(m.group("a"))
        return Resolved(f"{m.group('a')}-{m.group('b')} {ko}", "official", text, trim=(max(0, at), 0))

    def _label_value(self, text: str, allow_mt: bool) -> Resolved | None:
        """뒤에 숫자 값이 붙은 화면 글. 글만 번역하고 값은 그대로 잇는다 ('剩余15' → '잔여 15',
        '库存更新倒计时：25D 08：36：30' → '…: 25D 08:36:30'). 통째로 기계 번역하면 초마다 바뀌는 남은 시간을 매번
        새로 번역하고, '剩余15'·'剩余20' 이 '남은 수량 15'·'남은 20' 처럼 따로 놀았다.
        구분 기호(：)가 없으면 글이 게임 데이터의 화면 글일 때만 쓴다 ('…专精等级2' 같은 문장은 문장째 번역한다)."""
        m = _LABEL_VALUE.fullmatch(text.strip())
        if not m:
            return None
        label, sep = m.group("label").strip(), m.group("sep")
        value = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", m.group("value"))).strip(" .:")
        lk = tn.key(label)
        if len(lk) < 2 or not re.search(r"\d", value):
            return None
        nick = self.cfg.get("nickname", "박사")
        hit = self._ui_hit(label)
        if hit is not None:
            ko, via = tn.display_ko(hit.ko, nick), "official"
        elif not sep or len(lk) > LABEL_MAX:
            return None
        else:
            own, term = self.user.get(lk), self.glossary.base.get(label)
            packed, cached = self.pack.get(lk), self.cache.get(lk)
            if own:
                ko, via = own[0], "user"
            elif term:
                ko, via = term, "official"
            elif packed:
                ko, via = tn.display_ko(packed[0], nick), "pack"
            elif cached:
                ko, via = tn.strip_rich(cached[0]), "cache"
            else:
                if not worth_translating(label, self.charset):
                    return None
                if allow_mt:
                    self.enqueue(label, URGENT)
                return Resolved(None, "pending", label)
        self.stats[via] += 1
        ko = ko.rstrip(" :：")
        return Resolved(f"{ko}{':' if sep else ''} {value}", via, text)

    def _without_icons(self, text: str) -> Resolved | None:
        """앞 글자나 뒤 글자 하나를 떼면 게임 데이터에 번역이 있는 글자가 되는가.

        아이콘을 글자로 읽은 경우다: 공격 속도 칼 아이콘 '囝', 이름 앞 장식선 '一', 필터 아이콘 ≡ '三',
        사람 아이콘 '0'. 앞 글자는 한자가 아니거나, 선 모양이거나, 게임 글에 거의 안 나오는 한자('囝' 은 52만 글 중 1)일
        때만 뗀다. 흔한 글자를 떼면 '保密级'(OCR 이 '等' 을 놓침)이 '密级：■■' 가 된다.
        뒤 글자는 아이콘 모양 글자만 뗀다. 대사(스토리 줄)와는 맞추지 않는다.
        """
        core = text.strip()
        if len(core) < 3:
            return None
        head = core[0]
        icon_head = not ("㐀" <= head <= "鿿") or head in _ICON_TAIL or head in self.rare
        cands = []
        if icon_head:
            cands.append(core[1:])
        # 뒤 글자: 아이콘 모양 한자, 또는 한자 바로 뒤에 붙은 영문·기호 한 글자 (꼬리표 경계를 읽은 'i', ']')
        icon_tail = core[-1] in _ICON_TAIL or (not ("㐀" <= core[-1] <= "鿿") and "㐀" <= core[-2] <= "鿿")
        if icon_tail:
            cands.append(core[:-1])
            if icon_head and len(core) >= 4:
                cands.append(core[1:-1])
        for cand in cands:
            if len(tn.key(cand)) < 2 or not tn.has_cjk(cand):
                continue
            named = self.pack.name(tn.key(cand))
            if named:
                # 한국 서버에 없는 오퍼레이터 이름 앞의 장식선 ('一冻河' → 토스카)
                at = text.find(cand)
                self.stats["pack"] += 1
                return Resolved(named, "pack", cand, trim=(at, len(text) - at - len(cand)))
            hit = self.db.lookup(cand, fuzzy=False)
            if hit and hit.ko and hit.file_id < 0:
                at = text.find(cand)
                return replace(self._official(hit, cand), trim=(at, len(text) - at - len(cand)))
        return None

    def _completed(self, text: str) -> Resolved | None:
        """앞뒤 한두 글자를 놓친 짧은 화면 글자를 게임 데이터에서 채운다 ('标生命' → '目标生命值', 놓친 글자 (1, 1)).
        채울 수 있는 글이 하나뿐일 때만 쓴다. 놓친 글자가 적은 쪽부터 본다."""
        k = tn.key(text)
        if not (COMPLETE_MIN <= len(k) <= COMPLETE_MAX) or not all("㐀" <= ch <= "鿿" for ch in k):
            return None
        for extra in (1, 2):
            found = {ko: (cand_k, zh) for cand_k, zh, ko in self.db.completions(k, extra)}
            if len(found) > 1:
                return None
            if found:
                ko, (cand_k, zh) = next(iter(found.items()))
                at = cand_k.find(k)
                self.stats["official"] += 1
                return Resolved(tn.display_ko(ko, self.cfg.get("nickname", "박사")), "official", zh,
                                missed=(at, len(cand_k) - at - len(k)))
        return None

    def _within(self, text: str) -> Resolved | None:
        """문단이 잘려 앞이나 뒤가 빠진 채 읽힌 글을 공식·번역 묶음의 긴 글에서 찾는다 (읽은 글자가 그 글 안에 통째로 든다).
        2026-11-10 사용자: '문단 일부가 잘리면 통째로 기계번역으로 넘어감'. 후보의 번역이 하나뿐일 때만 쓴다.
        한국어 문장을 잘라 쓰지 않고 번역 전체를 보인다 (중·한 문장 수가 맞는 글은 35% 뿐이라 자르면 틀린다)."""
        k = tn.key(text)
        if len(k) < WITHIN_MIN or tn.cjk_count(k) < len(k) * 0.7:
            return None
        if k in self._within_cache:
            found = self._within_cache[k]
        else:
            found = self._find_within(k)
            if len(self._within_cache) >= WITHIN_CACHE:
                self._within_cache.pop(next(iter(self._within_cache)))
            self._within_cache[k] = found
        if found is None:
            return None
        ko, zh, via = found
        self.stats[via] += 1
        return Resolved(tn.display_ko(ko, self.cfg.get("nickname", "박사")), via, zh)

    def _new_index(self):
        from .index import PassageIndex

        index = PassageIndex(self.db, self.pack)
        threading.Thread(target=index.warm, name="index-warm", daemon=True).start()
        return index

    def _shadow_offer(self, text: str, rules: Resolved | None) -> None:
        """그림자 방식: 화면에는 옛 규칙의 결과를 그대로 쓰고, 같은 글을 색인으로도 맞춰 서로 다른 것만 기록한다 (뒤 스레드)."""
        k = tn.key(text)
        if k in self._shadow_seen or len(self._shadow_seen) > 5000:
            return
        self._shadow_seen.add(k)
        try:
            self._shadow_q.put_nowait((text, rules.text if rules else None, rules.via if rules else None))
        except queue.Full:
            self._shadow_seen.discard(k)

    def _shadow_worker(self) -> None:
        import json
        import os

        while not self._stop:
            text, ko, via = self._shadow_q.get()
            if self._stop:
                return
            try:
                found = self._index.cover(text) if self._index is not None else None
                other = found.ko if found else None
                if tn.key(other or "") == tn.key(ko or ""):
                    continue                                               # 같은 결과: 기록할 것이 없다
                if os.path.exists(self._shadow_path) and os.path.getsize(self._shadow_path) > 1024 * 1024:
                    continue                                               # 기록은 1MB 까지만
                row = {"zh": text, "rules": ko, "rules_via": via, "index": other, "index_via": found.via if found else None}
                with open(self._shadow_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            except Exception:                                           # 비교 때문에 번역이 멈추면 안 된다
                errlog.write("색인 비교 오류", True)

    def _indexed(self, text: str) -> Resolved | None:
        if self._index is None:
            self._index = self._new_index()
        found = self._index.cover(text)
        if found is None:
            return None
        self.stats[found.via] += 1
        return Resolved(found.ko, found.via, found.zh)

    def _by_sentences(self, text: str) -> Resolved | None:
        """공식 항목 여러 개가 한 블록으로 읽힌 긴 글을 문장 2~5개씩 묶어 가며 하나씩 공식(또는 번역 묶음) 번역으로 옮겨 잇는다.
        2026-11-10 배포판 캡처: 모듈 스토리 '荡平天下不平事。七月三十，多云。…' 는 일기 5편이 한 블록이라 한 문단에 들지 않아
        통째로 기계 번역이 됐다. 문장 하나라도 못 맞추면(OCR 오류) 쓰지 않고 기계 번역에 맡긴다."""
        k = tn.key(text)
        if len(k) < SENT_MIN or tn.cjk_count(k) < len(k) * 0.7:
            return None
        sents = [x for x in _SENT_SPLIT.findall(text) if tn.key(x)]
        if len(sents) < 2:
            return None
        if k in self._sent_cache:
            found = self._sent_cache[k]
        else:
            found = self._cover_sentences(sents)
            if len(self._sent_cache) >= WITHIN_CACHE:
                self._sent_cache.pop(next(iter(self._sent_cache)))
            self._sent_cache[k] = found
        if found is None:
            return None
        self.stats["official"] += 1
        return Resolved(" ".join(found), "official", text)

    def _cover_sentences(self, sents: list[str]) -> list[str] | None:
        nick = self.cfg.get("nickname", "박사")
        out: list[str] = []
        i = kept = 0
        while i < len(sents):
            for span in range(min(SENT_SPAN, len(sents) - i), 0, -1):
                piece = "".join(sents[i:i + span])
                pk = tn.key(piece)
                if len(pk) < 4:
                    continue
                hit = self.db.lookup(piece, fuzzy=False)
                ko = hit.ko if hit and hit.ko else None
                if ko is None:
                    packed = self.pack.get(pk)
                    ko = packed[0] if packed else None
                if ko:
                    out.append(tn.display_ko(ko, nick))
                    i += span
                    break
            else:
                # 못 맞춘 문장: OCR 이 숫자·기호를 섞어 읽었으면 비슷한 공식 문장으로 맞춘다. 그래도 안 되고 짧은 제목 한 줄이면
                # 원문 그대로 둔다 (모듈 스토리 맨 앞 '荡平天下不平事。' 는 공식 항목이 아니다). 그 밖에는 쓰지 않는다
                near = None
                for span in range(min(3, len(sents) - i), 0, -1):
                    piece = "".join(sents[i:i + span])
                    hit = self.db.lookup(piece, fuzzy=True) if len(tn.key(piece)) >= 12 else None
                    if hit and hit.ko and hit.via in ("fuzzy", "exact"):
                        near = (hit.ko, span)
                        break
                if near:
                    out.append(tn.display_ko(near[0], nick))
                    i += near[1]
                elif len(tn.key(sents[i])) <= 12 and not kept and i == 0:
                    out.append(sents[i].strip())
                    kept += 1
                    i += 1
                else:
                    return None
        return out

    def _template_split(self, text: str) -> Resolved | None:
        """숫자 틀 두 줄이 한 블록으로 읽힌 것: 첫 줄은 통째로, 둘째 줄은 앞부분만 보인다.
        2026-11-10 배포판 캡처: '【叙拉古】干员部署后攻击速度+46，持续42秒（受层数影响) <在场6名不同的【叙拉古】干员' 는 묶음에 첫 줄
        틀과 둘째 줄 틀이 따로 있는데 한 틀에 안 들어 기계 번역으로 갔다. 끊을 만한 자리(문장 부호·괄호 뒤)마다 앞은 틀 한 줄로,
        뒤는 틀의 일부로 맞춰 본다."""
        tk = tn.template_key(text)
        if len(tk) < WITHIN_MIN * 2 or tn.SLOT not in tk:
            return None
        for i, ch in enumerate(text):
            if ch not in "，。；：）)>＞>」” " or i < 8:
                continue
            head, tail = text[:i + 1], text[i + 1:].strip()
            if len(tn.template_key(head)) < WITHIN_MIN or len(tn.template_key(tail)) < 8:
                continue
            first = self._pack_template(head)
            if first is None or not first.text:
                continue
            rest = self._pack_template(tail) or self._template_within(tail, ratio=0.08)    # 끊어 낸 뒷부분은 틀의 아주 일부일 수 있다
            if rest is not None and rest.text:
                return Resolved(f"{first.text} {rest.text}", first.via, text)
        return None

    def _template_within(self, text: str, ratio: float = WITHIN_TPL_RATIO) -> Resolved | None:
        """줄이 잘려 숫자 틀의 일부만 읽힌 글(위수협의 스킬 설명 '…攻击速度+100，每次攻击对目标…最多5名地面敌人造成相当于')을
        틀에서 찾아, 보이는 숫자만 화면 값으로 채운다. 안 보이는 자리는 '…'. 틀이 하나로 좁혀질 때만 쓴다."""
        tk = tn.template_key(text)
        values = tn.numbers_of(tn.strip_rich(text))
        if len(tk) < WITHIN_MIN or tn.SLOT not in tk or tn.cjk_count(tk) < 8 or len(values) != tk.count(tn.SLOT):
            return None
        for head, tail in WITHIN_CUTS:
            part = tk[head:len(tk) - tail]
            if len(part) < WITHIN_MIN or part.count(tn.SLOT) != tk.count(tn.SLOT):
                continue
            found: dict[str, tuple[str, str]] = {}
            pairs = [(zh, ko, "official") for zh, ko in self.db.templates_containing(part, ratio) if ko]
            pairs += [(zh, ko, "pack") for zh, ko in self.pack.templates_containing(part, ratio)]
            for zh, ko, via in pairs:
                ck = tn.template_key(zh)
                at = ck.find(part)
                if at < 0:
                    continue
                first = ck[:at].count(tn.SLOT)
                # 보이는 절만 잘라 쓴다. 절 수가 안 맞으면 번역 전체를 쓰되 보이는 부분이 절반 가까이는 되어야 한다
                sliced = tn.clause_slice(zh, ko, part, first)
                if sliced is not None:
                    zh, ko, first = sliced
                elif len(part) < len(ck) * WITHIN_RATIO:
                    continue
                filled = tn.fill_template(zh, ko, text, first)
                if filled:
                    found.setdefault(filled, (zh, via))
            if len(found) == 1:
                filled, (zh, via) = next(iter(found.items()))
                self.stats[via] += 1
                return Resolved(tn.display_ko(filled, self.cfg.get("nickname", "박사")), via, zh)
            if found:
                return None
        return None

    def _find_within(self, k: str) -> tuple[str, str, str] | None:
        for head, tail in WITHIN_CUTS:
            part = k[head:len(k) - tail]
            if len(part) < WITHIN_MIN:
                break
            found: dict[str, tuple[str, str]] = {}
            for _ck, zh, ko, _src in self.db.containing(part, WITHIN_RATIO):
                found.setdefault(ko, (zh, "official"))
            for ck, ko, _cat in self.pack.containing(part, WITHIN_RATIO):
                found.setdefault(ko, (ck, "pack"))
            if len(found) == 1:
                ko, (zh, via) = next(iter(found.items()))
                return ko, zh, via
            if found:
                return None     # 후보가 여럿이면 어느 글인지 모른다
        return None

    def _ui_hit(self, text: str):
        """화면 글(대사 아님)로 공식 번역이 있는 글자면 그 Hit."""
        hit = self.db.lookup(text, fuzzy=False)
        return hit if hit and hit.ko and hit.file_id < 0 and hit.src != "story" else None

    def _bracket_tail(self, text: str) -> Resolved | None:
        """'제목（괄호）' 의 두 쪽이 저마다 화면 글이면 번역을 이어 붙인다 ('职业需求（最多3项）' → '모집 요건 (최대 3항)').
        모집 화면의 '职业需求' 와 그 아래 줄 '(最多3项)' 을 OCR 이 한 덩어리로 읽었다."""
        m = _BRACKET_TAIL.fullmatch(text.strip())
        if not m:
            return None
        head, tail = self._ui_hit(m.group(1)), self._ui_hit(m.group(2))
        if head is None or tail is None:
            return None
        self.stats["official"] += 1
        nick = self.cfg.get("nickname", "박사")
        return Resolved(f"{tn.display_ko(head.ko, nick)} {tn.display_ko(tail.ko, nick)}", "official", text)

    def _noisy_prefix(self, text: str) -> Resolved | None:
        """앞에 숫자·기호(와 헛것 한자 하나)가 붙은 화면 글 ('27：弓1：22联络中' → '联络中' 서칭 중). 붙은 앞부분은 상자에서 뺀다."""
        for i in range(1, len(text) - 1):
            head, tail = text[:i], text[i:]
            if sum("㐀" <= ch <= "鿿" for ch in head) > 1 or not re.search(r"[\d\W]", head) or len(tn.key(tail)) < 2:
                continue
            hit = self._ui_hit(tail)
            if hit is not None:
                found = self._official(hit, tail)
                # 숫자 뒤에 세는 말이 오면 숫자도 글이다 ('19个干员' → '19명 인원'. 공식 번역 '명 인원' 이 숫자 뒤에 붙는 꼴)
                count = re.search(r"\d+$", head)
                if count and tail[0] in _COUNTERS and found.text and not found.text[0].isdigit():
                    return replace(found, text=count.group() + found.text, zh=text[count.start():],
                                   trim=(count.start(), 0))
                return replace(found, trim=(i, 0))
        return None

    def _guess_name(self, text: str, masked: str, pick) -> Resolved | None:
        found, holes = self.names.candidates(text, masked)
        head = 0
        if not found and len(text) >= 4 and text[0] in _ICON_TAIL:
            # 이름 앞 장식선을 '一' 로 읽은 것 ('一罗比塔')
            found, holes = self.names.candidates(text[1:], masked[1:])
            head = 1
        if not found:
            return None
        if holes:
            # 갈라 읽은 글자는 화면 모양으로 확인한다. 후보가 하나여도 모양이 다르면 쓰지 않는다
            if pick is None:
                return None
            found = [pick(found, holes)]
            if found[0] is None:
                return None
        elif len({self.names.ko(k) for k in found}) > 1:
            return None
        self.stats["official"] += 1
        return Resolved(self.names.ko(found[0]), "official", self.names.zh(found[0]),
                        missed=self.names.missed(text[head:], found[0]), trim=(head, 0))

    def _after_story_hit(self, hit) -> None:
        """공식 번역이 없는 스토리를 읽는 중이면 다음 대사들을 미리 번역해 둔다."""
        if hit is None or hit.file_id < 0:
            return
        # Gemini 가 이 스토리를 통째로 맡으면 일반 번역기로 미리 번역하지 않는다 (한도 절약)
        if self.story_ai is not None and self.story_ai.request(hit.file_id):
            return
        for line in self.db.upcoming(LOOKAHEAD_LINES):
            if not self.cache.get(tn.key(line)) and not self.pack.get(tn.key(line)):
                self.enqueue(line, BACKGROUND)

    # -------------------------------------------------------------- 기계 번역
    def enqueue(self, zh: str, priority: int) -> None:
        k = tn.key(zh)
        now = time.time()
        with self._lock:
            if k in self._inflight or now - self._failed_at.get(k, 0.0) < RETRY_AFTER:
                return
            self._inflight.add(k)
            self._seq += 1
            self._queue.put((priority, self._seq, zh))

    def _worker(self) -> None:
        while not self._stop:
            priority, _seq, zh = self._queue.get()
            if self._stop or priority < 0:
                return
            k = tn.key(zh)
            try:
                with self._lock:
                    wait = self._last_call + MIN_INTERVAL - time.time()
                    self._last_call = max(time.time(), self._last_call + MIN_INTERVAL)
                if wait > 0:
                    time.sleep(wait)
                ko, label = self.translate(zh)
                self.cache.put(zh, with_icon(zh, ko), label)
                self.stats["mt"] += 1
                self.on_ready()
            except mt_mod.QuotaExhausted:
                # 사용자가 고를 때까지 기다린다. 30초 뒤에 다시 물어보되 매번 알리지는 않는다
                self._failed_at[k] = time.time()
            except Exception as exc:
                errlog.write("기계 번역 실패", True, note=f"원문: {zh[:80]}")
                self.stats["failed"] += 1
                self._failed_at[k] = time.time()
                self.log(f"번역 실패: {exc}")
            finally:
                with self._lock:
                    self._inflight.discard(k)

    def translate(self, zh: str) -> tuple[str, str]:
        """(번역문, 실제로 번역한 번역기). 막혀서 구글로 넘어갔으면 캐시에도 구글로 적힌다."""
        engine = self.engine
        masked, mapping = self.glossary.protect(zh, self.db.story_speakers())
        if not mapping:
            return engine.run(zh)
        out, label = engine.run(masked)
        restored = self.glossary.restore(out, mapping)
        # 번역기가 토큰을 망가뜨렸으면 용어 없이라도 번역문을 보여준다
        return (restored, label) if restored is not None else engine.run(zh)

    def translate_many(self, zhs: list[str], speakers: set[str]) -> list[tuple[str, str]]:
        """미리 번역용. speakers 는 그 스토리의 화자 (지금 읽는 스토리가 아니다)."""
        return translate_batch(self.glossary, self.engine, zhs, speakers)


def translate_batch(glossary, engine, zhs: list[str],
                    speakers: set[str] | list[set[str]]) -> list[tuple[str, str]]:
    """여러 줄을 공식 용어를 지키며 번역한다. 미리 번역과 번역 묶음 만들기(tools/build_pack.py)가 같이 쓴다.
    speakers 는 이름으로 지킬 두 글자 이름: 모든 줄에 같은 집합이거나 줄마다 하나씩."""
    per_line = speakers if isinstance(speakers, list) else [speakers] * len(zhs)
    protected = [glossary.protect(zh, names) for zh, names in zip(zhs, per_line)]
    out = []
    for zh, (_masked, mapping), (ko, label) in zip(zhs, protected, engine.run_many([m for m, _ in protected])):
        restored = glossary.restore(ko, mapping) if mapping else ko
        # 번역기가 토큰을 망가뜨렸으면 용어 없이라도 번역한다
        out.append((restored, label) if restored is not None else engine.run(zh))
    return out


def _affix_trim(text: str, zh: str) -> tuple[int, int]:
    """화면 글자 text 가 맞춘 원문 zh 앞이나 뒤에 글자가 더 붙은 꼴이면 (앞, 뒤) 더 붙은 글자 수."""
    if zh and text != zh and len(text) > len(zh):
        if text.endswith(zh):
            return (len(text) - len(zh), 0)
        if text.startswith(zh):
            return (0, len(text) - len(zh))
    return (0, 0)


def _countdown(text: str) -> str | None:
    """'8小时' → '8시간'. 숫자와 시간 단위뿐인 글만."""
    m = _COUNTDOWN_ONLY.fullmatch(text.strip())
    return f"{m.group(1)}{_UNITS[m.group(2)]}" if m else None


def with_icon(src: str, ko: str) -> str:
    """화면 글의 화폐 아이콘('▲10')이 번역문에서 빠졌으면 같은 숫자 앞에 되살린다 (기계 번역이 기호를 버린다)."""
    for m in _ICON_READ.finditer(src):
        number = re.match(r"\s*(\d+)", src[m.end():]).group(1)
        if ICON + number in ko.replace("△", ICON).replace(" ", ""):
            continue
        ko = re.sub(rf"(?:[△▲]\s*)?(?<![\d.])({number})(?![\d.])", lambda n: ICON + n.group(1), ko, count=1)
    return ko.replace("△", ICON)


def _usable(hit, k: str):
    """한 글자는 대사와 맞추지 않는다 ('收藏品' 을 '收。' 로 반만 읽었더니 대사 '收……' 의 '수……' 가 떴다)."""
    if hit is not None and len(k) == 1 and (hit.file_id >= 0 or hit.src == "story"):
        return None
    return hit


def _in_story(hit) -> bool:
    return hit is not None and hit.file_id >= 0


def _clean(zh: str) -> str:
    zh = re.sub(r"(?:Dr\.?)?\{@nickname\}", "博士", tn.strip_rich(zh))
    return re.sub(r"\s+", " ", zh.replace("\\n", " ")).strip()
