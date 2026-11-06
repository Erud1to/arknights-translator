"""공식 번역이 없는 문장의 기계 번역.

구글 무료 경로 두 개를 번갈아 쓴다. 공식 API 가 아니라서 IP 단위로 막히는 일이 있어,
429 를 받은 경로는 잠시 쉬게 한다. 키를 넣으면 Papago / DeepL 을 쓴다.
"""
from __future__ import annotations

import html
import re
import time

import requests

API_URL = "https://translate.googleapis.com/translate_a/single"
M_URL = "https://translate.google.com/m"
_RESULT_RE = re.compile(r'class="result-container"\s*>(.*?)</div>', re.S)
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
COOLDOWN = 600

FAILED = "[번역 실패]"


class GoogleFree:
    label = "구글 번역"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": _UA})
        self._blocked: dict[str, float] = {}

    def translate(self, text: str) -> str:
        errors = []
        for name, fn in (("api", self._via_api), ("m", self._via_m)):
            if time.time() < self._blocked.get(name, 0.0):
                continue
            try:
                out = fn(text)
                if out:
                    return out
            except requests.HTTPError as exc:
                code = exc.response.status_code if exc.response is not None else 0
                if code in (429, 403):
                    self._blocked[name] = time.time() + COOLDOWN
                errors.append(f"{name}:{code}")
            except Exception as exc:
                errors.append(f"{name}:{type(exc).__name__}")
        raise RuntimeError(",".join(errors) or "경로 전부 대기 중")

    def _via_api(self, text: str) -> str:
        resp = self.session.get(
            API_URL,
            params={"client": "gtx", "sl": "zh-CN", "tl": "ko", "dt": "t", "q": text},
            timeout=7,
        )
        resp.raise_for_status()
        data = resp.json()
        return "".join(seg[0] for seg in (data[0] or []) if seg and seg[0]).strip()

    def _via_m(self, text: str) -> str:
        resp = self.session.get(M_URL, params={"sl": "zh-CN", "tl": "ko", "q": text}, timeout=8)
        resp.raise_for_status()
        m = _RESULT_RE.search(resp.text)
        if not m:
            return ""
        return html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()


class Papago:
    label = "Papago"
    URL = "https://papago.apigw.ntruss.com/nmt/v1/translation"

    def __init__(self, client_id: str, client_secret: str):
        self.headers = {
            "X-NCP-APIGW-API-KEY-ID": client_id,
            "X-NCP-APIGW-API-KEY": client_secret,
        }

    def translate(self, text: str) -> str:
        resp = requests.post(
            self.URL, headers=self.headers,
            data={"source": "zh-CN", "target": "ko", "text": text}, timeout=8,
        )
        resp.raise_for_status()
        return resp.json()["message"]["result"]["translatedText"].strip()


class DeepL:
    label = "DeepL"

    def __init__(self, key: str):
        # 무료 키는 ':fx' 로 끝나고 주소가 따로 있다
        host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
        self.base = f"https://{host}/v2"
        self.headers = {"Authorization": f"DeepL-Auth-Key {key}"}

    def translate(self, text: str) -> str:
        resp = requests.post(
            f"{self.base}/translate", headers=self.headers,
            data={"text": text, "source_lang": "ZH", "target_lang": "KO"}, timeout=8,
        )
        resp.raise_for_status()
        return resp.json()["translations"][0]["text"].strip()

    def translate_many(self, texts: list[str]) -> list[str]:
        """한 요청에 여러 줄 (최대 50줄). 미리 번역할 때 요청 수를 줄인다."""
        data = [("text", t) for t in texts] + [("source_lang", "ZH"), ("target_lang", "KO")]
        resp = requests.post(f"{self.base}/translate", headers=self.headers, data=data, timeout=30)
        resp.raise_for_status()
        out = [t["text"].strip() for t in resp.json()["translations"]]
        if len(out) != len(texts):
            raise ValueError("DeepL 이 보낸 줄 수와 다르게 답했습니다")
        return out

    def usage(self) -> tuple[int, int]:
        """(이번 결제 기간에 쓴 글자 수, 한도). 키가 맞는지 확인하는 데도 쓴다."""
        resp = requests.get(f"{self.base}/usage", headers=self.headers, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        return int(data["character_count"]), int(data["character_limit"])


# 이 응답을 받으면 그 번역기를 이번 실행 동안 더 부르지 않는다.
# DeepL 문서: 456 은 할당량 소진이라 다시 시도하면 안 된다.
STOP_REASONS = {456: "한도를 다 써서", 403: "키가 맞지 않아", 401: "키가 맞지 않아"}
QUOTA = 456


class QuotaExhausted(Exception):
    """한도를 다 썼다. 사용자가 새 키를 넣거나 구글 번역을 고를 때까지 번역을 멈춘다."""


class Chain:
    """고른 번역기가 막히면 구글 번역으로 넘어간다. 단, 한도 소진은 사용자에게 먼저 묻는다.

    한도 소진이면 알림을 띄우고 고를 때까지 멈춘다 (DeepL API Developer 는 다 쓰면 초기화되지
    않아 새 키를 받아야 한다). 틀린 키면 이번 실행 동안 구글만 쓰고, 일시적인 장애(429, 5xx,
    연결 끊김)면 그 문장만 구글로 번역한다.
    """

    def __init__(self, primary, log=None, on_quota=None):
        self.primary = primary
        self.fallback = GoogleFree()
        self.log = log or (lambda msg: None)
        self.on_quota = on_quota or (lambda chain: None)
        self.stopped = ""
        self.waiting = False             # 한도 소진 뒤 사용자가 고르기를 기다리는 중
        self.usage: tuple[int, int] | None = None

    @property
    def label(self) -> str:
        if self.waiting:
            return "번역 멈춤 (한도 소진)"
        if self.primary is None or self.stopped:
            return self.fallback.label
        return self.primary.label

    def quota_hit(self, usage: tuple[int, int] | None = None) -> None:
        """한도 소진을 알게 됐다 (번역 중이든, 키를 확인하다가든). 한 번만 알린다."""
        if self.waiting or self.stopped:
            return
        self.stopped = STOP_REASONS[QUOTA]
        self.waiting = True
        self.usage = usage
        if self.usage is None and hasattr(self.primary, "usage"):
            try:
                self.usage = self.primary.usage()
            except (requests.RequestException, KeyError, ValueError):
                pass
        self.on_quota(self)

    def continue_with_fallback(self) -> None:
        """사용자가 '구글 번역으로 계속' 을 골랐다."""
        self.waiting = False
        self.log(f"{self.primary.label if self.primary else '번역기'} 한도를 다 써서 구글 번역을 씁니다.")

    def run(self, text: str) -> tuple[str, str]:
        """(번역문, 실제로 번역한 번역기 이름)."""
        if self.waiting:
            raise QuotaExhausted()
        if self.primary is not None and not self.stopped:
            try:
                return self.primary.translate(text), self.primary.label
            except requests.HTTPError as exc:
                code = exc.response.status_code if exc.response is not None else 0
                if code == QUOTA:
                    self.quota_hit()
                    raise QuotaExhausted() from exc
                reason = STOP_REASONS.get(code)
                if reason:
                    self.stopped = reason
                    self.log(f"{self.primary.label} {reason} 구글 번역으로 바꿨습니다.")
            except (requests.RequestException, KeyError, ValueError):
                pass
        return self.fallback.translate(text), self.fallback.label

    @property
    def batch(self) -> int:
        """한 번에 보낼 수 있는 줄 수. 지금 쓰는 번역기가 여러 줄을 못 받으면 1."""
        usable = self.primary is not None and not self.stopped and not self.waiting
        return 25 if usable and hasattr(self.primary, "translate_many") else 1

    def run_many(self, texts: list[str]) -> list[tuple[str, str]]:
        """미리 번역용. 여러 줄을 받는 번역기면 한 번에 보내고, 일시적인 장애는 구글로 넘기지 않고
        그대로 알린다 (미리 번역은 서두를 일이 없으니 잠시 뒤 다시 보낸다)."""
        if self.waiting:
            raise QuotaExhausted()
        if len(texts) == 1 or self.batch == 1:
            return [self.run(t) for t in texts]
        try:
            return [(t, self.primary.label) for t in self.primary.translate_many(texts)]
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            if code == QUOTA:
                self.quota_hit()
                raise QuotaExhausted() from exc
            reason = STOP_REASONS.get(code)
            if reason:
                self.stopped = reason
                self.log(f"{self.primary.label} {reason} 구글 번역으로 바꿨습니다.")
            raise


def create(cfg: dict, log=None, on_quota=None) -> Chain:
    engine = cfg.get("engine", "google")
    primary = None
    if engine == "papago" and cfg.get("papago_id") and cfg.get("papago_secret"):
        primary = Papago(cfg["papago_id"], cfg["papago_secret"])
    elif engine == "deepl" and cfg.get("deepl_key"):
        primary = DeepL(cfg["deepl_key"])
    return Chain(primary, log, on_quota)
