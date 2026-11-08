"""번역 묶음: 공식 번역이 없는 게임 글의 번역을 배포자가 미리 만들어 나눠 주는 것.

사용자가 번역기 키를 받지 않아도 대부분의 화면이 바로 번역되게 한다. 프로그램은 화면 글자를 게임 데이터
원문에 맞춰 찾으므로, 원문 → 번역문 표만 있으면 된다.

  pack/manifest.json   버전, 만든 게임 데이터 커밋, 파일마다 줄 수와 sha256
  pack/<분류>.tsv      한 줄에 '원문<TAB>번역문'. '#' 로 시작하는 줄은 설명

TSV 는 사람이 읽고 고쳐 보낼(Pull Request) 수 있게 원문 순서로 정렬해 둔다. 탭·줄바꿈·역슬래시는 \\t \\n \\r \\\\ 로,
원문이 '#' 로 시작하면 \\# 으로 적는다.
찾는 순서는 resolver 가 정한다: 내 용어집 → 공식 번역 → 번역 묶음 → 번역캐시 → 기계 번역.
"""
from __future__ import annotations

import collections
import difflib
import hashlib
import json
import os
import re
import threading
import urllib.parse
import urllib.request

from . import textnorm as tn

FORMAT = 1
MANIFEST = "manifest.json"
NAMES = "names"         # 오퍼레이터 이름 분류 (names.tsv)
USER_AGENT = "ArknightsTranslatorOverlay/1.0 (translation pack)"
ISSUE_TEMPLATE = "mistranslation.yml"   # 번역 저장소의 오역 제보 양식
REPORT_MAX = 1500                        # 칸마다 넣을 글자 수 상한 (주소가 너무 길면 브라우저가 못 연다)
FUZZY_MIN = 20          # 이보다 짧은 숫자 틀은 비슷한 것으로 고르지 않는다 (글자 하나 차이로 뜻이 바뀐다)
FUZZY_RATIO = 0.9       # 화면 글자와 틀 원문이 이만큼 같아야 한다 (75자 설명에서 몇 자 놓친 정도)
FUZZY_MARGIN = 0.01     # 둘째로 비슷한 틀과 이만큼은 차이 나야 한다


def _slots_align(sm: difflib.SequenceMatcher) -> bool:
    """sm 이 견준 두 틀 키(sm.a, sm.b)의 숫자 자리(#)가 하나하나 같은 자리에 맞서는가. 개수만 같고 자리가 밀리면 숫자가
    엉뚱한 칸에 들어간다: OCR 이 '攻击力+60%' 를 '攻击力1+60%' 로 읽고 끝의 '2秒' 를 놓쳐 '공격력 +1 … 재능 효과 60%배'
    가 됐다 (2026-11-08)."""
    tk, cand = sm.a, sm.b
    matched = sum(tk[i:i + n].count(tn.SLOT) for i, _j, n in sm.get_matching_blocks())
    return matched == tk.count(tn.SLOT) == cand.count(tn.SLOT)


def escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace("\t", "\\t")
            .replace("\n", "\\n").replace("\r", "\\r"))


def unescape(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            out.append({"t": "\t", "n": "\n", "r": "\r"}.get(text[i + 1], text[i + 1]))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def read_tsv(path: str) -> list[tuple[str, str]]:
    rows = []
    # 줄은 \n 으로만 나눈다. 예전 묶음에 날것으로 들어간 \r 이 줄을 끊지 않게
    with open(path, encoding="utf-8-sig", newline="\n") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line or line.startswith("#") or "\t" not in line:
                continue
            zh, ko = line.split("\t", 1)
            rows.append((unescape(zh), unescape(ko)))
    return rows


def write_tsv(path: str, rows, title: str) -> None:
    """원문 키 순서로 정렬해 쓴다. 다음 판과 견줄 때 바뀐 줄만 보이게."""
    ordered = sorted(rows, key=lambda r: tn.key(r[0]))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"# {title}\n# 원문\t번역문\n")
        for zh, ko in ordered:
            key = escape(zh)
            if key.startswith("#"):         # 설명 줄로 읽히지 않게
                key = "\\" + key
            f.write(f"{key}\t{escape(ko)}\n")
    os.replace(tmp, path)


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _gram_index(templates: dict) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for tk in templates:
        if len(tk) >= FUZZY_MIN:
            for g in {tk[i:i + 3] for i in range(len(tk) - 2)}:
                index.setdefault(g, []).append(tk)
    return index


class PackStore:
    """받아 둔 번역 묶음을 메모리 표로 올려 둔다."""

    def __init__(self, folder: str):
        self.folder = folder
        self._lock = threading.Lock()
        self._table: dict[str, tuple[str, str]] = {}    # 원문 키 -> (번역문, 분류)
        # 숫자 틀 (원문에 '{…}' 자리표시자가 있는 줄): 숫자를 '#' 으로 바꾼 키 -> (원문 틀, 번역 틀)
        self._templates: dict[str, tuple[str, str]] = {}
        # 오퍼레이터 이름 (names.tsv, 배포자가 손으로 정한 것): 원문 키 -> 이름. 같은 글자의 대사 줄('嘉辛塔？' → '자신타?')보다 먼저 쓴다
        self._names: dict[str, str] = {}
        self._grams: dict[str, list[str]] = {}          # 틀 키의 세 글자 조각 -> 틀 키 (비슷한 틀 찾기)
        self.manifest: dict = {}
        self.load()

    def load(self) -> None:
        table, templates, names, manifest = {}, {}, {}, {}
        path = os.path.join(self.folder, MANIFEST)
        try:
            with open(path, encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            pass
        for name in (manifest.get("files") or {}):
            file_path = os.path.join(self.folder, name)
            if not os.path.exists(file_path):
                continue
            category = os.path.splitext(name)[0]
            for zh, ko in read_tsv(file_path):
                k = tn.key(zh)
                if k and ko and category == NAMES:
                    names[k] = ko
                    continue
                if k and ko:
                    table.setdefault(k, (ko, category))
                    if "{" in zh:
                        tk = tn.template_key(zh)
                        if tn.SLOT in tk:
                            templates.setdefault(tk, (zh, ko))
        with self._lock:
            self._table, self._templates, self._names, self.manifest = table, templates, names, manifest
            self._grams = _gram_index(templates)

    def get(self, k: str) -> tuple[str, str] | None:
        return self._table.get(k)

    def name(self, k: str) -> str | None:
        """한국 서버에 없는 오퍼레이터의 이름 (영문 이름을 한국 서버 표기법으로 옮긴 것)."""
        return self._names.get(k)

    def template(self, tk: str) -> tuple[str, str] | None:
        """(원문 틀, 번역 틀). tk 는 textnorm.template_key."""
        return self._templates.get(tk)

    def similar_template(self, tk: str) -> tuple[str, str] | None:
        """OCR 이 몇 글자 놓친 긴 숫자 틀 ('…飞行敌人缚地22秒…' 을 '…飞敌缚地22秒…' 로 읽었다).
        PaddleOCR 은 밑줄 친 낱말이 든 줄에서 같은 글자를 자르는 폭에 따라 빠뜨리기도 하고 안 빠뜨리기도 했다.
        숫자 자리 수가 같고 거의 같은 틀이 하나뿐일 때만 준다."""
        if len(tk) < FUZZY_MIN:
            return None
        grams = {tk[i:i + 3] for i in range(len(tk) - 2)}
        votes = collections.Counter()
        index = self._grams
        for g in grams:
            votes.update(index.get(g, ()))
        best, best_score, second = None, 0.0, 0.0
        best_sm = None
        slots = tk.count(tn.SLOT)
        for cand, n in votes.most_common(8):
            if n < len(grams) * 0.5:
                break
            if cand.count(tn.SLOT) != slots or not 0.8 <= len(cand) / len(tk) <= 1.25:
                continue
            sm = difflib.SequenceMatcher(None, tk, cand, autojunk=False)
            score = sm.ratio()
            if score > best_score:
                best, best_score, second, best_sm = cand, score, best_score, sm
            elif score > second:
                second = score
        if best is None or best_score < FUZZY_RATIO or best_score - second < FUZZY_MARGIN:
            return None
        # ratio() 가 이미 구해 둔 맞는 조각을 그대로 쓴다
        if not _slots_align(best_sm):
            return None
        return self._templates.get(best)

    def __len__(self) -> int:
        return len(self._table)

    @property
    def version(self) -> str:
        return str(self.manifest.get("version") or "")


def report_url(base_url: str, fields: dict[str, str]) -> str | None:
    """번역 저장소의 '오역 제보' 이슈 쓰기 화면 주소 (칸을 미리 채운다). 묶음 주소가 GitHub 이 아니면 None.

    묶음 주소는 'https://raw.githubusercontent.com/<계정>/<저장소>/<가지>/' 꼴이다. fields 의 이름은 번역 저장소의
    .github/ISSUE_TEMPLATE/mistranslation.yml 칸 id 와 같다 (zh, ko, source, version). 제출은 사용자가 브라우저에서 한다.
    """
    m = re.match(r"https?://(?:raw\.githubusercontent\.com|github\.com)/([\w.-]+)/([\w.-]+)", (base_url or "").strip())
    if not m:
        return None
    owner, repo = m.group(1), m.group(2).removesuffix(".git")
    query = {"template": ISSUE_TEMPLATE, "title": "[오역] " + fields.get("zh", "")[:30]}
    query.update({k: v[:REPORT_MAX] for k, v in fields.items() if v})
    return f"https://github.com/{owner}/{repo}/issues/new?" + urllib.parse.urlencode(query)


def remote_version(base_url: str, timeout: float = 10.0) -> str:
    """배포처 묶음의 버전. 새 판이 있는지 알리는 데만 쓴다."""
    data = json.loads(_get(base_url.rstrip("/") + "/" + MANIFEST, timeout).decode("utf-8"))
    return str(data.get("version") or "")


def fetch(base_url: str, folder: str, timeout: float = 30.0) -> tuple[str, list[str]]:
    """배포처에서 새 번역 묶음을 받는다. (새 버전, 바뀐 파일들). 이미 최신이면 바뀐 파일이 없다.

    manifest 를 먼저 받아 sha256 이 다른 파일만 받고, 받은 파일의 sha256 을 확인한 뒤 바꿔 끼운다.
    하나라도 어긋나면 아무것도 바꾸지 않는다.
    """
    base = base_url.rstrip("/") + "/"
    remote = json.loads(_get(base + MANIFEST, timeout).decode("utf-8"))
    if int(remote.get("format", 0)) > FORMAT:
        raise ValueError("이 프로그램보다 새 형식의 번역 묶음입니다. 프로그램을 업데이트하세요.")
    os.makedirs(folder, exist_ok=True)
    local = {}
    try:
        with open(os.path.join(folder, MANIFEST), encoding="utf-8") as f:
            local = json.load(f).get("files") or {}
    except (OSError, ValueError):
        pass
    staged = []
    for name, info in (remote.get("files") or {}).items():
        if os.path.basename(name) != name or not name.endswith(".tsv"):
            continue    # 다른 폴더로 쓰게 하는 이름은 받지 않는다
        target = os.path.join(folder, name)
        if (local.get(name) or {}).get("sha256") == info.get("sha256") and os.path.exists(target):
            continue
        data = _get(base + name, timeout)
        if hashlib.sha256(data).hexdigest() != info.get("sha256"):
            raise ValueError(f"{name} 의 내용이 목록과 다릅니다 (받는 도중 바뀌었거나 깨졌습니다).")
        staged.append((target, data))
    for target, data in staged:
        with open(target + ".tmp", "wb") as f:
            f.write(data)
    for target, _data in staged:
        os.replace(target + ".tmp", target)
    with open(os.path.join(folder, MANIFEST + ".tmp"), "w", encoding="utf-8") as f:
        json.dump(remote, f, ensure_ascii=False, indent=2)
    os.replace(os.path.join(folder, MANIFEST + ".tmp"), os.path.join(folder, MANIFEST))
    return str(remote.get("version") or ""), [os.path.basename(t) for t, _ in staged]


def _get(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.read()
