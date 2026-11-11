"""프로그램 판 번호와 새 판 알림.

새 판을 낼 때 VERSION 을 올리고, GitHub Releases 에 같은 이름의 태그('v0.9.0')로 zip 을 올린다.
REPO 를 정해 두면 켤 때 Releases 의 최신 판과 견줘 데이터 탭에 알린다 (받기는 사용자가 브라우저에서).
"""
from __future__ import annotations

import re

VERSION = "0.9.4"
# 프로그램을 올릴 GitHub 저장소 'owner/name'. 비어 있으면 새 판을 찾지 않는다 (config.json 의 app_repo 가 먼저)
REPO = "Erud1to/arknights-translator"
LATEST_URL = "https://api.github.com/repos/{repo}/releases/latest"
USER_AGENT = f"ArknightsTranslatorOverlay/{VERSION}"


def parse(tag: str) -> tuple[int, ...] | None:
    """'v1.2.0' · '1.2' → (1, 2, 0) · (1, 2). 숫자 판이 아니면 None."""
    m = re.fullmatch(r"\s*v?(\d+(?:\.\d+)*)\s*", tag or "")
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def is_newer(tag: str, current: str = VERSION) -> bool:
    new, now = parse(tag), parse(current)
    if new is None or now is None:
        return False
    width = max(len(new), len(now))
    return new + (0,) * (width - len(new)) > now + (0,) * (width - len(now))


def latest(repo: str, timeout: float = 10.0) -> tuple[str, str] | None:
    """(태그, 릴리스 화면 주소). 지금 판보다 새 판일 때만. 저장소가 없거나 연결이 안 되면 None."""
    if not repo or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        return None
    import requests

    try:
        resp = requests.get(LATEST_URL.format(repo=repo), timeout=timeout,
                            headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT})
        resp.raise_for_status()
        data = resp.json()
        tag, url = str(data.get("tag_name", "")), str(data.get("html_url", ""))
    except (requests.RequestException, ValueError, AttributeError):
        return None
    if not url.startswith("https://github.com/") or not is_newer(tag):
        return None
    return tag, url
