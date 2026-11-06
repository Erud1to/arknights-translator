"""공식 번역이 없는 스토리 파일을 Gemini 로 통째로 번역해 번역캐시에 넣는다.

스토리를 읽기 시작하면(대사 하나가 그 파일에서 확인되면) 그 파일의 나머지 대사를 한 번에 보낸다.
답이 오기 전의 대사는 일반 번역기(DeepL·구글)가 먼저 띄우고, 답이 오면 그 줄들을 갈아 끼운다.
Gemini 가 막히면(한도·키) 이번 실행 동안 일반 번역기만 쓴다.
"""
from __future__ import annotations

import queue
import re
import threading

from . import textnorm as tn
from .stylist import BATCH, CONTEXT, EXAMPLES, SYSTEM, Gemini, GeminiError, build_prompt, parse

ORIGIN = "Gemini"
# 이 출처의 줄만 Gemini 번역으로 갈아 끼운다. 사용자가 고친 줄은 건드리지 않는다
MACHINE_ORIGINS = {"구글 번역", "DeepL", "Papago"}
NARRATION_EXAMPLES = 4


def clean(zh: str) -> str:
    zh = re.sub(r"(?:Dr\.?)?\{@nickname\}", "博士", tn.strip_rich(zh))
    return re.sub(r"\s+", " ", zh.replace("\\n", " ")).strip()


class StoryTranslator:
    def __init__(self, resolver, key: str, models):
        self.r = resolver
        self.gemini = Gemini(key, models)
        self.stopped = ""            # 막혀서 그만둔 이유
        self.state = "대기 중"
        self._queue: queue.Queue = queue.Queue()
        self._pending: set[int] = set()
        self._done: set[int] = set()
        self._examples: dict[str, tuple] = {}
        self._narration: list | None = None
        self._lock = threading.Lock()
        threading.Thread(target=self._run, name="gemini", daemon=True).start()

    def stop(self) -> None:
        self._queue.put(None)

    def request(self, file_id: int) -> bool:
        """이 스토리 파일을 맡을 수 있으면 True. 일반 번역기의 미리 번역은 건너뛰어도 된다."""
        if self.stopped:
            return False
        with self._lock:
            if file_id not in self._done and file_id not in self._pending:
                self._pending.add(file_id)
                self._queue.put(file_id)
        return True

    # ---------------------------------------------------------------- 일꾼
    def _run(self) -> None:
        while True:
            file_id = self._queue.get()
            if file_id is None:
                return
            try:
                self._translate_file(file_id)
                with self._lock:
                    self._done.add(file_id)
            except GeminiError as exc:
                if exc.stop:
                    self.stopped = str(exc)
                    self.state = f"{exc} 일반 번역기를 씁니다"
                    self.r.log(f"Gemini {exc} 일반 번역기로 바꿨습니다.")
                else:
                    self.state = f"실패: {exc}. 다음에 이 스토리를 읽을 때 다시 시도합니다"
            except Exception as exc:     # 응답 모양이 예상과 달라도 번역기 전체가 죽지 않게
                self.state = f"실패: {type(exc).__name__}: {exc}"
            finally:
                with self._lock:
                    self._pending.discard(file_id)

    def _translate_file(self, file_id: int) -> None:
        db, cache = self.r.db, self.r.cache
        rows = db.script(file_id)
        path = db.file_path(file_id).rsplit("/", 1)[-1]
        todo = []
        for idx, kind, speaker, speaker_ko, zh, ko in rows:
            text = clean(zh)
            if ko is not None or kind not in ("say", "option", "caption") or not tn.has_cjk(text):
                continue
            if self.r.pack.get(tn.key(text)):
                continue    # 번역 묶음에 이미 옮겨 둔 줄 (묶음이 번역캐시보다 먼저 쓰인다)
            hit = cache.get(tn.key(text))
            if hit and hit[1] not in MACHINE_ORIGINS:
                continue    # 이미 Gemini 로 번역했거나 사용자가 고친 줄
            todo.append({"idx": idx, "kind": kind, "speaker": speaker or "", "zh": text})
        if not todo:
            self.state = f"{path}: 번역할 대사 없음"
            return

        total, finished = len(todo), 0
        for start in range(0, total, BATCH):
            chunk = todo[start:start + BATCH]
            self.state = f"{path}: {finished}/{total}줄 번역 중"
            speakers = {line["speaker"] for line in chunk if line["speaker"]}
            names = self.r.glossary.found(" ".join(sorted(speakers)), speakers)
            lines = []
            for n, line in enumerate(chunk, 1):
                lines.append({**line, "id": n, "speaker_ko": names.get(line["speaker"], "")})
            text = "\n".join(l["zh"] for l in chunk) + "\n" + "\n".join(speakers)
            prompt = build_prompt(
                lines, self._context(rows, chunk[0]["idx"]), self._speaker_examples(speakers, names),
                self._narration_examples(), self.r.glossary.found(text, speakers),
            )
            reply, model = self.gemini.generate(SYSTEM, prompt)
            got = parse(reply, {l["id"] for l in lines})
            entries = [(l["zh"], got[l["id"]]) for l in lines if l["id"] in got]
            cache.replace(entries, ORIGIN, MACHINE_ORIGINS)
            finished += len(entries)
            self.state = f"{path}: {finished}/{total}줄 완료 ({model})"
            self.r.on_ready()

    # ---------------------------------------------------------------- 자료
    def _context(self, rows, first_idx: int) -> list[dict]:
        """번역할 대사 바로 앞의 줄들. 공식 번역이나 이미 번역된 것이 있으면 같이 보여 준다."""
        before = [r for r in rows if r[0] < first_idx][-CONTEXT:]
        out = []
        for _idx, _kind, speaker, speaker_ko, zh, ko in before:
            text = clean(zh)
            if not tn.has_cjk(text):
                continue
            done = tn.display_ko(ko) if ko else (self.r.cache.get(tn.key(text)) or ("",))[0]
            out.append({"speaker": speaker or "", "speaker_ko": speaker_ko or "", "zh": text, "ko": done})
        return out

    def _speaker_examples(self, speakers: set[str], names: dict[str, str]) -> dict:
        missing = {s for s in speakers if s not in self._examples}
        if missing:
            self._examples.update(self.r.db.speaker_examples(missing, EXAMPLES))
        out = {}
        for speaker in sorted(speakers):
            name, pairs = self._examples[speaker]
            out[speaker] = (name or names.get(speaker, ""), pairs)
        return out

    def _narration_examples(self) -> list:
        if self._narration is None:
            self._narration = self.r.db.narration_examples(NARRATION_EXAMPLES)
        return self._narration
