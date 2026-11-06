"""번역 묶음 다듬기 (배포자용). 짧은 글(선택지·이름·결과 문구)의 기계 번역을 Gemini 로 공식 번역 말투에 맞춘다.

기계 번역기(DeepL)는 한 줄씩 따로 옮겨서, 한 화면의 선택지가 '큰 통 하나를 옮기다' / '작은 양동이 두 개를
옮겨 주세요' 처럼 제각각이 된다. 한국 서버 공식 번역의 통합전략 짧은 글을 예시로 보여 주고 고친다:
선택지는 '~한다'('선물을 받는다'), 이름은 명사, 결과 문구는 짧게('소장품 1개 획득').

  set GEMINI_KEY=...  &&  python -I tools/polish_pack.py --out ..\\ak-translations
  python -I tools/polish_pack.py --out ..\\ak-translations --only rogue --limit 100 --dry-run
  python -I tools/polish_pack.py --out ..\\ak-translations --slots      # 숫자 틀(templates): 스킬 설명·시스템 문구

--slots 는 '{…}' 자리표시자가 든 줄(숫자 틀)을 한국 서버 공식 스킬 설명·시스템 문구 말투로 맞춘다. 자리표시자가 하나라도
바뀌거나 빠지면 받지 않는다. 자리표시자 뒤 조사는 값을 모르므로 '이(가)·을(를)' 꼴로 쓴다 (공식 번역도 그렇게 쓴다).

고친 줄은 Gemini 응답이 다음을 모두 지킬 때만 바꾼다: 숫자가 같다, 원래 번역에 있던 공식 용어가 그대로 있다,
한자가 새로 생기지 않았다, 길이가 크게 바뀌지 않았다. 묶음 도구는 이미 있는 줄을 다시 번역하지 않으므로 다음 판에도 남는다.
키는 환경 변수(GEMINI_KEY)로만 받는다.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)    # -I 로 실행하면 스크립트 폴더가 경로에 없다
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_pack  # noqa: E402
from ak_translator import pack, textnorm as tn  # noqa: E402
from ak_translator.glossary import Glossary  # noqa: E402
from ak_translator.memory import GLOSSARY_HEADER, MarkdownStore  # noqa: E402
from ak_translator.names import load_names  # noqa: E402
from ak_translator.official import OfficialDB  # noqa: E402
from ak_translator.stylist import DEFAULT_MODELS, Gemini, GeminiError  # noqa: E402

SHORT = 20          # 원문(문장 부호 뺀) 이만큼 이하인 줄만 다듬는다. 긴 설명문은 말투가 이미 고르다
BATCH = 100
EXAMPLES = 60       # 보여 줄 공식 번역 예시 수
LABEL = "Gemini 다듬기"
RETRIES = 4         # 붐빔(503)은 몇 번 다시 보낸다
RETRY_WAIT = 20.0
_CJK = re.compile(r"[㐀-鿿]")
_SENTENCE = re.compile(r"[。！？!?…~～]")

SYSTEM = """너는 모바일 게임 명일방주(Arknights) 한국 서버의 현지화 검수자다. 통합전략·생명연산·이벤트 화면의
짧은 글(플레이어가 고르는 선택지, 유물·이벤트·장소 이름, 결과·효과·조건 문구)을 기계 번역한 한국어를
한국 서버 공식 번역 말투로 다듬는다.

규칙
1. 플레이어가 고르는 선택지(행동)는 [공식 번역 예시]처럼 '~한다' 꼴로 끝낸다 (선물을 받는다, 들어가 본다,
   휴식을 취한다). '~하세요', '~해 주세요', '~하다', '~하자', '~해보자!' 로 끝내지 않는다.
2. 이름(유물·이벤트·장소·노드·적)은 명사로 둔다. 문장 어미를 붙이지 않는다.
3. 결과·효과·조건 문구는 공식 예시처럼 짧게 쓴다 (소장품 1개 획득, 목표 HP +2, 오리지늄각뿔 4개 소모).
4. 뜻을 바꾸거나 정보를 빼거나 더하지 않는다. 숫자·기호·괄호는 그대로 둔다.
5. 줄마다 주어진 [용어]의 한국어 표기는 반드시 그대로 쓴다.
6. 맞춤법을 바로잡는다 (피빛 → 핏빛 등).
7. 인물이 하는 말로 보이는 줄은 높임과 말투를 바꾸지 않는다. 시제도 바꾸지 않는다.
8. 이미 알맞으면 그대로 돌려준다. 설명을 붙이지 않는다.

출력은 JSON 하나만: {"lines": [{"id": 번호, "ko": "다듬은 번역"}, ...]}"""


SYSTEM_SLOTS = """너는 모바일 게임 명일방주(Arknights) 한국 서버의 현지화 검수자다. 숫자가 들어갈 자리('{0}', '{atk_scale:0%}'
같은 자리표시자)가 있는 게임 글(스킬·재능 설명, 효과 문구, 시스템 알림)을 기계 번역한 한국어를 한국 서버 공식 번역 말투로 다듬는다.

규칙
1. 자리표시자('{'와 '}'로 둘러싼 것)는 글자 하나 바꾸지 말고 모두 그대로 둔다. 빼거나 더하지 않는다. 자리는 한국어 어순에 맞게 옮겨도 된다.
2. 스킬·재능·효과 설명은 [공식 번역 예시]처럼 '~입힘', '~증가', '~감소', '~획득' 같은 짧은 끝맺음으로 쓴다 ('~합니다', '~했다'로 끝내지 않는다).
3. 시스템 알림(획득·합류·남은 시간 등)도 공식 예시처럼 짧게 쓴다 (신규 재능 {0} 습득, 잔여 횟수: {0}).
4. 자리표시자 바로 뒤 조사는 값에 따라 달라지므로 '이(가)', '을(를)', '은(는)', '와(과)', '(으)로' 꼴로 쓴다.
5. 용어: damage=대미지, 物理伤害=물리 대미지, 法术伤害=마법 대미지, 攻击力=공격력, 防御力=방어력, 部署=배치, 再部署=재배치, 干员=오퍼레이터.
   줄마다 주어진 [용어]의 한국어 표기는 반드시 그대로 쓴다.
6. 뜻을 바꾸거나 정보를 빼거나 더하지 않는다. 숫자·기호·괄호는 그대로 둔다.
7. 중국어 낱말을 한국 한자음으로 그대로 읽은 말(自选 → 자선)을 새로 만들지 않는다. 원래 번역의 낱말이 뜻에 맞으면 그대로 둔다.
8. 이미 알맞으면 그대로 돌려준다. 설명을 붙이지 않는다.

출력은 JSON 하나만: {"lines": [{"id": 번호, "ko": "다듬은 번역"}, ...]}"""
_SLOT = re.compile(r"\{[^{}]*\}")
_RICH_TAG = re.compile(r"</?[@$]?[\w.=#]*>")


def slot_examples(db) -> list[dict]:
    """한국 서버 공식 번역의 스킬 설명·통합전략·시스템 숫자 틀 (서식 꼬리표는 뗀다)."""
    rows = db._db().execute(
        "SELECT zh, ko FROM templates WHERE ko IS NOT NULL AND zh LIKE '%{%' AND length(zh) BETWEEN 8 AND 120 "
        "AND src IN ('excel:skill_table', 'excel:roguelike_topic_table', 'ui', 'excel:character_table')").fetchall()
    rows = [(_RICH_TAG.sub("", zh).replace("\\n", " "), _RICH_TAG.sub("", ko).replace("\\n", " ")) for zh, ko in rows]
    rows = [(zh, ko) for zh, ko in rows if "(( " not in ko]
    rng = random.Random(7)
    return [{"zh": zh, "ko": ko} for zh, ko in rng.sample(rows, min(EXAMPLES, len(rows)))]


def official_bits(db, zh: str) -> dict[str, str]:
    """글 속의 4~6자 한자 덩어리 가운데 그 자체로 공식 번역이 있는 것 ('自选编队' → '자체 편성'). 긴 것부터, 겹치지 않게.
    용어 표에는 없는 화면 이름이라 Gemini 가 '자선 편대' 처럼 한자음으로 읽었다."""
    out, taken = {}, set()
    for size in (6, 5, 4):
        for i in range(len(zh) - size + 1):
            part = zh[i:i + size]
            if taken & set(range(i, i + size)) or not all("㐀" <= ch <= "鿿" for ch in part):
                continue
            hit = db.lookup(part, fuzzy=False)
            if hit and hit.ko and hit.via == "exact" and "{" not in hit.ko and len(hit.ko) <= 12:
                out[part] = tn.display_ko(hit.ko)
                taken |= set(range(i, i + size))
    return out


def accept_slots(old: str, new: str, terms: dict[str, str]) -> bool:
    """숫자 틀은 자리표시자가 하나도 안 바뀌어야 받는다. 나머지는 accept 와 같다."""
    if sorted(_SLOT.findall(old)) != sorted(_SLOT.findall(new.strip())):
        return False
    return accept(_SLOT.sub("#", old), _SLOT.sub("#", new), terms)


def examples(db) -> list[dict]:
    """한국 서버 공식 번역의 통합전략 짧은 글 (선택지·이름·결과 문구가 고루 섞인다)."""
    rows = db._db().execute(
        "SELECT zh, ko FROM texts WHERE src = 'excel:roguelike_topic_table' AND ko IS NOT NULL "
        "AND length(k) BETWEEN 2 AND ?", (SHORT,)).fetchall()
    rows = [(zh, ko) for zh, ko in rows if "<" not in zh and "{" not in zh and "\n" not in zh]
    rng = random.Random(7)      # 판마다 같은 예시를 써야 다듬은 결과가 들쭉날쭉하지 않다
    return [{"zh": zh, "ko": ko} for zh, ko in rng.sample(rows, min(EXAMPLES, len(rows)))]


def ask(gemini, prompt: str, system: str = SYSTEM) -> tuple[str, str]:
    """잠깐 붐비는 오류(503 등)는 쉬었다가 다시 보낸다. 키·한도 문제는 바로 멈춘다. story_pack 도 쓴다."""
    for attempt in range(RETRIES):
        try:
            return gemini.generate(system, prompt)
        except GeminiError as exc:
            if exc.stop or attempt == RETRIES - 1:
                raise
            print(f"  Gemini 가 {exc} — {RETRY_WAIT:.0f}초 뒤 다시 ({attempt + 1}/{RETRIES - 1})")
            time.sleep(RETRY_WAIT)
    raise GeminiError("다시 보내기 실패", stop=False)


def wanted(zh: str) -> bool:
    """선택지·이름·결과 문구로 보이는 줄. 마침표·물음표·느낌표가 있으면 인물 대사나 설명문이라 뺀다
    (첫 시험에서 '명중 여부는 신경 쓰지 마세요' 같은 대사를 반말로 바꿨다)."""
    return len(tn.key(zh)) <= SHORT and not _SENTENCE.search(zh)


def accept(old: str, new: str, terms: dict[str, str]) -> bool:
    """Gemini 가 고친 번역을 받아들일지. 의심스러우면 원래 번역을 둔다."""
    new = new.strip()
    if not new or new == old:
        return False
    if sorted(re.findall(r"\d+", old)) != sorted(re.findall(r"\d+", new)):
        return False
    if len(_CJK.findall(new)) > len(_CJK.findall(old)):
        return False
    if any(ko in old and ko not in new for ko in terms.values()):
        return False
    return 0.5 <= len(new) / max(1, len(old)) <= 1.8


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="번역 묶음의 짧은 글을 Gemini 로 공식 말투에 맞춘다")
    ap.add_argument("--out", required=True, help="묶음 폴더 (번역 저장소)")
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "official.sqlite"))
    ap.add_argument("--only", default="rogue,sandbox,events", help="다듬을 분류 (쉼표로)")
    ap.add_argument("--limit", type=int, default=0, help="다듬을 줄 수 상한 (시험용)")
    ap.add_argument("--dry-run", action="store_true", help="고칠 줄만 보여 주고 묶음은 그대로 둔다")
    ap.add_argument("--slots", action="store_true", help="숫자 틀('{…}' 이 든 줄)을 다듬는다 (--only 기본 templates)")
    args = ap.parse_args()
    if args.slots and args.only == ap.get_default("only"):
        args.only = "templates"
    key = os.environ.get("GEMINI_KEY", "")
    if not key:
        raise SystemExit("GEMINI_KEY 환경 변수가 없습니다.")

    db = OfficialDB(args.db)
    glossary = Glossary(db, MarkdownStore(os.path.join(ROOT, "data", "용어집.md"), GLOSSARY_HEADER))
    glossary.add_names(load_names(db))
    glossary.add_terms(db.rogue_names())
    # 공식 문장에서 늘 같은 한국어로 옮겨지는 이벤트·작전·스테이지·생명연산 이름 ('视相博物馆' = 폴리비전 박물관, tools/name_terms.py)
    import name_terms

    glossary.add_terms(name_terms.fixed_names(args.db).items())
    shots = slot_examples(db) if args.slots else examples(db)
    system = SYSTEM_SLOTS if args.slots else SYSTEM
    check = accept_slots if args.slots else accept
    done = build_pack.load_existing(args.out)
    todo = [(cat, k, zh, ko) for cat in args.only.split(",") if cat in done
            for k, (zh, ko) in done[cat].items() if ("{" in zh if args.slots else wanted(zh))]
    if args.limit:
        todo = todo[:args.limit]
    print(f"다듬을 줄 {len(todo):,} (분류 {args.only}) · 요청 {-(-len(todo) // BATCH)}번")

    with open(os.path.join(args.out, pack.MANIFEST), encoding="utf-8") as f:
        manifest = json.load(f)
    before = manifest.get("engines", {}).get(LABEL, 0)    # 지난번까지 다듬은 줄 수
    gemini = Gemini(key, DEFAULT_MODELS)
    changed = 0
    for start in range(0, len(todo), BATCH):
        batch = todo[start:start + BATCH]
        lines, terms = [], {}
        for i, (_cat, _k, zh, ko) in enumerate(batch):
            hints = {**official_bits(db, zh), **glossary.found(zh)} if args.slots else glossary.found(zh)
            found = {z: t for z, t in hints.items() if t in ko}
            terms[i] = found        # 원래 번역에 있던 공식 용어는 다듬은 뒤에도 있어야 받는다
            shown = hints if args.slots else found      # 숫자 틀은 원래 번역에 없던 공식 용어도 알려 준다
            lines.append({"id": i, "zh": zh, "ko": ko, **({"용어": shown} if shown else {})})
        prompt = ("[공식 번역 예시]\n" + json.dumps(shots, ensure_ascii=False) +
                  "\n\n[다듬을 줄]\n" + json.dumps(lines, ensure_ascii=False))
        try:
            text, model = ask(gemini, prompt, system)
            reply = {int(x["id"]): str(x["ko"]) for x in json.loads(text).get("lines", [])
                     if isinstance(x, dict) and "id" in x and "ko" in x}
        except GeminiError as exc:
            print(f"Gemini 가 {exc} 멈춥니다. 지금까지 고친 것은 저장합니다.")
            break
        except (ValueError, TypeError):
            print("  응답을 읽지 못해 이 묶음은 건너뜁니다.")
            continue
        for i, (cat, k, zh, ko) in enumerate(batch):
            new = reply.get(i, "")
            if check(ko, new, terms[i]):
                changed += 1
                if args.dry_run:
                    print(f"  {zh} | {ko} → {new.strip()}")
                else:
                    done[cat][k] = (zh, new.strip())
        print(f"  {min(start + BATCH, len(todo)):,}/{len(todo):,}줄 · 고침 {changed:,} ({model})")
        if not args.dry_run:
            engines = dict(manifest.get("engines", {}), **{LABEL: before + changed})
            build_pack.write(args.out, done, manifest.get("game_revision", ""), engines)
        time.sleep(1.0)
    print(f"고친 줄 {changed:,}" + (" (시험이라 저장하지 않음)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
