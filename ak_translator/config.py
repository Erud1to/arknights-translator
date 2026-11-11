"""설정. 바꾸는 즉시 config.json 에 저장된다."""
from __future__ import annotations

import json
import os
import sys

# exe 로 묶었으면(PyInstaller) exe 가 있는 폴더, 아니면 프로젝트 폴더. config.json 과 data/ 가 여기에 생긴다
FROZEN = bool(getattr(sys, "frozen", False))
ROOT = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPDATER_EXE = "UpdateData.exe"     # exe 판의 공식 데이터 갱신 프로그램 (update_data.py 를 묶은 것)
# 같이 배포하는 글꼴 (Pretendard, SIL OFL). exe 판은 _internal/fonts. 설치하지 않고 이 프로세스에서만 쓴다
FONT_DIR = os.path.join(getattr(sys, "_MEIPASS", ROOT), "fonts")
# PaddleOCR 모델 (ppocr.py). 배포판은 _internal/models
MODEL_DIR = os.path.join(getattr(sys, "_MEIPASS", ROOT), "models")
FONT_FAMILY = "Pretendard"
DATA_DIR = os.path.join(ROOT, "data")
CONFIG_PATH = os.path.join(ROOT, "config.json")

DEFAULTS = {
    "engine": "google",
    "papago_id": "",
    "papago_secret": "",
    "deepl_key": "",
    "gemini_style": False,
    "gemini_key": "",
    "gemini_models": ["gemini-3.8-flash", "gemini-3.5-flash-lite"],
    "nickname": "박사",
    "box_style": "blend",
    "font_family": FONT_FAMILY,
    "font_scale": 1.0,
    "max_font_size": 44,
    "min_font_size": 11,
    "interval_ms": 150,
    "settle_ms": 280,
    "max_wait_ms": 2500,
    "change_ratio": 0.03,
    "battle_pause": True,
    # 공개모집 화면에서 ★4 이상 확정 태그 조합을 테두리와 목록으로 보여 주기 (중국 서버 모집 목록, 9시간 기준)
    "recruit_hint": True,
    # 글자 인식: auto 는 GPU 가 있으면 PaddleOCR, 없으면 Windows OCR. windows / paddle 로 고정할 수 있다
    "ocr_engine": "auto",
    # 번역 묶음 배포처 (manifest.json 이 있는 주소). config.json 에 비어 있으면 이 기본값을 쓴다
    "pack_url": "https://raw.githubusercontent.com/Erud1to/ak-translations/main/",
    # 화면 글을 공식·번역 묶음 글에 맞추는 방식. index: 조각 색인(기본), rules: 옛 규칙, shadow: 옛 규칙으로 보이되 색인과 다른 결과를 기록
    "matcher": "index",
    # 게임 창 제목에 이 글자가 있으면 그 창을 게임으로 본다 (기본 탐색이 못 찾을 때 쓰는 대비책. 비우면 쓰지 않음)
    "game_title": "",
    "panel_geometry": "",
    # peek: 누르고 있는 동안 번역문을 숨겨 원문을 본다
    "hotkeys": {"translate": "ctrl+alt+t", "toggle": "ctrl+alt+h", "capture": "ctrl+alt+s",
                "quit": "ctrl+alt+q", "peek": "ctrl+alt+o"},
}


def load() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return cfg
    # 예전에 저장된 단축키 목록에는 새로 생긴 단축키가 없다. 덮어쓰지 않고 합친다
    hotkeys = {**cfg["hotkeys"], **(saved.pop("hotkeys", None) or {})}
    # 주소가 생기기 전에 저장된 설정에는 pack_url 이 빈 칸으로 들어 있다. 빈 칸은 '정하지 않음' 으로 보고 기본값을 쓴다
    for key in ("pack_url",):
        if not str(saved.get(key) or "").strip():
            saved.pop(key, None)
    cfg.update(saved)
    cfg["hotkeys"] = hotkeys
    return cfg


def save(cfg: dict) -> None:
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)
