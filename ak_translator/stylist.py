"""한국 서버에 없는 스토리를 Gemini 로 번역한다. 인물마다 공식 번역의 말투를 예시로 보여 준다.

기계 번역기는 문장 하나만 보고 옮기므로 반말·존댓말과 어투를 모른다. 여기서는 스토리 파일을 통째로
보내면서, 그 파일에 나오는 인물들의 한국 서버 공식 대사(중국어 원문과 짝)를 예시로 함께 보낸다.
무료 요청 횟수가 적을 수 있어 대사 한 줄씩이 아니라 파일 하나(최대 BATCH 줄)를 한 번에 보낸다.

요청은 generateContent 를 쓴다. Gemini 3.6 이상은 temperature 같은 값을 무시하므로 넣지 않는다.
"""
from __future__ import annotations

import json
import re

import requests

API = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODELS = ("gemini-3.8-flash", "gemini-3.5-flash-lite")
BATCH = 150        # 한 번에 보낼 대사 수. 대부분의 스토리 파일이 한 번에 끝난다
EXAMPLES = 6       # 인물마다 보여 줄 공식 대사 수
CONTEXT = 8        # 앞 장면으로 보여 줄 줄 수
TIMEOUT = 180

SYSTEM = """너는 모바일 게임 명일방주(Arknights)의 중국어 스토리 대본을 한국어로 옮기는 번역가다.
한국 서버의 공식 번역과 구분되지 않게 옮기는 것이 목표다.

규칙
1. [용어집]의 이름과 용어는 반드시 그 표기 그대로 쓴다.
2. 인물의 말투(반말·존댓말, 어미, 호칭, 1인칭)는 [인물별 공식 번역 예시]를 따른다. 예시가 없는 인물은
   장면과 관계에 맞게 정하고, 같은 장면 안에서는 바꾸지 않는다.
3. 지문(화자 없음)은 [지문 예시]처럼 간결한 서술체로 옮긴다.
4. 직역하지 말고 한국어 게임 대사로 자연스럽게 옮긴다. 다만 내용을 빼거나 더하지 않는다.
5. 줄을 합치거나 나누지 않는다. 입력 번호마다 정확히 한 줄씩 낸다.
6. 말줄임표는 '…', 문장 부호는 원문의 느낌을 살린다.
7. 설명이나 주석을 붙이지 않는다.

출력은 JSON 하나만: {"lines": [{"id": 번호, "ko": "번역"}, ...]}"""


class GeminiError(Exception):
    def __init__(self, message: str, stop: bool):
        super().__init__(message)
        self.stop = stop    # True 면 이번 실행 동안 더 부르지 않는다


class Gemini:
    label = "Gemini"

    def __init__(self, key: str, models=DEFAULT_MODELS):
        self.headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
        self.models = list(models)
        self.exhausted: set[str] = set()   # 오늘 무료 한도를 다 쓴 모델

    def list_models(self) -> set[str]:
        """키 확인용. 한도를 쓰지 않는다."""
        names: set[str] = set()
        url = f"{API}/models?pageSize=200"
        resp = requests.get(url, headers=self.headers, timeout=15)
        resp.raise_for_status()
        for model in resp.json().get("models", []):
            names.add(model.get("name", "").removeprefix("models/"))
        return names

    def generate(self, system: str, prompt: str) -> tuple[str, str]:
        """(응답 글자, 쓴 모델). 앞 모델이 한도에 걸리면 다음 모델로 넘어간다."""
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        busy = ""
        for model in self.models:
            if model in self.exhausted:
                continue
            try:
                resp = requests.post(f"{API}/models/{model}:generateContent",
                                     headers=self.headers, json=body, timeout=TIMEOUT)
            except requests.RequestException as exc:
                raise GeminiError(f"연결 실패 ({type(exc).__name__})", stop=False) from exc
            if resp.status_code == 200:
                return _text_of(resp.json()), model
            reason = _reason(resp)
            if resp.status_code in (429, 404):
                # 429: 그 모델의 무료 한도 소진. 404: 이 계정에서 쓸 수 없는 모델
                self.exhausted.add(model)
                continue
            if resp.status_code in (500, 503):
                # 그 모델만 잠깐 붐빈다 (503 UNAVAILABLE). 다음 모델로 넘어가되 한도 소진으로 치지는 않는다
                busy = f"{resp.status_code} {reason}"
                continue
            if resp.status_code in (401, 403) or "API_KEY" in reason:
                raise GeminiError("키가 맞지 않아", stop=True)
            raise GeminiError(f"요청 실패 ({resp.status_code} {reason})", stop=resp.status_code == 400)
        if busy:
            raise GeminiError(f"요청 실패 ({busy})", stop=False)
        raise GeminiError("오늘 무료 한도를 다 써서", stop=True)


def _reason(resp) -> str:
    try:
        err = resp.json()
        err = err[0] if isinstance(err, list) else err
        info = err.get("error", {})
        details = [d.get("reason", "") for d in info.get("details", []) if isinstance(d, dict)]
        return " ".join(filter(None, [info.get("status", "")] + details))
    except ValueError:
        return ""


def _text_of(data: dict) -> str:
    """응답에서 글자만 모은다. 생각 과정(thought) 조각은 뺀다."""
    out = []
    for cand in data.get("candidates", [])[:1]:
        for part in (cand.get("content") or {}).get("parts", []):
            if part.get("text") and not part.get("thought"):
                out.append(part["text"])
    return "".join(out)


# ------------------------------------------------------------------ 요청 만들기
def build_prompt(lines: list[dict], context: list[dict], examples: dict, narration: list,
                 glossary: dict[str, str]) -> str:
    """lines/context: {"id", "speaker", "speaker_ko", "zh", "ko"}. examples: {화자: (한국 이름, [(중, 한)])}."""
    parts = ["[용어집]"]
    parts += [f"- {zh} → {ko}" for zh, ko in sorted(glossary.items())] or ["(없음)"]

    parts += ["", "[인물별 공식 번역 예시]"]
    if examples:
        for speaker, (name_ko, pairs) in examples.items():
            parts.append(f"◆ {speaker} ({name_ko or '공식 이름 없음'})")
            if pairs:
                for zh, ko in pairs:
                    parts += [f"  中: {zh}", f"  韓: {ko}"]
            else:
                parts.append("  (한국 서버 대사 없음 — 장면에 맞게 정할 것)")
    else:
        parts.append("(없음)")

    parts += ["", "[지문 예시]"]
    for zh, ko in narration:
        parts += [f"  中: {zh}", f"  韓: {ko}"]

    if context:
        parts += ["", "[앞 장면 — 참고만 하고 번역하지 말 것]"]
        for line in context:
            who = line["speaker_ko"] or line["speaker"] or "지문"
            parts.append(f"{who}: {line['zh']}" + (f"  → {line['ko']}" if line.get("ko") else ""))

    parts += ["", "[번역할 대사]  번호|화자|원문"]
    for line in lines:
        who = line["speaker"] or ("선택지" if line.get("kind") == "option" else "지문")
        parts.append(f"{line['id']}|{who}|{line['zh']}")
    return "\n".join(parts)


def parse(text: str, ids: set[int]) -> dict[int, str]:
    """모델 응답에서 번호별 번역을 꺼낸다. 형식이 깨진 줄은 버린다 (그 줄은 일반 번역기로 간다)."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return {}
    rows = data.get("lines") if isinstance(data, dict) else data
    out: dict[int, str] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            i = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        ko = row.get("ko")
        if i in ids and isinstance(ko, str) and ko.strip() and i not in out:
            out[i] = ko.strip()
    return out
