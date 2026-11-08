"""배포자가 만들어 올린 공식 번역 DB 받기. 사용자 PC 에서 게임 데이터(약 500MB, Git 필요)를 받아 10분 동안 만들지 않는다.

배포자가 `tools/publish_db.py` 로 만든 두 파일을 프로그램 저장소의 GitHub 릴리스 'db' 에 올려 둔다:
  official-db.json   판 정보 (만든 날, 게임 데이터 커밋, zip·DB 크기와 sha256)
  official-db.zip    official.sqlite 한 개 (LZMA)
받은 zip 과 푼 DB 는 sha256 을 견주고, data/official.sqlite.new 로 둔다. 번역기가 켜질 때 바꿔 끼운다 (app.main).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile

from . import version

INFO_NAME = "official-db.json"
ZIP_NAME = "official-db.zip"
DB_MEMBER = "official.sqlite"
RELEASE_TAG = "db"
CHUNK = 1 << 20
TIMEOUT = 20
RETRIES = 1            # 5xx·연결 오류면 이만큼 더 해 본다
RETRY_WAIT = 1.5       # 다시 해 보기 전에 쉬는 시간 (초)
# zip 을 받다가 끊기면 받은 데서부터 이어 받기를 이만큼 해 본다. 2026-11-08 시험에서 122MB 를 받는 동안 GitHub 쪽이
# 저절로 두 번 끊었다 (일부러 끊은 한 번 말고)
RESUMES = 8
# github.com 릴리스 받기 주소 → GitHub API 로 같은 파일 받기 (_get 설명)
_RELEASE_URL = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/releases/download/([^/]+)/([^/?#]+)$")
API_RELEASE = "https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"
_via_api: dict[str, str] = {}   # 직접 주소가 안 돼 API 로 받은 주소 → 그 API 주소 (이어 받기 때 바로 쓴다)


class FetchError(Exception):
    pass


def base_url(cfg: dict) -> str:
    """받을 곳. config.json 의 db_url 이 먼저, 없으면 프로그램 저장소(app_repo / version.REPO)의 릴리스 'db'."""
    url = (cfg.get("db_url") or "").strip()
    if url:
        return url if url.endswith("/") else url + "/"
    repo = (cfg.get("app_repo") or version.REPO or "").strip()
    if re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        return f"https://github.com/{repo}/releases/download/{RELEASE_TAG}/"
    return ""


def _get(url: str, stream: bool = False, start: int = 0):
    """url 을 받는다. 안 되면 FetchError.

    github.com 릴리스 받기 주소(…/releases/download/…)는 2026-11-08 GitHub 한국 쪽 서버(koreacentral)에서 504 를
    자주 내 배포판이 첫 실행에 공식 데이터를 못 받았다. 다른 저장소(cli/cli)도 같았고, GitHub API 로 같은 파일을
    받는 길은 그때도 됐다. 그래서 한 번 더 해 보고, 그래도 안 되면 API 로 받는다.
    start 를 주면 그 바이트부터 달라고 한다 (이어 받기). 서버가 들어주면 206, 아니면 200 으로 처음부터 온다."""
    if not url.startswith("https://") and not url.startswith("http://127.0.0.1"):
        raise FetchError("https 주소가 아닙니다")
    if url in _via_api:
        try:
            return _request(_via_api[url], stream, "application/octet-stream", start)
        except FetchError:
            pass
    try:
        return _request(url, stream, start=start)
    except FetchError:
        m = _RELEASE_URL.match(url)
        if not m:
            raise
        try:
            api = _asset_api_url(*m.groups())
            resp = _request(api, stream, "application/octet-stream", start)
        except FetchError:
            pass
        else:
            _via_api[url] = api
            return resp
        raise


def _request(url: str, stream: bool, accept: str = "", start: int = 0):
    import time

    import requests

    headers = {"User-Agent": version.USER_AGENT}
    if accept:
        headers["Accept"] = accept
    if start:
        headers["Range"] = f"bytes={start}-"
    for attempt in range(RETRIES + 1):
        last = attempt == RETRIES
        try:
            resp = requests.get(url, stream=stream, timeout=TIMEOUT, headers=headers)
            if resp.status_code >= 500 and not last:
                resp.close()
                time.sleep(RETRY_WAIT)
                continue
            resp.raise_for_status()
            return resp
        except (requests.ConnectionError, requests.Timeout) as exc:
            if last:
                raise FetchError(f"받기 실패: {exc}") from exc
            time.sleep(RETRY_WAIT)
        except requests.RequestException as exc:
            raise FetchError(f"받기 실패: {exc}") from exc
    raise FetchError("받기 실패")


def _asset_api_url(owner: str, repo: str, tag: str, name: str) -> str:
    """릴리스 자산을 API 로 받을 주소 (Accept: application/octet-stream 으로 부르면 파일이 온다)."""
    resp = _request(API_RELEASE.format(owner=owner, repo=repo, tag=tag), False, "application/vnd.github+json")
    try:
        assets = resp.json().get("assets") or []
    except (ValueError, AttributeError) as exc:
        raise FetchError("릴리스 정보를 읽지 못했습니다") from exc
    for asset in assets:
        url = str(asset.get("url") or "")
        if asset.get("name") == name and url.startswith("https://api.github.com/"):
            return url
    raise FetchError(f"릴리스에 {name} 이 없습니다")


def remote_info(base: str) -> dict | None:
    """올라가 있는 판 정보. 받을 곳이 없거나 연결이 안 되면 None."""
    if not base:
        return None
    try:
        info = _get(base + INFO_NAME).json()
    except (FetchError, ValueError):
        return None
    if not isinstance(info, dict) or info.get("format") != 1 or not isinstance(info.get("zip"), dict):
        return None
    return info


def is_newer(info: dict, meta: dict) -> bool:
    """올라간 DB 가 지금 DB 와 다른 판인가 (게임 데이터 커밋 또는 만든 날이 다르다)."""
    return (info.get("revision") or "") != (meta.get("revision") or "") or \
        (info.get("built_at") or "") > (meta.get("built_at") or "")


def size_mb(info: dict) -> int:
    return round(int(info["zip"].get("size", 0)) / 1e6)


def download(base: str, info: dict, data_dir: str, progress=None, cancelled=None) -> str:
    """zip 을 받아 검사하고 data/official.sqlite.new 로 푼다. progress(받은 바이트, 전체 바이트, 단계).
    cancelled() 가 True 를 돌려주면 멈춘다. 돌려주는 값: .new 파일 경로."""
    import time

    import requests

    os.makedirs(data_dir, exist_ok=True)
    zinfo, dinfo = info["zip"], info.get("db") or {}
    total = int(zinfo.get("size", 0))
    part = os.path.join(data_dir, ZIP_NAME + ".part")
    h = hashlib.sha256()
    got = 0
    try:
        with open(part, "wb") as f:
            for attempt in range(RESUMES + 1):
                resp = _get(base + ZIP_NAME, stream=True, start=got)
                try:
                    if got and resp.status_code != 206:     # 이어 받기를 안 들어줬다. 처음부터 다시 받는다
                        f.seek(0)
                        f.truncate()
                        h, got = hashlib.sha256(), 0
                    for chunk in resp.iter_content(CHUNK):
                        if cancelled and cancelled():
                            raise FetchError("멈춤")
                        f.write(chunk)
                        h.update(chunk)
                        got += len(chunk)
                        if progress:
                            progress(got, total, "받는 중")
                    break
                except requests.RequestException as exc:      # 받다가 끊겼다. 받은 데서부터 이어 받는다
                    if attempt == RESUMES:
                        raise FetchError(f"받다가 끊겼습니다 ({got / 1e6:.0f}MB 받음): {exc}") from exc
                    time.sleep(RETRY_WAIT)
                finally:
                    resp.close()
        if (total and got != total) or h.hexdigest() != zinfo.get("sha256"):
            raise FetchError("받은 파일이 올라간 파일과 다릅니다 (크기·sha256). 다시 받아 주세요.")
        target = os.path.join(data_dir, DB_MEMBER + ".new")
        tmp = target + ".part"
        h = hashlib.sha256()
        done = 0
        size = int(dinfo.get("size", 0))
        with zipfile.ZipFile(part) as z, z.open(DB_MEMBER) as src, open(tmp, "wb") as dst:
            for chunk in iter(lambda: src.read(CHUNK), b""):
                if cancelled and cancelled():
                    raise FetchError("멈춤")
                dst.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, size, "푸는 중")
        if dinfo.get("sha256") and h.hexdigest() != dinfo["sha256"]:
            os.remove(tmp)
            raise FetchError("푼 DB 가 올라간 DB 와 다릅니다 (sha256).")
        os.replace(tmp, target)
        return target
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise FetchError(f"받은 파일을 쓰거나 풀지 못했습니다: {exc}") from exc
    finally:
        try:
            os.remove(part)
        except OSError:
            pass


def info_for(db_path: str, zip_path: str) -> dict:
    """배포자용: 만든 zip 과 DB 로 판 정보를 만든다 (tools/publish_db.py)."""
    import sqlite3

    def digest(path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(CHUNK), b""):
                h.update(chunk)
        return h.hexdigest()

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    meta = dict(con.execute("SELECT name, value FROM meta").fetchall())
    con.close()
    return {
        "format": 1,
        "built_at": meta.get("built_at", ""),
        "revision": meta.get("revision", ""),
        "texts_with_ko": int(meta.get("texts_with_ko", 0) or 0),
        "zip": {"name": ZIP_NAME, "size": os.path.getsize(zip_path), "sha256": digest(zip_path)},
        "db": {"size": os.path.getsize(db_path), "sha256": digest(db_path)},
    }


def write_info(info: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
