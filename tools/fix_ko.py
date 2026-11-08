"""번역 묶음에서 틀린 한국어를 찾아 고치기 (배포자용). 중국어 원문이 아니라 번역문으로 찾는다.

  python -I tools/fix_ko.py 성준보                         # 찾기: 번역문에 '성준보' 가 든 줄 (분류별 수와 보기)
  python -I tools/fix_ko.py 성준보 성준성 --zh 圣骏堡       # 원문에 '圣骏堡' 가 든 줄만
  python -I tools/fix_ko.py 성준보 성준성 생트페테르부르크 --zh 圣骏堡 --to "디에티 그라이퍼부르크"
                                                          # 바꾸기 미리 보기 (아직 저장 안 함)
  ... --to "디에티 그라이퍼부르크" --apply                # 묶음 폴더에 저장
  ... --to "디에티 그라이퍼부르크" --apply --publish      # 저장 + 검사 + 내 번역기에 넣기 + 커밋·push

찾을 말은 여러 개 줄 수 있다 (긴 것부터 맞춘다). 바꿀 때 바로 뒤에 붙은 조사를 새 말의 받침에 맞춘다:
을/를 · 이/가 · 은/는 · 과/와 · 으로/로(ㄹ 받침은 로) · 이나/나 · 이라/라 · 이랑/랑 · 이며/며 · 이야/야 · 이여/여 ·
이다/다 · 이고/고 · 이었/였 · 이에요/예요. 새 말이 한글로 끝나지 않으면(영문·숫자) 조사를 그대로 두고 따로 알린다.
--regex 면 찾을 말을 정규식으로 본다 (이때 조사는 맞추지 않는다).

공식 번역은 이 도구로 고칠 수 없다 (게임 데이터 DB 에 있고 번역 묶음보다 먼저 쓰인다). 찾기 결과 끝에 공식 번역에
같은 말이 몇 번 나오는지 함께 보여 준다 — 바꿀 공식 표기를 확인할 때 쓴다 (예: '디에티 그라이퍼부르크' 로 찾기).
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import build_pack  # noqa: E402
import update_data  # noqa: E402
from ak_translator.pack import MANIFEST  # noqa: E402

PACK = os.path.join(os.path.dirname(ROOT), "ak-translations")
DB = update_data.TARGET
# (받침 있을 때, 받침 없을 때). 긴 것부터 맞춘다
PARTICLES = (("이었", "였"), ("이에요", "예요"), ("이라는", "라는"), ("이라고", "라고"), ("이라", "라"),
             ("이랑", "랑"), ("이나", "나"), ("이며", "며"), ("이야", "야"), ("이여", "여"), ("이다", "다"),
             ("이고", "고"), ("으로", "로"), ("을", "를"), ("은", "는"), ("과", "와"), ("이", "가"))
_FORMS = sorted({f for pair in PARTICLES for f in pair}, key=len, reverse=True)
_PAIR = {f: pair for pair in PARTICLES for f in pair}


def final(word: str) -> str | None:
    """끝 글자 받침: 'none' · 'rieul'(ㄹ) · 'other'. 끝이 한글 음절이 아니면 None."""
    ch = word.rstrip()[-1:]
    if not ("가" <= ch <= "힣"):
        return None
    jong = (ord(ch) - 0xAC00) % 28
    if jong == 0:
        return "none"
    if jong == 8:
        return "rieul"
    return "other"


def particle_for(form: str, word: str) -> str | None:
    """조사 form 을 word 뒤에 맞는 꼴로. 받침을 모르면 None."""
    kind = final(word)
    if kind is None:
        return None
    with_b, without_b = _PAIR[form]
    if with_b == "으로":
        return "로" if kind in ("none", "rieul") else "으로"
    return without_b if kind == "none" else with_b


def make_replacer(patterns: list[str], target: str | None, regex: bool):
    """(찾기 정규식, 한 줄 바꾸는 함수). 바꾸는 함수는 (새 번역, 조사 고친 수, 조사 모를 수)."""
    words = patterns if regex else [re.escape(p) for p in sorted(patterns, key=len, reverse=True)]
    alternation = "(" + "|".join(words) + ")"
    find = re.compile(alternation)
    if target is None:
        return find, None
    with_particle = find if regex else re.compile(alternation + "(" + "|".join(_FORMS) + ")?")

    def fix(ko: str) -> tuple[str, int, int]:
        changed = unknown = 0

        def one(m: re.Match) -> str:
            nonlocal changed, unknown
            form = None if regex else m.group(2)
            if not form:
                return target
            right = particle_for(form, target)
            if right is None:
                unknown += 1
                return target + form
            if right != form:
                changed += 1
            return target + right

        return with_particle.sub(one, ko), changed, unknown

    return find, fix


def official_count(text: str) -> int | None:
    """공식 번역(한국 서버 문장·스토리)에 이 말이 몇 줄 나오나."""
    if not os.path.exists(DB):
        return None
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        like = f"%{text}%"
        return sum(con.execute(f"SELECT COUNT(*) FROM {t} WHERE ko LIKE ?", (like,)).fetchone()[0]
                   for t in ("texts", "story"))
    finally:
        con.close()


def mark(text: str, find: re.Pattern, width: int = 90) -> str:
    """찾은 곳을 «» 로 표시하고, 첫 찾은 곳 둘레만 잘라 보인다."""
    m = find.search(text)
    start = max(0, (m.start() if m else 0) - width // 3)
    piece = text[start:start + width]
    return ("…" if start else "") + find.sub(lambda x: f"«{x.group(0)}»", piece) + ("…" if start + width < len(text) else "")


def publish(folder: str, message: str) -> int:
    from game_update import git

    code = subprocess.call([sys.executable, "-I", os.path.join(ROOT, "tools", "install_pack.py"), "--out", folder, "--stage"],
                           cwd=ROOT)
    if code != 0:
        print("번역 묶음 검사에 걸려 올리지 않았습니다. 저장한 파일은 그대로 있습니다.")
        return 1
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "-m", message)
    git(folder, "push", "-q")
    print("올렸습니다. 쓰는 사람은 켤 때 새 번역 묶음 알림을 받습니다. 내 번역기는 다시 켜면 씁니다.")
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="번역 묶음에서 한국어 번역문으로 찾아 고치기")
    ap.add_argument("words", nargs="+", help="찾을 한국어 (여러 개 가능)")
    ap.add_argument("--to", help="이 말로 바꾼다 (주지 않으면 찾기만)")
    ap.add_argument("--zh", help="원문(중국어)에 이 글이 든 줄만")
    ap.add_argument("--only", default="", help="이 분류만 (쉼표로): " + ",".join(build_pack.CATEGORIES))
    ap.add_argument("--regex", action="store_true", help="찾을 말을 정규식으로 (조사는 맞추지 않는다)")
    ap.add_argument("--show", type=int, default=15, help="분류마다 보여 줄 줄 수 (0 이면 모두)")
    ap.add_argument("--apply", action="store_true", help="바꾼 것을 묶음 폴더에 저장한다")
    ap.add_argument("--publish", action="store_true", help="저장 뒤 검사·내 번역기에 넣기·커밋·push 까지")
    ap.add_argument("--out", default=PACK, help="번역 저장소 폴더")
    args = ap.parse_args()
    if args.publish and not args.apply:
        raise SystemExit("--publish 는 --apply 와 같이 씁니다.")
    if args.apply and not args.to:
        raise SystemExit("--apply 는 --to 와 같이 씁니다.")
    if args.publish:
        # 고치기 전에 GitHub 에서 고친 줄(웹 편집·Action)을 먼저 받는다. 방법은 game_update 와 같다
        from game_update import sync_pack

        problem = sync_pack(args.out, False)
        if problem:
            raise SystemExit(problem)

    done = build_pack.load_existing(args.out)
    only = {c.strip() for c in args.only.split(",") if c.strip()}
    find, fix = make_replacer(args.words, args.to, args.regex)
    hits: dict[str, list] = collections.defaultdict(list)
    for cat, rows in done.items():
        if only and cat not in only:
            continue
        for k, (zh, ko) in rows.items():
            if find.search(ko) and (not args.zh or args.zh in zh):
                hits[cat].append((k, zh, ko))

    total = sum(map(len, hits.values()))
    what = " · ".join(args.words) + (f" (원문에 '{args.zh}')" if args.zh else "")
    print(f"번역 묶음에서 {what}: {total:,}줄")
    if not total:
        for word in ([] if args.regex else args.words):
            n = official_count(word)
            if n:
                print(f"  공식 번역에는 '{word}' 가 {n:,}줄 있습니다 (공식 번역은 이 도구로 못 고칩니다).")
        return 0

    changed_particles = unknown_particles = 0
    new_rows: dict[str, dict] = {}
    for cat in sorted(hits, key=lambda c: -len(hits[c])):
        rows = hits[cat]
        print(f"\n[{cat}] {len(rows):,}줄")
        limit = len(rows) if args.show == 0 else args.show
        for n, (k, zh, ko) in enumerate(rows):
            if fix is None:
                if n < limit:
                    print(f"  {zh[:40]}\n    {mark(ko, find)}")
                continue
            new, changed, unknown = fix(ko)
            changed_particles += changed
            unknown_particles += unknown
            new_rows.setdefault(cat, {})[k] = (zh, new)
            if n < limit:
                print(f"  {zh[:40]}\n    전: {mark(ko, find)}\n    후: {new[:120]}")
        if len(rows) > limit:
            print(f"  … 나머지 {len(rows) - limit:,}줄 (--show 0 이면 모두)")

    if not args.regex:
        print()
        for word in args.words + ([args.to] if args.to else []):
            n = official_count(word)
            if n is not None:
                print(f"공식 번역에 '{word}': {n:,}줄")
    if fix is None:
        return 0
    print(f"\n바꿀 줄 {total:,} · 조사 고침 {changed_particles:,}"
          + (f" · 조사를 못 맞춘 곳 {unknown_particles:,} ('{args.to}' 가 한글로 끝나지 않음 — 저장 뒤 확인)"
             if unknown_particles else ""))
    if not args.apply:
        print("미리 보기입니다. 저장하려면 --apply 를 붙이세요.")
        return 0

    for cat, rows in new_rows.items():
        done[cat].update(rows)
    with open(os.path.join(args.out, MANIFEST), encoding="utf-8") as f:
        manifest = json.load(f)
    saved = build_pack.write(args.out, done, manifest.get("game_revision", ""), manifest.get("engines", {}))
    print(f"저장했습니다: {args.out} (묶음 판 {saved['version']})")
    if args.publish:
        return publish(args.out, f"번역 고침: {', '.join(args.words)} → {args.to} ({total:,}줄)")
    print("올리려면 같은 명령에 --publish 를 붙이거나 game_update.bat 을 실행하세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
