"""번역 묶음 만들기 (배포자용). 사용자는 이것을 쓰지 않고, 만든 묶음을 프로그램이 받아 쓴다.

  1. data/official.sqlite 에서 공식 번역이 없는 게임 글을 분류별로 모은다
  2. 지난 묶음에 없는 글만 기계 번역한다 (공식 용어는 지킨다)
  3. 분류별 TSV 와 manifest.json 을 쓴다. --publish 면 묶음 폴더(git 저장소)에 커밋하고 올린다

  python -I tools/build_pack.py --out ..\\ak-translations --dry-run          # 얼마나 번역할지만 본다
  set DEEPL_KEY=...  &&  python -I tools/build_pack.py --out ..\\ak-translations --engine deepl
  python -I tools/build_pack.py --out ..\\ak-translations --only rogue --limit 50   # 일부만 시험

번역기 키는 환경 변수(DEEPL_KEY, PAPAGO_ID, PAPAGO_SECRET)로만 받는다. 묶음 폴더에는 키를 쓰지 않는다.
스토리 줄은 기본으로 넣지 않는다 (--with-story). 한국 서버에 아직 없는 스토리 전문을 공개 저장소에
올리는 일이 되기 때문이다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)    # -I 로 실행하면 스크립트 폴더가 경로에 없다
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ak_translator import mt, pack, pretranslate, textnorm as tn  # noqa: E402
from ak_translator.glossary import Glossary  # noqa: E402
from ak_translator.memory import GLOSSARY_HEADER, MarkdownStore, read_table  # noqa: E402
from ak_translator.names import load_names  # noqa: E402
from ak_translator.official import OfficialDB  # noqa: E402
from ak_translator.resolver import _clean, translate_batch  # noqa: E402

# 분류 이름 → 설명 (TSV 첫 줄과 manifest 에 들어간다)
CATEGORIES = {
    "operators": "오퍼레이터: 프로필·파일 자료·음성·모듈",
    "rogue": "통합전략: 유물·조우·선택지 설명",
    "sandbox": "생명연산",
    "events": "이벤트 화면·작전 설명·적 도감",
    "ui": "그 밖의 화면 글 (메달·상점·기반시설 등)",
    "story": "스토리 (tools/story_pack.py: DeepL 초벌 → Gemini 인물 말투 맞춤)",
    "names": "한국 서버에 없는 오퍼레이터 이름: 영문 이름을 한국 서버 표기법으로 (배포자가 손으로 정한다. 도구는 번역하지 않는다)",
    "templates": "숫자가 바뀌는 설명: 스킬·재능·모듈·기반시설·유물. {…} 는 화면 숫자가 들어갈 자리라 지우거나 고치지 말 것",
}
SAVE_EVERY = 20     # 이만큼 요청마다 중간 저장 (도중에 멈춰도 쓴 한도를 잃지 않게)
# 한도가 모자랄 때 먼저 채울 분류. 통합전략·오퍼레이터처럼 자주 보는 화면이 먼저, 메달·상점 같은 ui 는 나중
MANUAL = {"names"}  # 손으로 정하는 분류. 도구가 번역하지도 비우지도 않는다
PRIORITY = ["templates", "rogue", "operators", "events", "sandbox", "ui", "story"]
# 숫자 틀의 자리표시자('{atk_scale:0%}')를 번역기에 보낼 때 바꿔 둘 숫자. 번역기는 숫자를 지키고 말 순서에 맞게
# 옮겨 놓는다 (라틴 토큰보다 자리가 자연스럽다). 돌아온 번역문에서 다시 자리표시자로 바꾼다
_SLOT = re.compile(r"\{(?!@)[^{}]+\}")
MARK_BASE = 9001
RETRY_WAIT = 30.0   # 번역기가 실패하면 이만큼 쉬었다가 다시 보낸다
MAX_FAILS = 3
# 두 글자 이름은 보통 말이기도 해서 ('真理' 진리 / 이스티나, '跃跃欲试') 화면 번역은 스토리 화자일 때만 지킨다.
# 묶음에는 화자가 없으니, 공식 번역에서 이 비율 이상 이름으로 옮겨진 이름과 공식 글에 거의 안 나오는 이름은 늘 지킨다
NAME_RATE = 0.8
NAME_RARE = 5
# '干员X', '干员X、Y' 의 X·Y 는 보통 말이어도 이름이다 ('解锁干员暴雨的…', '拥有干员折光、跃跃')
_OPERATOR = re.compile(r"干员([^，。；：！？的并]+)")
README = """# 명일방주 번역 오버레이 — 번역 묶음

공식 한국어 번역이 아직 없는 중국 서버 글을 미리 번역해 둔 묶음입니다. 번역 오버레이 프로그램이 받아 씁니다.
기계 번역(DeepL)에 공식 용어를 지키게 하고, 짧은 글과 스토리는 Gemini 로 한국 서버 공식 번역 말투에 맞췄습니다.
공식 번역이 아니며, 한국 서버에 공식 번역이 나오면 프로그램은 공식 번역을 먼저 씁니다.

## 오역 제보

- 프로그램의 **최근 번역** 탭에서 줄을 고르고 **오역 제보 ↗** 를 누르면 원문·번역이 채워진 제보 화면이 열립니다.
- 직접 쓸 때는 Issues → New issue → '오역 제보'.
- 내 PC 에서만 바로 고치려면 프로그램의 `data\\용어집.md` 에 `| 원문 | 번역 |` 으로 적으세요 (번역 묶음보다 먼저 쓰입니다).

## 고치기 (배포자·기여자)

- 분류마다 `<분류>.tsv` 한 파일, 한 줄에 `원문<TAB>번역문`. 탭·줄바꿈은 `\\t` `\\n` 으로,
  `#` 로 시작하는 원문은 `\\#` 으로 적습니다 (`#` 로 시작하는 줄은 설명).
- **번역문만** 고칩니다. 원문은 게임 글과 맞춰 찾는 열쇠라 고치면 그 줄이 쓰이지 않습니다.
- GitHub 웹에서 고치거나 Pull Request 를 합치면 GitHub Action 이 `manifest.json` 을 자동으로 고쳐 씁니다.
  로컬에서 고쳤으면 올리기 전에 `python tools/refresh_manifest.py .` 를 돌리세요. manifest 를 안 고치면 이미 받은
  사람에게는 고친 내용이 가지 않고, 새로 받는 사람은 sha256 이 맞지 않아 받기가 거부됩니다.
- 고친 줄은 묶음을 다시 만들어도 그대로 남습니다 (도구는 이미 있는 줄을 다시 번역하지 않습니다).
"""
# 번역 저장소에 넣어 두는 파일 (제보 양식, manifest 자동 갱신). 묶음 폴더에 없을 때만 복사한다
PACK_REPO_FILES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pack_repo")


def collect(db, with_story: bool) -> dict[str, list[str]]:
    """분류별 번역할 원문 (중복 없이, 먼저 든 분류에만)."""
    catalog = pretranslate.load(db)
    groups = {
        "operators": [g for g in catalog.operators if g.key != pretranslate.FRESH],
        "rogue": [catalog.rogue],
        "sandbox": [catalog.sandbox],
        "events": catalog.events,
    }
    seen: set[str] = set()
    out: dict[str, list[str]] = {name: [] for name in CATEGORIES}

    def put(category: str, zh: str) -> None:
        k = tn.key(zh)
        if k and k not in seen and tn.has_cjk(zh):
            seen.add(k)
            out[category].append(zh)

    for category, gs in groups.items():
        for g in gs:
            for zh, file_id in g.items:
                if file_id >= 0:
                    if with_story:
                        put("story", zh)
                    continue
                put(category, zh)
    for zh in db.untranslated_untagged():
        put("ui", tn.strip_rich(zh).strip())
    # 한국 서버에 없는 숫자 틀 (스킬·재능·모듈 설명 등). 화면에서는 resolver._pack_template 이 숫자를 채운다
    for zh, _src in db.untranslated_templates():
        for part in template_segments(zh):
            put("templates", part)
    return {name: items for name, items in out.items() if items}


_QUOTED = re.compile(r"“([^“”]{2,8})”")


def fix_quoted_terms(glossary, engine, items: list[str]) -> None:
    """숫자 틀은 게임 글의 줄마다 따로 번역해서, 한 스킬의 '“簇火”' 가 줄마다 '불꽃 덩어리'·'화염 군집' 으로 갈렸다.
    따옴표 안 용어를 먼저 한 번씩만 번역해 용어집에 넣어 두면, 줄 번역에서 그 용어를 그대로 쓴다."""
    terms = sorted({m for zh in items for m in _QUOTED.findall(zh)
                    if tn.has_cjk(m) and m not in glossary.base})
    if not terms:
        return
    results = translate_batch(glossary, engine, terms, [set()] * len(terms))
    pairs = [(zh, ko.strip().strip("“”\"'‘’")) for zh, (ko, _label) in zip(terms, results)
             if ko and usable_translation(ko)]
    glossary.add_terms(pairs)
    print(f"따옴표 용어 {len(pairs):,}개를 먼저 번역해 용어로 지킵니다 (예: " +
          ", ".join(f"{zh}={ko}" for zh, ko in pairs[:3]) + ")")


def template_segments(zh: str) -> list[str]:
    """숫자 틀을 게임 글의 줄마다 나눈다. 스킬 설명 '…（可叠加5层）' 과 그다음 줄 '受到攻击后触发，可充能{…}次' 는
    화면에서 글자색이 달라 따로 묶이므로, 줄마다 따로 번역해 둬야 화면 글과 맞는다. 게임 글의 줄바꿈은 역슬래시+n
    두 글자로 적혀 있기도 하고 진짜 줄바꿈이기도 하다."""
    text = re.sub(r"(?:Dr\.?)?\{@nickname\}", "博士", tn.strip_rich(zh))
    parts = (re.sub(r"\s+", " ", part).strip() for part in re.split(r"\\n|\n", text))
    return [part for part in parts if tn.cjk_count(part) >= 2]


def mark_slots(zh: str) -> tuple[str, dict[str, str]]:
    """'{atk_scale:0%}' 같은 자리표시자를 9001% 같은 숫자로 바꾼 글과 {숫자: 자리표시자}."""
    marks: dict[str, str] = {}

    def sub(m):
        number = str(MARK_BASE + len(marks))
        marks[number] = m.group(0)
        return number + ("%" if "%" in m.group(0) else "")

    return _SLOT.sub(sub, zh), marks


def unmark_slots(ko: str, marks: dict[str, str]) -> str | None:
    """번역문의 숫자를 자리표시자로 되돌린다. 숫자 하나라도 없어졌거나 둘이 되었으면 None (그 줄은 버린다)."""
    out = ko
    for number, slot in marks.items():
        pattern = re.compile(number + (r"\s*%" if "%" in slot else "") + r"(?!\d)")
        if len(pattern.findall(out)) != 1:
            return None
        out = pattern.sub(lambda _m, slot=slot: slot, out)
    return out


def load_cache(path: str) -> dict[str, tuple[str, str]]:
    """{원문 키: (번역문, 출처)}.

    번역캐시에는 화면 OCR 이 잘못 읽은 줄('点R丨', '适示')과 문단 조각도 섞여 있다. 묶음에는 원문이 게임 글과
    키까지 똑같은 줄만 들어가므로 그런 줄은 쓰이지 않는다 (2026-11-04: 3,242줄 중 287줄이 게임 글이 아니었다).
    번역이 비었거나 실패했거나 중국어가 그대로 남은 줄도 빼서 새로 번역하게 한다.
    """
    if not os.path.exists(path):
        return {}
    out = {}
    for row in read_table(path):
        k = tn.key(row[0])
        # 태그를 덜 뗀 원문으로 번역했던 줄은 번역문에도 '<$ba.liftoff>' 가 남아 있다
        ko = tn.strip_rich(row[1]).strip() if len(row) > 1 else ""
        if k and usable_translation(ko):
            out[k] = (ko, row[2] if len(row) > 2 else "")
    return out


def usable_translation(ko: str) -> bool:
    """번역문으로 쓸 만한가. 괄호 안 한자 풀이('은시(隱市)')는 괜찮고, 괄호 밖에 중국어가 남았으면 덜 번역된 것이다."""
    if not ko or mt.FAILED in ko:
        return False
    outside = re.sub(r"[(（][^()（）]*[)）]", "", ko)
    return len(re.findall(r"[㐀-鿿]", outside)) < 2


def load_existing(folder: str) -> dict[str, dict[str, tuple[str, str]]]:
    """{분류: {원문 키: (원문, 번역문)}}."""
    out = {}
    for name in CATEGORIES:
        path = os.path.join(folder, f"{name}.tsv")
        if os.path.exists(path):
            out[name] = {tn.key(zh): (zh, ko) for zh, ko in pack.read_tsv(path)}
    return out


def write(folder: str, done: dict, revision: str, engines: dict) -> dict:
    os.makedirs(folder, exist_ok=True)
    files = {}
    for name, rows in done.items():
        if not rows:
            continue
        path = os.path.join(folder, f"{name}.tsv")
        pack.write_tsv(path, rows.values(), CATEGORIES[name])
        files[f"{name}.tsv"] = {"count": len(rows), "sha256": pack.sha256(path), "about": CATEGORIES[name]}
    manifest = {
        "format": pack.FORMAT,
        "version": dt.datetime.now().strftime("%Y.%m.%d-%H%M"),
        "game_revision": revision,
        "engines": engines,
        "files": files,
    }
    with open(os.path.join(folder, pack.MANIFEST), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    readme = os.path.join(folder, "README.md")
    if not os.path.exists(readme):
        with open(readme, "w", encoding="utf-8", newline="\n") as f:
            f.write(README)
    for base, _dirs, names in os.walk(PACK_REPO_FILES):
        for name in names:
            src = os.path.join(base, name)
            dest = os.path.join(folder, os.path.relpath(src, PACK_REPO_FILES))
            if not os.path.exists(dest) and not name.endswith(".pyc"):
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(src, dest)
    return manifest


class NameRules:
    """묶음 번역에서 이름으로 지킬 두 글자 이름을 줄마다 고른다 (Glossary.protect 의 speakers 로 넘긴다).
    terms 는 이름처럼 확인할 다른 공식 이름 (통합전략 유물·이벤트 이름)."""

    def __init__(self, db, names, terms=()):
        self.short = {zh: ko for zh, ko in names if len(zh) == 2 and ko}
        self.always = {zh for zh, (seen, named) in db.name_usage(self.short).items()
                       if seen < NAME_RARE or named / seen >= NAME_RATE}
        self.names = {zh for zh, ko in names if ko} | {zh for zh, _ko in terms}

    def speakers(self, zh: str) -> set[str]:
        listed = {piece for m in _OPERATOR.finditer(zh) for piece in m.group(1).split("、")}
        return self.always | (listed & self.short.keys())

    def missing(self, glossary, zh: str, ko: str) -> list[str]:
        """번역문에 공식 이름이 빠진 이름 (번역기가 자리표를 망가뜨렸거나, 두 글자 이름을 지키기 전에 번역한 줄)."""
        return [name for name, official in glossary.found(zh, self.speakers(zh)).items()
                if name in self.names and official not in ko]


def quota_left(engine) -> int | None:
    """번역기가 알려 주는 남은 한도(DeepL). 모르면 None."""
    usage = getattr(getattr(engine, "primary", None), "usage", None)
    if usage is None:
        return None
    used, limit = usage()
    return max(0, limit - used)


def spending_cap(engine, budget: int, reserve: int) -> int | None:
    """이번에 번역해도 되는 글자 수. DeepL 이면 남은 한도에서 reserve 를 남긴다. 제한이 없으면 None."""
    caps = [budget] if budget else []
    left = quota_left(engine)
    if left is not None:
        caps.append(max(0, left - reserve))
        print(f"DeepL 남은 한도 {left:,}자 · 화면 번역용으로 {reserve:,}자를 남기고 최대 {caps[-1]:,}자를 씁니다.")
    return min(caps) if caps else None


def make_engine(name: str):
    cfg = {"engine": name, "deepl_key": os.environ.get("DEEPL_KEY", ""),
           "papago_id": os.environ.get("PAPAGO_ID", ""), "papago_secret": os.environ.get("PAPAGO_SECRET", "")}
    if name == "deepl" and not cfg["deepl_key"]:
        raise SystemExit("DEEPL_KEY 환경 변수가 없습니다.")
    stop = {"quota": False}

    def on_quota(_chain):
        stop["quota"] = True

    chain = mt.create(cfg, print, on_quota)
    if name != "google" and chain.primary is None:
        raise SystemExit(f"{name} 키가 없습니다.")
    return chain


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="번역 묶음 만들기")
    ap.add_argument("--out", required=True, help="묶음 폴더 (번역 저장소를 clone 한 곳)")
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "official.sqlite"))
    ap.add_argument("--engine", default="deepl", choices=["deepl", "papago", "google"])
    ap.add_argument("--only", default="", help="이 분류만 (쉼표로): " + ",".join(CATEGORIES))
    ap.add_argument("--with-story", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="새로 번역할 줄 수 상한 (시험용)")
    ap.add_argument("--dry-run", action="store_true", help="번역하지 않고 양만 본다")
    ap.add_argument("--keep-stale", action="store_true", help="공식 번역이 생긴 줄도 묶음에 남긴다")
    ap.add_argument("--publish", action="store_true", help="다 되면 묶음 폴더에서 git commit + push")
    ap.add_argument("--cache", default=os.path.join(ROOT, "data", "번역캐시.md"),
                    help="이미 번역해 둔 줄을 가져올 번역캐시 (기본: 이 프로그램의 data/번역캐시.md)")
    ap.add_argument("--no-cache", action="store_true", help="번역캐시를 쓰지 않고 전부 새로 번역한다")
    ap.add_argument("--cache-only", action="store_true", help="번역기를 부르지 않고 번역캐시에 있는 줄로만 묶음을 만든다")
    ap.add_argument("--budget", type=int, default=0, help="이번에 번역할 글자 수 상한 (0 이면 없음)")
    ap.add_argument("--reserve", type=int, default=50000,
                    help="DeepL 남은 한도 가운데 게임 중 화면 번역용으로 남겨 둘 글자 수 (기본 5만)")
    ap.add_argument("--redo-quoted", action="store_true",
                    help="따옴표 용어('“簇火”')가 든 숫자 틀을 용어를 맞춰 다시 번역한다")
    ap.add_argument("--redo-names", action="store_true",
                    help="이미 번역한 줄 가운데 오퍼레이터·통합전략 공식 이름이 빠진 줄을 다시 번역한다")
    args = ap.parse_args()

    db = OfficialDB(args.db)
    revision = db.meta.get("revision", "")
    todo = collect(db, args.with_story)
    if args.only:
        wanted = {c.strip() for c in args.only.split(",") if c.strip()}
        todo = {name: items for name, items in todo.items() if name in wanted}
    existing = load_existing(args.out)
    names = load_names(db)
    user = MarkdownStore(os.path.join(ROOT, "data", "용어집.md"), GLOSSARY_HEADER)
    glossary = Glossary(db, user)
    glossary.add_names(names)
    rogue = db.rogue_names()            # 통합전략 유물·이벤트 이름도 공식 이름으로 지킨다
    glossary.add_terms(rogue)
    # 공식 문장에서 늘 같은 한국어로 옮겨지는 이벤트·작전·스테이지·생명연산 이름 ('视相博物馆' = 폴리비전 박물관, tools/name_terms.py)
    import name_terms

    glossary.add_terms(name_terms.fixed_names(args.db).items())
    rules = NameRules(db, names, rogue)

    # 지난 묶음에서 이어받기. 공식 번역이 생겨 더는 필요 없는 줄은 뺀다 (--keep-stale 이면 남긴다)
    done: dict[str, dict[str, tuple[str, str]]] = {}
    new: list[tuple[str, str]] = []
    for name, items in todo.items():
        old = existing.get(name, {})
        keys = {tn.key(zh) for zh in items}
        done[name] = dict(old) if args.keep_stale else {k: v for k, v in old.items() if k in keys}
        new += [(name, zh) for zh in items if tn.key(zh) not in old]
    if "story" in existing and "story" not in done:
        # 스토리는 tools/story_pack.py 가 옮긴다 (DeepL 초벌 → Gemini). 여기서는 공식 번역이 생긴 줄만 뺀다
        still = {tn.key(_clean(zh)) for _f, _path, zh in db.untranslated_story()}
        old = existing["story"]
        done["story"] = dict(old) if args.keep_stale else {k: v for k, v in old.items() if k in still}
    for name, old in existing.items():
        if name not in done:
            # --only 로 다른 분류만 돌렸으면 이 분류는 그대로 둔다. 다 돌렸는데 번역할 글이 없어졌으면 비운다
            done[name] = old if (args.only or args.keep_stale or name in MANUAL) else {}
    # 화면에서·미리 번역으로 이미 번역해 둔 줄(번역캐시)은 다시 번역하지 않고 그대로 쓴다
    try:
        with open(os.path.join(args.out, pack.MANIFEST), encoding="utf-8") as f:
            engines: dict[str, int] = dict(json.load(f).get("engines", {}))   # 지난 판까지 쓴 번역기에 더해 간다
    except (OSError, ValueError):
        engines = {}
    reused: dict[str, int] = {}
    if not args.no_cache:
        cache = load_cache(args.cache)
        rest = []
        for name, zh in new:
            hit = cache.get(tn.key(zh))
            if hit is None:
                rest.append((name, zh))
                continue
            done[name][tn.key(zh)] = (zh, hit[0])
            label = f"번역캐시({hit[1] or '?'})"
            engines[label] = engines.get(label, 0) + 1
            reused[name] = reused.get(name, 0) + 1
        new = rest
    if args.redo_names:
        # 번역캐시에서 가져온 줄도 본다. 다시 번역할 줄은 번역캐시로 도로 채우지 않게 캐시 다음에 더한다.
        # 예전 번역은 새 번역이 덮을 때까지 남긴다 (한도에 닿아 도중에 멈춰도 줄이 비지 않게)
        redo = [(name, zh) for name in todo for zh, ko in done.get(name, {}).values()
                if rules.missing(glossary, zh, ko)]
        new += redo
        print(f"공식 이름이 빠진 줄 {len(redo):,}줄을 다시 번역합니다.")
    if args.redo_quoted:
        redo = [("templates", zh) for zh, _ko in done.get("templates", {}).values() if _QUOTED.search(zh)]
        new += [item for item in redo if item not in new]
        print(f"따옴표 용어가 든 숫자 틀 {len(redo):,}줄을 다시 번역합니다.")
    print(f"게임 데이터 {db.meta.get('built_at', '?')} (커밋 {revision[:10]})")
    for name, items in todo.items():
        fresh = [zh for n, zh in new if n == name]
        print(f"  {name:10s} 번역할 글 {len(items):6,}줄 · 번역캐시에서 {reused.get(name, 0):6,}줄 · "
              f"새로 번역 {len(fresh):6,}줄 {sum(map(len, fresh)):9,}자")
    # 한도가 모자라 도중에 멈춰도 쓸모 있는 것부터 채워지게 분류 순서를 정한다
    new.sort(key=lambda item: PRIORITY.index(item[0]) if item[0] in PRIORITY else len(PRIORITY))
    if args.limit:
        new = new[:args.limit]
    total = sum(len(zh) for _n, zh in new)
    if args.cache_only:
        print(f"번역캐시에 없는 {len(new):,}줄, {total:,}자는 이번에 번역하지 않고 남겨 둔다.")
        new = []
    else:
        print(f"새로 번역: {len(new):,}줄, {total:,}자 ({args.engine})")
    if args.dry_run or not new:
        if not args.dry_run:
            manifest = write(args.out, done, revision, engines)
            print(f"새로 번역할 것은 없어 묶음만 다시 썼습니다 (버전 {manifest['version']}).")
        return 0

    engine = make_engine(args.engine)
    budget = spending_cap(engine, args.budget, args.reserve)
    fix_quoted_terms(glossary, engine, [zh for name, zh in new if name == "templates"])
    i = requests = fails = lost_slots = 0
    started = time.time()
    try:
        spent = 0
        while i < len(new):
            size = engine.batch
            batch, chars = [new[i]], len(new[i][1])
            for item in new[i + 1:i + size]:
                if item[0] != batch[0][0] or chars + len(item[1]) > pretranslate.BATCH_CHARS:
                    break
                batch.append(item)
                chars += len(item[1])
            if budget is not None and spent + chars > budget:
                print(f"쓸 수 있는 글자 수({budget:,}자)에 닿아 멈춥니다. 남은 {len(new) - i:,}줄은 다음에 번역합니다.")
                break
            spent += chars
            marks = [mark_slots(zh) if name == "templates" else (zh, None) for name, zh in batch]
            try:
                results = translate_batch(glossary, engine, [m for m, _k in marks],
                                          [rules.speakers(zh) for _n, zh in batch])
            except mt.QuotaExhausted:
                print("번역기 한도를 다 써서 멈춥니다. 지금까지 번역한 것은 저장합니다.")
                break
            except Exception as exc:    # 번역기 쪽 장애 (429, 연결 끊김 등). 쉬었다가 다시, 거듭되면 멈춘다
                fails += 1
                print(f"  번역 실패 ({type(exc).__name__}: {exc}) — {RETRY_WAIT:.0f}초 뒤 다시 ({fails}/{MAX_FAILS})")
                if fails >= MAX_FAILS:
                    print("번역기가 계속 실패해 멈춥니다. 지금까지 번역한 것은 저장합니다.")
                    break
                time.sleep(RETRY_WAIT)
                continue
            fails = 0
            for (name, zh), (_m, slots), (ko, label) in zip(batch, marks, results):
                if slots is not None:
                    ko = unmark_slots(ko, slots)
                    if ko is None:
                        lost_slots += 1     # 번역기가 숫자를 빠뜨렸다. 다음에 다시 번역한다
                        continue
                done.setdefault(name, {})[tn.key(zh)] = (zh, ko)
                engines[label] = engines.get(label, 0) + 1
            i += len(batch)
            requests += 1
            if requests % SAVE_EVERY == 0:
                write(args.out, done, revision, engines)
                rate = i / max(1e-6, time.time() - started)
                print(f"  {i:,}/{len(new):,}줄 ({rate:.1f}줄/초)")
                # 번역기는 이름 자리표·태그까지 세어 원문 글자 수보다 더 깎는다. 실제 남은 한도로 한 번 더 막는다
                try:
                    left = quota_left(engine) if budget is not None else None
                except Exception:           # 한도 조회만 실패했으면 어림 글자 수로 계속 막는다
                    left = None
                if left is not None and left <= args.reserve:
                    print(f"DeepL 남은 한도가 {left:,}자라 화면 번역용 몫을 지키려고 멈춥니다. "
                          f"남은 {len(new) - i:,}줄은 다음에 번역합니다.")
                    break
            time.sleep(pretranslate.PAUSE_BATCH if len(batch) > 1 else pretranslate.PAUSE_SINGLE)
    except KeyboardInterrupt:
        print("멈춤. 지금까지 번역한 것은 저장합니다.")
    manifest = write(args.out, done, revision, engines)
    if lost_slots:
        print(f"숫자 자리를 번역기가 빠뜨려 버린 줄 {lost_slots:,} (다음 실행 때 다시 번역한다)")
    print(f"번역 {i:,}줄 · 버전 {manifest['version']} · " +
          ", ".join(f"{k} {v['count']:,}줄" for k, v in manifest["files"].items()))
    if args.publish:
        publish(args.out, manifest["version"])
    return 0


def publish(folder: str, version: str) -> None:
    if not os.path.isdir(os.path.join(folder, ".git")):
        print(f"{folder} 는 git 저장소가 아니라 올리지 않았습니다.")
        return
    subprocess.run(["git", "-C", folder, "add", "-A"], check=True)
    if subprocess.run(["git", "-C", folder, "diff", "--cached", "--quiet"]).returncode == 0:
        print("바뀐 것이 없어 올리지 않았습니다.")
        return
    subprocess.run(["git", "-C", folder, "commit", "-m", f"번역 묶음 {version}"], check=True)
    subprocess.run(["git", "-C", folder, "push"], check=True)
    print("올렸습니다.")


if __name__ == "__main__":
    sys.exit(main())
