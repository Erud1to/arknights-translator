"""번역 묶음 번역문 속 공식 고유 이름 바로잡기 (배포자용, Gemini).

묶음을 만들 때 용어집이 지키지 않은 이름(이벤트·작전·스테이지·통합전략·생명연산 이름, '蚀刻章'=훈장 등)이 기계 번역
표기로 들어갔다 ('视相博物馆' → '시상 박물관', 공식은 '폴리비전 박물관'). 공식 문장에서 늘 같은 한국어로 옮겨지는 이름
(tools/name_terms.py)이 원문에 있는데 번역문에 그 공식 표기가 없는 줄을 찾아, Gemini 에게 이름 자리만 공식 표기로 바꾸게 한다.

  python -I tools/with_keys.py fix_names --dry-run --limit 40   # 바뀔 것만 보기
  python -I tools/with_keys.py fix_names                        # 묶음에 쓴다

받아들이는 조건: 요구한 공식 이름이 모두 들어 있다, 숫자·'{…}' 자리표시자가 같다, 한자가 새로 생기지 않았다,
바뀐 곳이 모두 공식 이름 자리(앞뒤 NEAR 글자)
안이다. 이름이 이 줄에서 보통 말로 쓰였으면 Gemini 가 고치지 않고 돌려준다 ('新线路' = '새 노선').
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_pack  # noqa: E402
import name_terms  # noqa: E402
from polish_pack import ask  # noqa: E402
from ak_translator import pack  # noqa: E402
from ak_translator.stylist import DEFAULT_MODELS, Gemini, GeminiError  # noqa: E402

BATCH = 40
NEAR = 3            # 이름 앞뒤로 바뀌어도 되는 글자 수 (조사·띄어쓰기·따옴표)
LABEL = "공식 이름 맞춤"
_CJK = re.compile(r"[㐀-鿿]")
_SLOT = re.compile(r"\{[^{}]*\}")

SYSTEM = """너는 모바일 게임 명일방주(Arknights) 한국 서버의 현지화 검수자다. 기계 번역한 한국어 문장에서 고유 이름(이벤트·작전·
스테이지·장소·아이템·통합전략 테마 등)이 공식 표기와 다르게 옮겨진 곳을 고친다.

규칙
1. 줄마다 [이름]에 중국어 이름과 한국 서버 공식 표기가 있다. 번역문에서 그 중국어 이름을 옮긴 자리를 찾아 공식 표기로 바꾼다.
   공식 표기는 글자 하나 바꾸지 말고 그대로 쓴다 (띄어쓰기 포함).
2. 바꾼 이름 뒤 조사는 받침에 맞게 고친다 (을/를, 이/가, 은/는, 와/과, 으로/로).
3. 그 밖의 문장은 바꾸지 않는다. 말투·어미·문장 부호·숫자·'{…}' 자리표시자·따옴표는 그대로 둔다.
4. 그 중국어가 이 줄에서는 고유 이름이 아니라 보통 말로 쓰였으면(예: '新线路' 가 이벤트 이름이 아니라 '새 노선' 이라는 뜻)
   그 줄은 고치지 말고 원래 번역을 그대로 돌려준다.
5. 이름 자리와 그 조사 말고는 한 글자도 바꾸지 않는다 (대명사·시제·높임·맞춤법도 그대로).
6. 설명을 붙이지 않는다.

출력은 JSON 하나만: {"lines": [{"id": 번호, "ko": "고친 번역"}, ...]}"""
_NORM = name_terms.norm
OPERATOR_SKIP = {"国度"}    # 문장에서는 '그의 나라' 말장난이라 이름 자리가 정해진 꼴만 rename_names 가 바꾼다
CJK_NEAR = 8                # --cjk: 남은 한자 자리 앞뒤로 바뀌어도 되는 글자 수 (이름 소리 옮김은 길이가 바뀐다)

SYSTEM_CJK = """너는 모바일 게임 명일방주(Arknights) 한국 서버의 현지화 검수자다. 기계 번역한 한국어 문장에 중국어 한자가 섞여 남은
곳('태양谷', '와마普', '다捞 센터')을 고친다.

규칙
1. 번역문에 남은 한자(괄호 안 풀이는 빼고)를 원문의 그 자리 뜻에 맞는 한국어로 바꾼다. 줄마다 [이름]에 공식 표기가 있으면
   그것을 글자 그대로 쓴다 (예: 太阳谷 = 선밸리). 공식 표기가 없는 고유 이름은 한국 서버 표기 관례대로 소리를 옮긴다.
2. 바꾼 말 뒤 조사는 받침에 맞게 고친다.
3. 그 밖의 문장은 바꾸지 않는다. 말투·어미·문장 부호·숫자·'{…}' 자리표시자·따옴표는 그대로 둔다.
4. 설명을 붙이지 않는다.

출력은 JSON 하나만: {"lines": [{"id": 번호, "ko": "고친 번역"}, ...]}"""


def mismatches(done: dict, names: dict[str, str]) -> list[tuple[str, str, str, str, dict]]:
    """[(분류, 키, 원문, 번역, {중국어 이름: 공식 표기})] — 공식 표기가 빠진 이름이 있는 줄."""
    out = []
    for cat, rows in done.items():
        if cat == pack.NAMES:
            continue
        for k, (zh, ko) in rows.items():
            need, nko, i = {}, _NORM(ko), 0
            while i < len(zh):
                hit = next((zh[i:i + n] for n in range(min(16, len(zh) - i), 2, -1)
                            if zh[i:i + n] in names and zh[i:i + n] != zh), None)
                if hit is None:
                    i += 1
                    continue
                if _NORM(names[hit]) not in nko:
                    need[hit] = names[hit]
                i += len(hit)
            if need:
                out.append((cat, k, zh, ko, need))
    return out


def accept(old: str, new: str, need: dict[str, str]) -> bool:
    new = new.strip()
    if not new or new == old or any(_NORM(ko) not in _NORM(new) for ko in need.values()):
        return False
    if sorted(re.findall(r"\d+", old)) != sorted(re.findall(r"\d+", new)):
        return False
    if sorted(_SLOT.findall(old)) != sorted(_SLOT.findall(new)):
        return False
    if len(_CJK.findall(new)) > len(_CJK.findall(old)):
        return False
    return localized(old, new, list(need.values()))


def localized(old: str, new: str, names: list[str]) -> bool:
    """바뀐 곳이 모두 공식 이름 자리(앞뒤 NEAR 글자 — 조사·띄어쓰기·따옴표) 안인가. 아니면 Gemini 가 이름 말고 다른 데도
    손댄 것이다 ('그녀에게는' → '그에게는', '장비 대…' → '장비응변')."""
    spans = [(m.start() - NEAR, m.end() + NEAR) for ko in names for m in re.finditer(re.escape(ko), new)]
    if not spans:
        return False
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if not any(a <= j1 and j2 <= b for a, b in spans):
            return False
    return True


def cjk_lines(done: dict, hints) -> list[tuple[str, str, str, str, dict]]:
    """괄호 밖에 한자가 남은 번역문 줄. 이름 힌트는 hints(원문) → {중국어: 공식 표기}."""
    out = []
    for cat, rows in done.items():
        for k, (zh, ko) in rows.items():
            if _CJK.search(re.sub(r"\([^()]*\)|（[^（）]*）", "", ko)):
                out.append((cat, k, zh, ko, hints(zh)))
    return out


def accept_cjk(old: str, new: str, _need: dict) -> bool:
    """남은 한자가 다 없어졌고, 바뀐 곳이 모두 예전 번역의 한자 자리 앞뒤 CJK_NEAR 글자 안이다."""
    new = new.strip()
    if not new or new == old or _CJK.search(re.sub(r"\([^()]*\)|（[^（）]*）", "", new)):
        return False
    if sorted(re.findall(r"\d+", old)) != sorted(re.findall(r"\d+", new)):
        return False
    if sorted(_SLOT.findall(old)) != sorted(_SLOT.findall(new)):
        return False
    spans = [(m.start() - CJK_NEAR, m.end() + CJK_NEAR) for m in _CJK.finditer(old)]
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    return all(any(a <= i1 and i2 <= b for a, b in spans)
               for tag, i1, i2, _j1, _j2 in sm.get_opcodes() if tag != "equal")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="묶음 번역문 속 공식 고유 이름 바로잡기")
    ap.add_argument("--out", required=True, help="묶음 폴더 (번역 저장소)")
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "official.sqlite"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--operators", action="store_true", help="한국 서버에 없는 오퍼레이터 이름(names.tsv)을 맞춘다")
    ap.add_argument("--cjk", action="store_true", help="번역문에 남은 한자를 고친다")
    args = ap.parse_args()
    key = os.environ.get("GEMINI_KEY", "")
    if not key:
        raise SystemExit("GEMINI_KEY 환경 변수가 없습니다.")
    done = build_pack.load_existing(args.out)
    system, check = SYSTEM, accept
    if args.operators:
        names = {zh: ko for zh, ko in done.get(pack.NAMES, {}).values() if zh not in OPERATOR_SKIP}
        todo = mismatches(done, names)
    elif args.cjk:
        from ak_translator.glossary import Glossary
        from ak_translator.memory import GLOSSARY_HEADER, MarkdownStore
        from ak_translator.names import load_names
        from ak_translator.official import OfficialDB
        from polish_pack import official_bits

        db = OfficialDB(args.db)
        glossary = Glossary(db, MarkdownStore(os.path.join(ROOT, "data", "용어집.md"), GLOSSARY_HEADER))
        glossary.add_names(load_names(db))
        glossary.add_terms(db.rogue_names())
        names = name_terms.fixed_names(args.db)
        glossary.add_terms(names.items())
        glossary.add_terms((zh, ko) for zh, ko in done.get(pack.NAMES, {}).values())
        todo = cjk_lines(done, lambda zh: {**official_bits(db, zh), **glossary.found(zh)})
        system, check = SYSTEM_CJK, accept_cjk
    else:
        names = name_terms.fixed_names(args.db)
        todo = mismatches(done, names)
    if args.limit:
        todo = todo[:args.limit]
    print(f"고칠 줄 {len(todo):,} · 요청 {-(-len(todo) // BATCH)}번")
    with open(os.path.join(args.out, pack.MANIFEST), encoding="utf-8") as f:
        manifest = json.load(f)
    before = manifest.get("engines", {}).get(LABEL, 0)
    gemini = Gemini(key, DEFAULT_MODELS)
    changed = rejected = 0
    for start in range(0, len(todo), BATCH):
        batch = todo[start:start + BATCH]
        lines = [{"id": i, "zh": zh, "ko": ko, "이름": need} for i, (_c, _k, zh, ko, need) in enumerate(batch)]
        try:
            text, model = ask(gemini, json.dumps(lines, ensure_ascii=False), system)
            reply = {int(x["id"]): str(x["ko"]) for x in json.loads(text).get("lines", [])
                     if isinstance(x, dict) and "id" in x and "ko" in x}
        except GeminiError as exc:
            print(f"Gemini 가 {exc} 멈춥니다. 지금까지 고친 것은 저장합니다.")
            break
        except (ValueError, TypeError):
            print("  응답을 읽지 못해 이 묶음은 건너뜁니다.")
            continue
        for i, (cat, k, zh, ko, need) in enumerate(batch):
            new = reply.get(i, "")
            if check(ko, new, need):
                changed += 1
                if args.dry_run:
                    print(f"  {ko[:70]}\n    → {new.strip()[:70]}")
                else:
                    done[cat][k] = (zh, new.strip())
            elif new.strip() and new.strip() != ko:
                rejected += 1
        print(f"  {min(start + BATCH, len(todo)):,}/{len(todo):,}줄 · 고침 {changed:,} · 안 받음 {rejected:,} ({model})")
        if not args.dry_run:
            engines = dict(manifest.get("engines", {}), **{LABEL: before + changed})
            build_pack.write(args.out, done, manifest.get("game_revision", ""), engines)
        time.sleep(1.0)
    print(f"고친 줄 {changed:,} · 안 받은 답 {rejected:,}" + (" (시험이라 저장하지 않음)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
