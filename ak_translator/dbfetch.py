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


def _get(url: str, stream: bool = False):
    import requests

    if not url.startswith("https://") and not url.startswith("http://127.0.0.1"):
        raise FetchError("https 주소가 아닙니다")
    try:
        resp = requests.get(url, stream=stream, timeout=TIMEOUT, headers={"User-Agent": version.USER_AGENT})
        resp.raise_for_status()
        return resp
    except requests.RequestException as exc:
        raise FetchError(f"받기 실패: {exc}") from exc


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
    os.makedirs(data_dir, exist_ok=True)
    zinfo, dinfo = info["zip"], info.get("db") or {}
    total = int(zinfo.get("size", 0))
    part = os.path.join(data_dir, ZIP_NAME + ".part")
    resp = _get(base + ZIP_NAME, stream=True)
    h = hashlib.sha256()
    got = 0
    try:
        with open(part, "wb") as f:
            for chunk in resp.iter_content(CHUNK):
                if cancelled and cancelled():
                    raise FetchError("멈춤")
                f.write(chunk)
                h.update(chunk)
                got += len(chunk)
                if progress:
                    progress(got, total, "받는 중")
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
        resp.close()
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
