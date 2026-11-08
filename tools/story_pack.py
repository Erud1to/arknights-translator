"""스토리 번역 묶음 만들기 (배포자용). 공식 번역이 없는 스토리를 'DeepL 초벌 → Gemini' 로 옮겨 묶음의 story 분류에 넣는다.

DeepL 은 한 줄씩 옮겨 인물 말투를 모르고(반말 인물이 갑자기 존댓말), Gemini 는 혼자 옮기면 가끔 뜻을 틀린다
('哨兵' 보초 → 초소). 2026-11-05 '시간이여, 앞으로!' 1화 60줄로 견준 결과, 공식 대사와 높임이 같은 줄이
DeepL 단독 27/45, Gemini 단독 42/44, DeepL 초벌 → Gemini 43/44 였고 초벌이 Gemini 의 오역을 잡았다.

  파일 하나(최대 stylist.BATCH 줄)마다: DeepL 로 초벌(공식 용어는 자리표로 지킨다) → Gemini 에 초벌·인물별 공식 대사
  예시·앞 장면·용어집을 함께 보낸다 → 검사(한자 남음, 공식 용어 빠짐)에 걸린 줄은 한 번 더 묻고, 그래도 안 되면
  초벌을 쓴다. 파일마다 저장하므로 Gemini 하루 한도에 걸려 멈춰도 다음에 이어서 한다. 최근 이벤트부터 한다.
  DeepL 한도(--reserve 를 남긴 만큼)를 다 쓰면 Gemini 혼자 옮긴다.

  set DEEPL_KEY=...  &&  set GEMINI_KEY=...
  python -I tools/story_pack.py --out ..\\ak-translations
  python -I tools/story_pack.py --out ..\\ak-translations --file 1408 --print     # 파일 하나 시험 (저장 안 함)

키는 환경 변수로만 받는다. 묶음 도구(build_pack)는 story 분류를 번역하지 않고, 공식 번역이 생긴 줄만 뺀다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)    # -I 로 실행하면 스크립트 폴더가 경로에 없다
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_pack  # noqa: E402
from polish_pack import ask  # noqa: E402
from ak_translator import mt, pack, textnorm as tn  # noqa: E402
from ak_translator.glossary import Glossary  # noqa: E402
from ak_translator.memory import GLOSSARY_HEADER, MarkdownStore  # noqa: E402
from ak_translator.names import load_names  # noqa: E402
from ak_translator.official import OfficialDB  # noqa: E402
from ak_translator.resolver import translate_batch  # noqa: E402
from ak_translator.story_ai import NARRATION_EXAMPLES, clean  # noqa: E402
from ak_translator.stylist import BATCH, CONTEXT, EXAMPLES, SYSTEM, Gemini, GeminiError  # noqa: E402
from ak_translator.stylist import build_prompt, parse  # noqa: E402

KINDS = ("say", "option", "caption")
# Gemini 가 붐빈다(503)며 몇 번 연달아 거절하면 이만큼씩 쉬었다가 같은 줄을 다시 보낸다. 2026-11-05 새벽 전체 실행이
# 503 세 번에 멈췄다 (12,485/29,305줄). 키·하루 한도 문제(stop)는 기다리지 않고 멈춘다
BUSY_WAITS = (120, 300, 600, 900, 1800)
# 앞 모델이 붐비거나(503) 한도에 걸리면(429) 다음 모델로 넘어간다. 모델마다 붐빔·한도가 따로라 여럿 둔다.
# 2026-11-05 저녁: 3.8-flash 429, 3.7·3.6 503 일 때 3.5-flash·3.5-flash-lite·2.5-flash 는 됐다
STORY_MODELS = ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash",
                "gemini-3.5-flash-lite", "gemini-2.5-flash")
LABELS = {"both": "DeepL 초벌+Gemini 스토리", "gemini": "Gemini 스토리", "draft": "DeepL 스토리"}
STORY_SYSTEM = SYSTEM.replace(
    "7. 설명이나 주석을 붙이지 않는다.",
    "7. 설명이나 주석을 붙이지 않는다.\n"
    "8. [초벌 번역]이 있으면 기계 번역 초안이다. 뜻을 확인하는 데만 참고하고, 문장과 말투는 위 규칙대로 새로 쓴다.\n"
    "9. 원문에 없는 욕설·비하 표현이나 요즘 말(팩트, 레전드 등)을 넣지 않는다.\n"
    "10. 맞춤법과 띄어쓰기를 지킨다.")


class DeepLLimit(Exception):
    """--stop-at-deepl-limit 일 때 DeepL 몫을 다 썼다. Gemini 혼자 옮기지 않고 멈춘다."""


class Story:
    """스토리 파일 하나씩 옮긴다. 인물 예시·지문 예시는 모아 두고 다시 쓴다."""

    def __init__(self, db, glossary, gemini, deepl, budget, stop_at_limit=False):
        self.db, self.glossary, self.gemini, self.deepl = db, glossary, gemini, deepl
        self.budget = budget            # DeepL 로 더 보낼 수 있는 글자 수 (None 이면 제한 없음)
        self.stop_at_limit = stop_at_limit  # DeepL 몫을 다 쓰면 Gemini 혼자 옮기지 않고 멈춘다
        self.spent = 0
        self.examples: dict[str, tuple] = {}
        self.narration = db.narration_examples(NARRATION_EXAMPLES)
        self.counts = {key: 0 for key in LABELS}

    def lines(self, file_id: int, done: dict) -> tuple[list, list]:
        """(그 파일의 모든 줄, 번역할 줄). 이미 묶음에 있는 줄은 뺀다."""
        rows = self.db.script(file_id)
        todo = []
        for idx, kind, speaker, _speaker_ko, zh, ko in rows:
            text = clean(zh)
            if ko is None and kind in KINDS and tn.has_cjk(text) and tn.key(text) not in done:
                todo.append({"idx": idx, "kind": kind, "speaker": speaker or "", "zh": text})
        return rows, todo

    def translate(self, rows: list, chunk: list, done: dict) -> dict[str, str]:
        """{원문: 번역}. chunk 는 한 파일 안의 연속된 줄."""
        speakers = {line["speaker"] for line in chunk if line["speaker"]}
        names = self.glossary.found(" ".join(sorted(speakers)), speakers)
        lines = [{**line, "id": n, "speaker_ko": names.get(line["speaker"], "")} for n, line in enumerate(chunk, 1)]
        blob = "\n".join(l["zh"] for l in chunk) + "\n" + "\n".join(speakers)
        terms = self.glossary.found(blob, speakers)
        base = build_prompt(lines, self._context(rows, chunk[0]["idx"], done),
                            self._speaker_examples(speakers, names), self.narration, terms)

        drafts = self._drafts(lines, speakers)
        prompt = base
        if drafts:
            prompt += "\n\n[초벌 번역 (기계 번역, 참고용)]  번호|초벌\n" + "\n".join(
                f"{l['id']}|{drafts[l['id']]}" for l in lines)
        reply = self._ask(prompt)
        got = parse(reply, {l["id"] for l in lines})

        bad = [l for l in lines if self._problems(l, got.get(l["id"], ""), speakers)]
        if bad:
            # 걸린 줄만 다시 묻는다. 용어집을 다시 강조한다
            again = [l for l in lines if l in bad]
            retry = build_prompt(again, self._context(rows, again[0]["idx"], done),
                                 self._speaker_examples(speakers, names), self.narration, terms)
            retry += ("\n\n[주의] 지난번 번역에 한자가 남았거나 [용어집] 표기를 쓰지 않았다. 용어집 표기를 반드시 그대로 쓸 것.")
            if drafts:
                retry += "\n\n[초벌 번역 (기계 번역, 참고용)]  번호|초벌\n" + "\n".join(
                    f"{l['id']}|{drafts[l['id']]}" for l in again)
            reply = self._ask(retry)
            got.update({i: ko for i, ko in parse(reply, {l["id"] for l in again}).items()
                        if not self._problems(next(l for l in again if l["id"] == i), ko, speakers)})

        out = {}
        for l in lines:
            ko = got.get(l["id"], "")
            if ko and not self._problems(l, ko, speakers):
                out[l["zh"]] = ko
                self.counts["both" if drafts else "gemini"] += 1
            elif drafts and not self._problems(l, drafts[l["id"]], speakers):
                out[l["zh"]] = drafts[l["id"]]      # Gemini 가 두 번 다 틀리면 용어가 맞는 초벌을 쓴다
                self.counts["draft"] += 1
            elif ko:
                out[l["zh"]] = ko
                self.counts["both" if drafts else "gemini"] += 1
        return out

    def _ask(self, prompt: str) -> str:
        """Gemini 에 묻는다. 붐비면(stop 아닌 오류) BUSY_WAITS 만큼씩 쉬었다가 다시, 다 기다려도 안 되면 멈춘다."""
        for wait in (*BUSY_WAITS, None):
            try:
                return ask(self.gemini, prompt, STORY_SYSTEM)[0]
            except GeminiError as exc:
                if exc.stop or wait is None:
                    raise
                print(f"  Gemini 가 계속 {exc} — {wait // 60}분 쉬었다가 같은 줄을 다시 보냅니다.", flush=True)
                time.sleep(wait)
        raise GeminiError("다시 보내기 실패", stop=False)

    def _drafts(self, lines: list, speakers: set[str]) -> dict[int, str] | None:
        if self.deepl is None:
            return None
        chars = sum(len(l["zh"]) for l in lines)
        if self.budget is not None and self.spent + chars > self.budget:
            if self.stop_at_limit:
                raise DeepLLimit("DeepL 쓸 몫을 다 썼습니다")
            print("  DeepL 쓸 몫을 다 써서 여기부터는 Gemini 혼자 옮깁니다.")
            self.deepl = None
            return None
        self.spent += chars
        try:
            results = translate_batch(self.glossary, self.deepl, [l["zh"] for l in lines], speakers)
        except mt.QuotaExhausted:
            if self.stop_at_limit:
                raise DeepLLimit("DeepL 한도를 다 썼습니다 (456)") from None
            print("  DeepL 한도를 다 써서 여기부터는 Gemini 혼자 옮깁니다.")
            self.deepl = None
            return None
        return {l["id"]: ko for l, (ko, _label) in zip(lines, results)}

    def _problems(self, line: dict, ko: str, speakers: set[str]) -> list[str]:
        if not ko.strip():
            return ["빈칸"]
        out = []
        if not build_pack.usable_translation(ko):
            out.append("한자 남음")
        out += [f"{zh}={name}" for zh, name in self.glossary.found(line["zh"], speakers).items() if name not in ko]
        return out

    def _context(self, rows: list, first_idx: int, done: dict) -> list[dict]:
        """번역할 줄 바로 앞의 줄들. 공식 번역이나 이번에 옮긴 번역을 같이 보여 준다."""
        out = []
        for _idx, _kind, speaker, speaker_ko, zh, ko in [r for r in rows if r[0] < first_idx][-CONTEXT:]:
            text = clean(zh)
            if tn.has_cjk(text):
                mine = done.get(tn.key(text), ("", ""))[1]
                out.append({"speaker": speaker or "", "speaker_ko": speaker_ko or "", "zh": text,
                            "ko": tn.display_ko(ko) if ko else mine})
        return out

    def _speaker_examples(self, speakers: set[str], names: dict[str, str]) -> dict:
        missing = {s for s in speakers if s not in self.examples}
        if missing:
            self.examples.update(self.db.speaker_examples(missing, EXAMPLES))
        return {s: (self.examples[s][0] or names.get(s, ""), self.examples[s][1]) for s in sorted(speakers)}


def file_order(db) -> list[int]:
    """공식 번역이 없는 스토리가 있는 파일. 최근 이벤트부터 (묶음 정렬값), 묶음이 없으면 뒤로."""
    tags = db.story_tags()
    sort = dict(db._db().execute("SELECT tag, sort FROM groups").fetchall())
    files = [r[0] for r in db._db().execute(
        "SELECT DISTINCT file_id FROM story WHERE ko IS NULL AND kind IN ('say','option','caption')")]
    return sorted(files, key=lambda f: (-(sort.get(tags.get(f), 0) or 0), f))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="스토리 번역 묶음 만들기 (DeepL 초벌 → Gemini)")
    ap.add_argument("--out", required=True, help="묶음 폴더 (번역 저장소)")
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "official.sqlite"))
    ap.add_argument("--file", type=int, default=0, help="이 스토리 파일 하나만 (시험용)")
    ap.add_argument("--limit-files", type=int, default=0, help="파일 수 상한 (시험용)")
    ap.add_argument("--print", action="store_true", help="번역을 보여 주기만 하고 묶음은 그대로 둔다")
    ap.add_argument("--no-draft", action="store_true", help="DeepL 초벌 없이 Gemini 혼자 옮긴다")
    ap.add_argument("--budget", type=int, default=0, help="DeepL 로 보낼 글자 수 상한 (0 이면 남은 한도에서 --reserve 를 뺀 만큼)")
    ap.add_argument("--reserve", type=int, default=50000, help="DeepL 남은 한도 가운데 게임 중 화면 번역용으로 남겨 둘 글자 수")
    ap.add_argument("--models", default=",".join(STORY_MODELS), help="쓸 Gemini 모델 (앞에서부터, 쉼표로)")
    ap.add_argument("--stop-at-deepl-limit", action="store_true",
                    help=f"DeepL 몫(--reserve 를 남긴 만큼)을 다 쓰면 Gemini 혼자 옮기지 않고 멈춘다 (끝 값 {build_pack.DEEPL_STOPPED})")
    args = ap.parse_args()
    gemini_key = os.environ.get("GEMINI_KEY", "")
    if not gemini_key:
        raise SystemExit("GEMINI_KEY 환경 변수가 없습니다.")

    db = OfficialDB(args.db)
    glossary = Glossary(db, MarkdownStore(os.path.join(ROOT, "data", "용어집.md"), GLOSSARY_HEADER))
    glossary.add_names(load_names(db))
    glossary.add_terms(db.rogue_names())
    # 공식 문장에서 늘 같은 한국어로 옮겨지는 이벤트·작전·스테이지·생명연산 이름 ('视相博物馆' = 폴리비전 박물관, tools/name_terms.py)
    import name_terms

    glossary.add_terms(name_terms.fixed_names(args.db).items())
    deepl, budget = None, None
    if not args.no_draft:
        deepl = build_pack.make_engine("deepl")
        budget = build_pack.spending_cap(deepl, args.budget, args.reserve)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    story = Story(db, glossary, Gemini(gemini_key, models), deepl, budget, args.stop_at_deepl_limit)

    done_all = build_pack.load_existing(args.out)
    done = done_all.setdefault("story", {})
    with open(os.path.join(args.out, pack.MANIFEST), encoding="utf-8") as f:
        manifest = json.load(f)
    before = {LABELS[k]: manifest.get("engines", {}).get(LABELS[k], 0) for k in LABELS}
    files = [args.file] if args.file else file_order(db)
    if args.limit_files:
        files = files[:args.limit_files]
    left = sum(len(story.lines(f, done)[1]) for f in files)
    print(f"스토리 파일 {len(files):,}개 · 번역할 줄 {left:,}")

    def save() -> None:
        if args.print:
            return
        engines = dict(manifest.get("engines", {}))
        engines.update({LABELS[k]: before[LABELS[k]] + story.counts[k] for k in LABELS if
                        before[LABELS[k]] + story.counts[k]})
        build_pack.write(args.out, done_all, manifest.get("game_revision", ""), engines)

    finished = 0
    code = 0
    started = time.time()
    try:
        for n, file_id in enumerate(files, 1):
            rows, todo = story.lines(file_id, done)
            if not todo:
                continue
            path = db.file_path(file_id).rsplit("/", 1)[-1]
            for start in range(0, len(todo), BATCH):
                out = story.translate(rows, todo[start:start + BATCH], done)
                for zh, ko in out.items():
                    if args.print:
                        print(f"  {zh[:40]} → {ko}")
                    done[tn.key(zh)] = (zh, ko)
                finished += len(out)
            rate = finished / max(1e-6, time.time() - started) * 60
            print(f"  [{n}/{len(files)}] {path} {len(todo)}줄 · 누계 {finished:,}/{left:,} ({rate:.0f}줄/분) {story.counts}")
            save()
            if story.deepl is not None and args.reserve:
                # DeepL 은 이름 자리표까지 세어 더 깎는다. 실제 남은 한도로 한 번 더 막는다
                try:
                    quota = build_pack.quota_left(story.deepl)
                except Exception:
                    quota = None
                if quota is not None and quota <= args.reserve:
                    if args.stop_at_deepl_limit:
                        raise DeepLLimit(f"DeepL 남은 한도가 {quota:,}자입니다")
                    print(f"  DeepL 남은 한도가 {quota:,}자라 여기부터는 Gemini 혼자 옮깁니다.")
                    story.deepl = None
    except DeepLLimit as exc:
        save()      # 파일 중간에서 멈췄으면 그 파일의 앞 묶음까지 남긴다
        print(f"{exc}. Gemini 혼자 옮기지 않고 멈춥니다. 지금까지 옮긴 줄은 저장했습니다 (남은 {left - finished:,}줄).")
        code = build_pack.DEEPL_STOPPED
    except GeminiError as exc:
        save()
        print(f"Gemini 가 {exc} 멈춥니다. 지금까지 옮긴 줄은 저장했습니다. 다시 돌리면 이어서 합니다.")
        code = build_pack.GEMINI_STOPPED
    except KeyboardInterrupt:
        save()
        print("멈춤. 지금까지 옮긴 줄은 저장했습니다.")
    print(f"옮긴 줄 {finished:,} · {story.counts} · DeepL 로 보낸 글자 {story.spent:,}")
    return code


if __name__ == "__main__":
    sys.exit(main())
