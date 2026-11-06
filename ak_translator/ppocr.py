"""PaddleOCR (검출 PP-OCRv5 mobile + 인식 PP-OCRv6 small) 로 화면 글자를 읽는다 (GPU 가 있을 때 Windows OCR 대신).

2026-11-05 저장 화면 43장 실측 (scratchpad bench_ocr.py): 게임 데이터로 확인된 한자 1,874 → 2,619,
Windows OCR 이 늘 틀리던 자리 48곳 중 17 → 43곳을 맞게 읽었다 (초록 칸 '自动回复', 파란 숫자 '490%',
반쪽으로 갈라 읽던 '棘刺'·'治疗'). 화면 한 장 0.6초(CUDA)·0.85초(DirectML), CPU 로는 3.6초라 GPU 가 없으면 쓰지 않는다.

ocr.WindowsOCR 와 같은 recognize(image) → [OCRLine] 을 낸다. 낱자 상자(words)는 CTC 출력의 시간 칸에서 얻는다.

  검출: BGR 을 그대로 (x/255 - mean)/std 해 DET_PAD 단위로 0 을 채운 판에 놓는다 (줄이지 않는다. 1440p 의
        작은 글자가 960 으로 줄이면 안 읽힌다) → 확률 지도 0.3 이진화 → 윤곽마다 최소 사각형 → 점수 0.6 미만
        버림 → 사각형을 넓이·둘레로 정한 거리만큼 넓힌다 (PaddleOCR 의 unclip 1.5 다음 최소 사각형과 같다)
  인식: 높이 48 로 비율 유지, (x/255 - 0.5)/0.5, 폭은 REC_STEP 단위·배치는 REC_BATCH 로 0 채움 → CTC 탐욕 디코드.
        게임 글에 없는 한자는 고르지 않는다 (번체 '奧' 대신 '奥' — MAA 가 정답 목록과 견주는 것과 같은 생각)
  같은 화면은 다시 인식하지 않는다: 잘라 낸 줄 그림이 지난번과 같으면 그 결과를 쓴다.
"""
from __future__ import annotations

import hashlib
import importlib.util
import math
import os
import re
import threading
import time
from collections import OrderedDict

import numpy as np
from PIL import Image

from .ocr import OCRLine

MODEL_SUBDIR = "ppocr"      # det.onnx (v5 mobile) · rec.onnx (v6 small) · rec_dict.txt · NOTICE.txt
# 상자 점수는 PaddleOCR 기본 0.6 보다 낮춘다. 상점의 그림 위 붉은 띠 글자 '寻访数据契约' 이 0.6 에서 빠졌다
DET_THRESH, BOX_THRESH, UNCLIP, MIN_SIZE = 0.3, 0.5, 1.5, 3
DET_PAD = 128           # 검출 판의 가로·세로를 이 단위로 올린다. DirectML 은 처음 보는 입력 크기마다 다시 준비해서(1~3초) 가짓수를 줄인다
REC_H, REC_STEP, REC_MAX_W, REC_BATCH = 48, 160, 3200, 8
CACHE_SIZE = 4096       # 기억해 둘 줄 그림 수
SLOW_LIMIT = 2.5        # 준비한 뒤 화면 한 장이 이보다 오래 걸리면 auto 에서는 Windows OCR 을 쓴다
# 이보다 확신이 낮은 줄은 버린다. 저장 화면 43장에서 0.65 아래는 모두 아이콘을 한 글자로 읽은 헛것이었다
# ('家' '乐' '中', 레벨 배지의 '茶' 가 공식 대사 '차 한잔 들거라.' 로 떴다). 여러 글자인 진짜 글은 0.7 아래가 없었다
MIN_CONF = 0.65
INK_COLOR_MIN = 48.0   # 줄 상자를 글자에 맞출 때 바탕색과 이 이상(RGB 거리) 다르면 글자로 친다 (_ink_box)
RARE_RATIO = 0.1        # 드문 한자를 골랐을 때 흔한 한자가 이 비율 넘게 닮았으면 흔한 쪽 ('換' → '换')
_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
_STD = np.array([0.229, 0.224, 0.225], np.float32)
_DOTS = re.compile(r"[…·]{2,}")


def model_dir(root: str) -> str:
    return os.path.join(root, MODEL_SUBDIR)


def available(root: str) -> bool:
    d = model_dir(root)
    return all(os.path.exists(os.path.join(d, f)) for f in ("det.onnx", "rec.onnx", "rec_dict.txt")) \
        and importlib.util.find_spec("onnxruntime") is not None and importlib.util.find_spec("cv2") is not None


def _cuda_dlls() -> None:
    """개발 PC 의 onnxruntime-gpu 는 CUDA 12 DLL(cublasLt 등)이 따로 없어 torch 에 든 것을 빌린다 (torch 를 import 하지는 않는다)."""
    spec = importlib.util.find_spec("torch")
    for base in (spec.submodule_search_locations or []) if spec else []:
        lib = os.path.join(base, "lib")
        if os.path.isdir(lib):
            # onnxruntime 이 CUDA 제공자 DLL 의 의존 DLL 을 찾을 때는 add_dll_directory 를 보지 않아 PATH 에도 넣는다
            os.add_dll_directory(lib)
            os.environ["PATH"] = lib + os.pathsep + os.environ.get("PATH", "")


def _sessions(folder: str, threads: int, cpu: bool = False):
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.log_severity_level = 3
    opts.intra_op_num_threads = threads
    avail = [] if cpu else ort.get_available_providers()
    if "DmlExecutionProvider" in avail:
        # DirectML 은 메모리 패턴 최적화·병렬 실행을 지원하지 않는다
        opts.enable_mem_pattern = False
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        providers, backend = ["DmlExecutionProvider", "CPUExecutionProvider"], "DirectML"
    elif "CUDAExecutionProvider" in avail:
        _cuda_dlls()
        # 기본(EXHAUSTIVE)은 새 입력 크기마다 cuDNN 알고리즘을 전부 재 봐서 한 장에 수십 초·메모리 9GB 까지 갔다
        providers = [("CUDAExecutionProvider", {"cudnn_conv_algo_search": "HEURISTIC",
                                                "arena_extend_strategy": "kSameAsRequested"}),
                     "CPUExecutionProvider"]
        backend = "CUDA"
    else:
        providers, backend = ["CPUExecutionProvider"], "CPU"
    det = ort.InferenceSession(os.path.join(folder, "det.onnx"), opts, providers=providers)
    rec = ort.InferenceSession(os.path.join(folder, "rec.onnx"), opts, providers=providers)
    if det.get_providers()[0] == "CPUExecutionProvider":
        backend = "CPU"     # GPU 를 못 잡고 CPU 로 떨어졌다 (DLL 없음 등)
    return det, rec, backend


class PaddleOCR:
    needs_reread = False    # Windows OCR 처럼 줄을 건너뛰지 않아 넷으로 나눠 다시 읽지 않는다

    def __init__(self, root: str, charset: str = "", threads: int = 4, rare: str = "", cpu: bool = False):
        folder = model_dir(root)
        self.det, self.rec, self.backend = _sessions(folder, threads, cpu)
        self.name = f"PaddleOCR ({self.backend})"
        with open(os.path.join(folder, "rec_dict.txt"), encoding="utf-8") as f:
            chars = [line.rstrip("\n") for line in f]
        self.chars = ["<blank>"] + chars + [" "]
        classes = self.rec.get_outputs()[0].shape[-1]
        if isinstance(classes, int) and classes != len(self.chars):
            raise RuntimeError(f"인식 모델 글자 수({classes})와 사전({len(self.chars)})이 다릅니다")
        allowed = np.ones(len(self.chars), bool)
        if charset:
            known = set(charset)
            for i, ch in enumerate(self.chars):
                if len(ch) == 1 and "㐀" <= ch <= "鿿" and ch not in known:
                    allowed[i] = False
        self._allowed = allowed
        self._allowed_idx = np.nonzero(allowed)[0]
        rare_set = set(rare)
        self._rare = np.array([len(ch) == 1 and ch in rare_set for ch in self.chars], bool)
        self._common_idx = np.nonzero(allowed & ~self._rare)[0]
        self._cache: OrderedDict[bytes, tuple] = OrderedDict()
        self._lock = threading.Lock()
        self.seconds_per_frame = 0.0

    # ------------------------------------------------------------------ 준비
    def warm(self, width: int, height: int) -> float:
        """처음 쓰는 입력 크기를 미리 한 번씩 돌린다 (DirectML 은 크기마다 1~3초 준비한다). 화면 한 장 추정 시간(초)."""
        H, W = _pad(height), _pad(width)
        self.det.run(None, {"x": np.zeros((1, 3, H, W), np.float32)})
        if self.backend != "CPU":   # CPU 는 크기마다 준비할 것이 없다
            for w in range(REC_STEP, REC_MAX_W + 1, REC_STEP):
                self.rec.run(None, {"x": np.zeros((REC_BATCH, 3, REC_H, w), np.float32)})
        t = time.perf_counter()
        self.det.run(None, {"x": np.zeros((1, 3, H, W), np.float32)})
        det = time.perf_counter() - t
        t = time.perf_counter()
        self.rec.run(None, {"x": np.zeros((REC_BATCH, 3, REC_H, 960), np.float32)})
        rec = time.perf_counter() - t
        # 메뉴 화면 한 장은 줄 60개쯤 = 8줄 배치 8번
        self.seconds_per_frame = det + rec * 8
        return self.seconds_per_frame

    # ------------------------------------------------------------------ 읽기
    def recognize(self, image: Image.Image, scale: float = 1.0) -> list[OCRLine]:
        if scale != 1.0:
            image = image.resize((int(image.width * scale), int(image.height * scale)), Image.LANCZOS)
        bgr = np.ascontiguousarray(np.asarray(image.convert("RGB"))[:, :, ::-1])
        with self._lock:
            quads = self._detect(bgr)
            crops = [_crop(bgr, q) for q in quads]
            keys = [hashlib.blake2b(c.tobytes() + str(c.shape).encode(), digest_size=16).digest() for c, _o in crops]
            todo = [i for i, k in enumerate(keys) if k not in self._cache]
            if todo:
                for i, res in zip(todo, self._recognize([crops[i][0] for i in todo])):
                    self._remember(keys[i], res)
            out = []
            for (crop, origin), key in zip(crops, keys):
                self._cache.move_to_end(key)
                line = _line(self._cache[key], origin)
                if line is not None:
                    out.append(line)
        if scale != 1.0:
            out = [_scaled(line, 1 / scale) for line in out]
        return out

    def _remember(self, key: bytes, res: tuple) -> None:
        self._cache[key] = res
        while len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)

    def _detect(self, img: np.ndarray) -> list[np.ndarray]:
        import cv2

        h, w = img.shape[:2]
        H, W = _pad(h), _pad(w)
        x = np.empty((H, W, 3), np.float32)
        x[:] = -_MEAN / _STD        # 검은 바탕으로 채운다
        x[:h, :w] = (img.astype(np.float32) / 255.0 - _MEAN) / _STD
        pred = self.det.run(None, {"x": x.transpose(2, 0, 1)[None]})[0][0, 0][:h, :w]
        bitmap = (pred > DET_THRESH).astype(np.uint8)
        contours, _ = cv2.findContours(bitmap, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        out = []
        for c in contours[:1000]:
            (cx, cy), (rw, rh), angle = cv2.minAreaRect(c)
            if min(rw, rh) < MIN_SIZE:
                continue
            if _box_score(pred, _order(cv2.boxPoints(((cx, cy), (rw, rh), angle)))) < BOX_THRESH:
                continue
            d = rw * rh * UNCLIP / (2 * (rw + rh))
            rw, rh = rw + 2 * d, rh + 2 * d
            if min(rw, rh) < MIN_SIZE + 2:
                continue
            pts = _order(cv2.boxPoints(((cx, cy), (rw, rh), angle)))
            pts[:, 0] = np.clip(pts[:, 0], 0, w)
            pts[:, 1] = np.clip(pts[:, 1], 0, h)
            out.append(pts)
        out.sort(key=lambda p: (round(p[:, 1].min() / 8), p[:, 0].min()))
        return out

    def _recognize(self, crops: list[np.ndarray]) -> list[tuple]:
        """줄 그림마다 (글자 목록, 글자별 가로 중심(그림 안 x), 확신도, 글자 획 상자(그림 안))."""
        import cv2

        order = np.argsort([c.shape[1] / max(1, c.shape[0]) for c in crops])
        results: list[tuple] = [((), (), 0.0, None)] * len(crops)
        for s in range(0, len(crops), REC_BATCH):
            idx = order[s:s + REC_BATCH]
            ratio = max(crops[i].shape[1] / max(1, crops[i].shape[0]) for i in idx)
            W = min(REC_MAX_W, max(REC_STEP, int(math.ceil(REC_H * ratio / REC_STEP)) * REC_STEP))
            batch = np.zeros((REC_BATCH, 3, REC_H, W), np.float32)
            widths = []
            for n, i in enumerate(idx):
                c = crops[i]
                rw = min(W, max(1, int(math.ceil(REC_H * c.shape[1] / max(1, c.shape[0])))))
                r = cv2.resize(c, (rw, REC_H)).astype(np.float32) / 255.0
                batch[n, :, :, :rw] = ((r - 0.5) / 0.5).transpose(2, 0, 1)
                widths.append(rw)
            prob = self.rec.run(None, {"x": batch})[0]
            step = W / prob.shape[1]
            for n, i in enumerate(idx):
                chars, steps, conf = self._decode(prob[n])
                scale = crops[i].shape[1] / widths[n]
                xs = tuple(float((t + 0.5) * step * scale) for t in steps)
                chars, xs = _join_dots(chars, xs)
                results[i] = (tuple(chars), xs, conf, _ink_box(crops[i]))
        return results

    def _decode(self, p: np.ndarray) -> tuple[list[str], list[int], float]:
        ids = p.argmax(-1)
        bad = ~self._allowed[ids]
        if bad.any():
            # 게임 글에 없는 한자를 골랐으면 있는 글자 가운데서 다시 고른다 (번체 '奧' → '奥', '兌' → '兑')
            ids[bad] = self._allowed_idx[p[bad][:, self._allowed_idx].argmax(-1)]
        odd = self._rare[ids]
        if odd.any():
            # 게임 글에 다섯 번 이하 나오는 한자(번체 '換' 등)는 흔한 한자가 꽤 닮았으면 그쪽으로 ('兑換券' → '兑换券')
            rows = np.nonzero(odd)[0]
            alt = self._common_idx[p[rows][:, self._common_idx].argmax(-1)]
            switch = p[rows, alt] >= p[rows, ids[rows]] * RARE_RATIO
            ids[rows[switch]] = alt[switch]
        keep = np.ones(len(ids), bool)
        keep[1:] = ids[1:] != ids[:-1]
        keep &= ids != 0
        steps = np.nonzero(keep)[0]
        conf = float(p[steps, ids[steps]].mean()) if len(steps) else 0.0
        return [self.chars[k] for k in ids[steps]], steps.tolist(), conf


def _pad(n: int) -> int:
    return max(DET_PAD, int(math.ceil(n / DET_PAD)) * DET_PAD)


def _order(pts) -> np.ndarray:
    """네 점을 왼쪽 위·오른쪽 위·오른쪽 아래·왼쪽 아래 순서로."""
    pts = sorted(np.asarray(pts, np.float32).tolist(), key=lambda p: p[0])
    tl, bl = sorted(pts[:2], key=lambda p: p[1])
    tr, br = sorted(pts[2:], key=lambda p: p[1])
    return np.array([tl, tr, br, bl], np.float32)


def _box_score(pred: np.ndarray, pts: np.ndarray) -> float:
    import cv2

    h, w = pred.shape
    x0 = int(np.clip(np.floor(pts[:, 0].min()), 0, w - 1))
    x1 = int(np.clip(np.ceil(pts[:, 0].max()), 0, w - 1))
    y0 = int(np.clip(np.floor(pts[:, 1].min()), 0, h - 1))
    y1 = int(np.clip(np.ceil(pts[:, 1].max()), 0, h - 1))
    mask = np.zeros((y1 - y0 + 1, x1 - x0 + 1), np.uint8)
    cv2.fillPoly(mask, (pts - [x0, y0]).reshape(1, -1, 2).astype(np.int32), 1)
    return cv2.mean(pred[y0:y1 + 1, x0:x1 + 1], mask)[0]


def _crop(img: np.ndarray, pts: np.ndarray) -> tuple[np.ndarray, tuple]:
    """줄 그림과 그 그림의 화면 위치 (x, y, 폭, 높이). 거의 수평이면 잘라 내기만 한다."""
    import cv2

    x0, y0 = int(max(0, pts[:, 0].min())), int(max(0, pts[:, 1].min()))
    x1, y1 = int(np.ceil(pts[:, 0].max())), int(np.ceil(pts[:, 1].max()))
    if abs(pts[0][1] - pts[1][1]) <= 1.5 and abs(pts[0][0] - pts[3][0]) <= 1.5 and x1 - x0 >= 2 and y1 - y0 >= 2:
        return np.ascontiguousarray(img[y0:y1, x0:x1]), (x0, y0, x1 - x0, y1 - y0)
    w = max(1, int(max(np.linalg.norm(pts[0] - pts[1]), np.linalg.norm(pts[2] - pts[3]))))
    h = max(1, int(max(np.linalg.norm(pts[0] - pts[3]), np.linalg.norm(pts[1] - pts[2]))))
    dst = np.array([[0, 0], [w, 0], [w, h], [0, h]], np.float32)
    out = cv2.warpPerspective(img, cv2.getPerspectiveTransform(pts, dst), (w, h),
                              borderMode=cv2.BORDER_REPLICATE, flags=cv2.INTER_LINEAR)
    return out, (x0, y0, max(1, x1 - x0), max(1, y1 - y0))


def _ink_box(crop: np.ndarray) -> tuple | None:
    """줄 그림 안에서 글자 획이 차지한 (x, y, 폭, 높이). 검출 상자는 글자보다 위아래로 넉넉해서, Windows OCR 처럼
    글자에 붙은 상자를 줘야 번역문 크기·바탕 재기가 같게 된다. 바탕과 글자가 안 갈리면 None."""
    from .style import _otsu

    arr = crop.astype(np.float32)
    gray = arr.mean(axis=2)
    ink = gray > _otsu(gray)
    if ink.mean() > 0.5:
        ink = ~ink
    # 밝기 경계만으로는 흰 글자 뒤의 파란 강조('…降低至15%')가 바탕으로 잡혀 상자 밖으로 빠졌다 (그 자리 원문이
    # 지워지지 않고 남았다). 바탕색과 확연히 다른 색도 글자로 친다
    bg_sel = ~ink
    if int(bg_sel.sum()) >= 16:
        bg = np.median(arr[bg_sel], axis=0)
        dist = np.sqrt(((arr - bg) ** 2).sum(axis=2))
        ink |= dist > max(INK_COLOR_MIN, float(np.percentile(dist, 97)) * 0.35)
    h, w = ink.shape
    rows = np.nonzero(ink.sum(axis=1) >= max(1, w * 0.01))[0]
    cols = np.nonzero(ink.sum(axis=0) >= 1)[0]
    if not len(rows) or not len(cols):
        return None
    box = (int(cols[0]), int(rows[0]), int(cols[-1] - cols[0] + 1), int(rows[-1] - rows[0] + 1))
    if box[3] < h * 0.35 or box[2] < w * 0.3:
        return None
    return box


def _join_dots(chars: list[str], xs: tuple) -> tuple[list[str], tuple]:
    """'……' 을 '…·' '·…' 로 읽은 것을 바로잡는다 (대사의 말줄임표). 글자 수가 바뀌면 자리도 맞춘다."""
    text = "".join(chars)
    if "…" not in text or not _DOTS.search(text):
        return chars, xs
    out_c, out_x, i = [], [], 0
    while i < len(chars):
        j = i
        while j < len(chars) and chars[j] in "…·":
            j += 1
        run = chars[i:j]
        if j - i >= 2 and "…" in run:
            out_c += ["…", "…"]
            out_x += [xs[i], xs[j - 1]]
            i = j
        else:
            out_c.append(chars[i])
            out_x.append(xs[i])
            i += 1
    return out_c, tuple(out_x)


def _line(res: tuple, origin: tuple) -> OCRLine | None:
    chars, xs, conf, ink = res
    text = "".join(chars).strip()
    if not text or conf < MIN_CONF:
        return None
    ox, oy, ow, oh = origin
    if ink is not None:
        bx, by, bw, bh = ink[0] + ox, ink[1] + oy, ink[2], ink[3]
    else:
        bx, by, bw, bh = ox, oy, ow, oh
    # 낱자 상자는 이웃 글자와의 가운데에서 나눠 빈틈 없이 잇는다. 한 줄 안에는 빈틈이 없다 (검출이 이미
    # 칸마다 상자를 가른다). 글자 폭으로 어림하면 '——' 를 '…' 한 자로 읽은 자리에 빈틈이 생겨 split_chips 가
    # 비서 대사 '早上好啊…嗯？就是…' 를 둘로 갈랐다
    pts = [(ch, ox + x) for ch, x in zip(chars, xs) if not ch.isspace()]
    words = []
    for k, (ch, cx) in enumerate(pts):
        x0 = bx if k == 0 else (pts[k - 1][1] + cx) / 2
        x1 = bx + bw if k == len(pts) - 1 else (cx + pts[k + 1][1]) / 2
        x0, x1 = max(bx, min(x0, bx + bw - 1)), max(bx + 1, min(x1, bx + bw))
        words.append((ch, int(x0), int(by), max(1, int(round(x1 - x0))), int(bh)))
    return OCRLine(text=text, box=(int(bx), int(by), int(bw), int(bh)), conf=conf, words=tuple(words))


def _scaled(line: OCRLine, k: float) -> OCRLine:
    s = lambda v: int(round(v * k))  # noqa: E731
    return OCRLine(text=line.text, box=tuple(s(v) for v in line.box), conf=line.conf,
                   words=tuple((t, s(x), s(y), s(w), s(h)) for t, x, y, w, h in line.words))
