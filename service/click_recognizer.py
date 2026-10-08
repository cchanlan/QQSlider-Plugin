"""点选验证码识别 — 本地 CLIP，不需要外部 AI 服务。

## 为什么需要它

腾讯点选的协议里**只有题面，没有答案**（实测 60 张真题确认）：

    instruction = "百香果"          # 题面，中文
    json_payload.select_region_list # [{"id":1,"range":[0,34,220,254]}, ...] 纯坐标
    prompt_id                       # 题面编号，与题面一一对应但每张图正解位置都变
    img_url                         # 随机哈希，不含标签

所以必须**看图**。解法是把 CLIP 模型当普通 pip 依赖装到本地，
纯 CPU 推理，不连任何外部服务。

## 模型选型（2026-10-08 在 N5105 上实测 60 张真题）

| 模型 | 体积 | 单次 | 准确率 |
|---|---|---|---|
| **Chinese-CLIP ViT-B/16 int8** | **182MB** | **1.8s** | **88%** |
| Chinese-CLIP q4f16 | 125.7MB | 6.0s | 更小但更慢（无 AVX 时 int4 解包开销大）|
| MobileCLIP-S0 int8 | 52MB | 1.27s | 29% —— **英文模型，认不了中文题面** |
| 纯 CV 找离群格 | 0 | 10ms | 48% |

结论：**只有 Chinese-CLIP 能用**。它是原生中文、单模型（图文一起喂）。

## 两阶段流程（CV 预筛 + CLIP 打分）

干扰图是「另一个概念的若干张变体」，所以「跟其它格最不像」的格子
大概率包含正解 —— 用它把 6 格缩到 3~4 格再喂 CLIP：

    全 6 格 3.7s  →  CV 预筛 3 格 1.8s（准确率不变）

## 环境硬约束（实测）

服务器 CPU 是 Intel Celeron N5105：**没有 AVX / AVX2 / FMA**（只有 SSE4.2），
无 GPU。所以 fp32 大模型慢到没法用，必须 int8；而且模型**锁死 224×224 输入**，
降分辨率会直接报 broadcast 错误。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image

LOG = logging.getLogger(__name__)

# ── 模型来源 ────────────────────────────────────────────────────────────
MODEL_REPO = "Xenova/chinese-clip-vit-base-patch16"
MODEL_FILES = {
    "onnx/model_quantized.onnx": "model_quantized.onnx",
    "tokenizer.json": "tokenizer.json",
}
# 大文件在 hf-mirror 上会被中途 reset，必须多源 + 断点续传（实测 182MB / 14.4s）
MODEL_MIRRORS = (
    "https://hf-mirror.com",
    "https://huggingface.co",
)
_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}

# ── 图像参数（Chinese-CLIP 的标准归一化，不能改）─────────────────────────
INPUT_SIZE = 224
_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32).reshape(1, 3, 1, 1)
_STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32).reshape(1, 3, 1, 1)

DEFAULT_TOP_K = 3


def otsu_split(scores, min_k: int = 1, max_k: int | None = None):
    """按 1-D Otsu（类间方差最大）把分数切成「高分簇 / 低分簇」。

    ## 为什么需要它

    点选有两种题面（`json_payload.lang_headers` 里写着）：

        zh-cn = "选择$最$符合描述的图片"      # 单数 → 选 1 格
        zh-cn = "选择$%所有%$符合描述的图片"  # 复数 → 选**所有**符合的格

    第二种是「多选」，**正解格数不固定**（实测 2~4 格都有）。所以不能用
    「固定选前 k 格」——少选一格就是错（实测连续 15 次全错都是这个原因）。

    真正的信号是**分数的断层**：符合描述的格子分数明显高一档。
    但「相邻两格最大差值」不稳（末尾那个离群低分格会抢走最大间隔，
    实测会把 4 格切成 5 格），而 **Otsu 看的是两类的类间方差，
    不会被单个离群值带偏** —— 实测同一个题目用最大间隔切 k=5（错）、
    用 Otsu 切 k=4（对）。

    @returns `(选中的 0-based 下标列表, 类间方差, 断点 k)`
        类间方差同时是个**置信度**：正常多选实测 55~170，
        而「包含文字：X」那种认不出来的题只有 0.3 左右 —— 差两个数量级，
        可以据此判断「这题没把握」，交给上层换题重试。
    """
    n = len(scores)
    if n <= 1:
        return list(range(n)), 0.0, n
    order = sorted(range(n), key=lambda i: -float(scores[i]))
    s = [float(scores[i]) for i in order]
    if max_k is None:
        max_k = n - 1
    lo = max(1, int(min_k))
    hi = min(int(max_k), n - 1)
    best_bcv, best_k = -1.0, lo
    for k in range(lo, hi + 1):
        m_hi = sum(s[:k]) / k
        m_lo = sum(s[k:]) / (n - k)
        # 两类权重 × 均值差的平方（Otsu 的类间方差，常数项略去）
        bcv = (k / n) * ((n - k) / n) * (m_hi - m_lo) ** 2
        if bcv > best_bcv:
            best_bcv, best_k = bcv, k
    return order[:best_k], best_bcv, best_k


class ClickSolverError(RuntimeError):
    """点选识别的基类错误。"""


class ModelNotReady(ClickSolverError):
    """CLIP 模型还没下载好。调用方应提示用户、或回落到纯 CV。"""


def model_dir() -> Path:
    """模型存放目录（跟着插件走，不用系统临时目录）。"""
    env = os.environ.get("QQ_SLIDER_MODEL_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent / ".models" / MODEL_REPO.replace("/", "__")


def model_ready() -> bool:
    """模型文件是否齐了（不校验大小，下载过程有原子性保护）。"""
    d = model_dir()
    return all((d / name).is_file() for name in MODEL_FILES.values())


def _log(logger, level: str, msg: str) -> None:
    """往调用方给的 logger 打日志；logger 可能只有部分方法，也可能压根没有。"""
    if logger is None:
        return
    fn = getattr(logger, level, None)
    if callable(fn):
        try:
            fn(msg)
        except Exception:  # noqa: BLE001
            pass


def _download(repo_path: str, dest: Path, logger=None, tries: int = 8) -> bool:
    """断点续传下载单个文件。大文件被 reset 时能接着下，不重头来。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    # 已完整就不用下
    if dest.is_file() and dest.stat().st_size > 1024:
        return True

    for attempt in range(tries):
        have = tmp.stat().st_size if tmp.is_file() else 0
        headers = dict(_UA)
        if have:
            headers["Range"] = f"bytes={have}-"
        base = MODEL_MIRRORS[attempt % len(MODEL_MIRRORS)]
        url = f"{base}/{MODEL_REPO}/resolve/main/{repo_path}?download=true"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=120) as resp:
                total = resp.headers.get("Content-Length")
                # 服务器不支持续传时会返回 200 而不是 206，得重下
                if have and resp.status != 206:
                    have = 0
                total = (int(total) + have) if total else None
                mode = "ab" if have else "wb"
                with open(tmp, mode) as fh:
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        fh.write(chunk)
            if total and tmp.stat().st_size < total:
                _log(logger, "debug", f"下载未完成 {repo_path}（{tmp.stat().st_size}/{total}），继续")
                continue
            # 原子替换：下载完整才改名，避免半个文件被当成好的
            tmp.replace(dest)
            _log(logger, "info", f"已下载 {repo_path}（{dest.stat().st_size / 1048576:.1f}MB）")
            return True
        except Exception as err:  # noqa: BLE001
            _log(logger, "debug", f"下载 {repo_path} 第 {attempt + 1} 次失败：{type(err).__name__}: {err}")
            time.sleep(1.5)
    return False


def ensure_model(logger=None, force: bool = False) -> bool:
    """确保模型就绪。首次约 182MB，实测 14~20 秒（hf-mirror）。"""
    if not force and model_ready():
        return True
    d = model_dir()
    for repo_path, name in MODEL_FILES.items():
        if not _download(repo_path, d / name, logger):
            return False
    return model_ready()


# ── 识别器 ──────────────────────────────────────────────────────────────
class ClickRecognizer:
    """CV 预筛 + CLIP 打分的点选识别器。

    常驻使用（模型载入 ~0.8s，之后每次推理 ~1.8s）。
    线程安全：内部一把锁串行化 —— 过码是低频操作，串行还避免 ONNX 会话并发问题。
    """

    def __init__(self, top_k: int = DEFAULT_TOP_K, threads: int | None = None):
        self.top_k = max(1, min(6, int(top_k)))
        self.threads = int(threads or min(4, os.cpu_count() or 2))
        self._lock = threading.Lock()
        self._sess = None
        self._tok = None
        self._loaded_at = 0.0

    # ── 载入 ──────────────────────────────────────────────────────────
    def load(self) -> None:
        """载入模型。没下载好就抛 ModelNotReady（调用方可回落纯 CV）。"""
        if self._sess is not None:
            return
        if not model_ready():
            raise ModelNotReady(f"CLIP 模型未就绪（{model_dir()}），先发 #滑块过码安装")
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as err:
            raise ModelNotReady(f"缺少依赖 {err.name}，先发 #滑块过码安装") from err

        d = model_dir()
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = self.threads
        # 图优化全开：CPU 上能省 10~20%
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._sess = ort.InferenceSession(
            str(d / "model_quantized.onnx"), opts, providers=["CPUExecutionProvider"]
        )
        self._tok = Tokenizer.from_file(str(d / "tokenizer.json"))
        self._loaded_at = time.time()
        LOG.info("点选识别模型已载入（threads=%d, top_k=%d）", self.threads, self.top_k)

    @property
    def loaded(self) -> bool:
        return self._sess is not None

    def close(self) -> None:
        with self._lock:
            self._sess = None
            self._tok = None

    # ── CV 预筛 ───────────────────────────────────────────────────────
    @staticmethod
    def _tile_feature(tile: Image.Image) -> np.ndarray:
        """一格的特征：HSV 彩色直方图 + 8×8 灰度结构。

        直方图管「颜色像不像」，灰度管「构图像不像」——
        干扰图常是同一概念的变体（背景/构图近、主体略变），
        两个一起才能把「异类」挑出来。
        """
        small = tile.resize((16, 16), Image.LANCZOS)
        arr = np.asarray(small, np.float32) / 255.0
        hist = np.concatenate(
            [np.histogram(arr[:, :, c], bins=8, range=(0, 1), density=True)[0] for c in range(3)]
        )
        gray = np.asarray(tile.convert("L").resize((8, 8), Image.LANCZOS), np.float32).ravel() / 255.0
        return np.concatenate([hist * 2.0, gray])

    def rank_tiles(self, img: Image.Image, regions) -> list[int]:
        """按「离群程度」给格子排序（越前面越可能不是主流那类）。

        离群度 = 该格到其它格的**最近邻距离**。
        同类变体之间距离小；孤零零的那一格距离必然大。
        """
        feats = np.array([self._tile_feature(img.crop(tuple(map(int, r)))) for r in regions])
        n = len(feats)
        if n < 2:
            return list(range(n))
        # 两两欧氏距离
        diff = feats[:, None, :] - feats[None, :, :]
        dist = np.linalg.norm(diff, axis=-1)
        nearest = np.array([np.min(np.delete(dist[i], i)) for i in range(n)])
        return [int(i) for i in np.argsort(-nearest)]

    # ── CLIP 打分 ─────────────────────────────────────────────────────
    def _clip_scores(self, img: Image.Image, regions, indices, label: str) -> np.ndarray:
        assert self._sess is not None and self._tok is not None
        tiles = [
            img.crop(tuple(map(int, regions[i]))).resize((INPUT_SIZE, INPUT_SIZE), Image.BICUBIC)
            for i in indices
        ]
        batch = np.stack([np.asarray(t, np.float32).transpose(2, 0, 1) / 255.0 for t in tiles])
        batch = (batch - _MEAN) / _STD

        enc = self._tok.encode(label)
        feed = {
            "input_ids": np.array([enc.ids], np.int64),
            "attention_mask": np.array([enc.attention_mask], np.int64),
            "pixel_values": batch,
        }
        # logits_per_image 直接就是「每格与题面的匹配分」
        out = self._sess.run(["logits_per_image"], feed)[0]
        return np.asarray(out, np.float32).ravel()

    def recognize(self, image_bytes: bytes, instruction: str, regions) -> dict:
        """识别点选答案。

        @param image_bytes 背景拼图（含 6 格）
        @param instruction 题面，如 "百香果"
        @param regions     格子坐标 [x0,y0,x1,y1] 列表（来自 select_region_list）
        @returns {ok, pick, pick_index, scores, margin, candidates, seconds, cv_rank}
        """
        label = str(instruction or "").strip().strip("\u201c\u201d\"' ")
        if not label:
            raise ClickSolverError("题面为空，无法识别")
        if not regions:
            raise ClickSolverError("没有格子坐标")

        started = time.perf_counter()
        img = Image.open(__import__("io").BytesIO(image_bytes)).convert("RGB")

        # ① CV 预筛：把 6 格缩到 top_k（省一半时间，实测准确率不掉）
        cv_rank = self.rank_tiles(img, regions)
        k = min(self.top_k, len(cv_rank))
        candidates = cv_rank[:k]

        # ② CLIP 在候选里挑最匹配题面的
        self.load()
        with self._lock:
            scores = self._clip_scores(img, regions, candidates, label)

        order = np.argsort(-scores)
        best = int(order[0])
        margin = float(scores[order[0]] - scores[order[1]]) if len(order) > 1 else float("inf")

        return {
            "ok": True,
            "pick": int(candidates[best]) + 1,      # 1-based，方便人看
            "pick_index": int(candidates[best]),    # 0-based，给提交用
            "candidates": [int(c) + 1 for c in candidates],
            "cv_rank": [int(c) + 1 for c in cv_rank],
            "scores": [float(s) for s in scores],
            "margin": margin,
            "seconds": round(time.perf_counter() - started, 2),
        }

    def recognize_multi(self, image_bytes: bytes, instruction: str, regions,
                        min_k: int = 1, max_k: int | None = None) -> dict:
        """多选题识别：给**全 6 格**打分，再按 Otsu 断层切出「符合描述」的那一簇。

        和 `recognize` 的区别有两个，都不能省：

        1. **不做 CV 预筛**。预筛是按「谁最离群」砍到 3 格，那是为
           「单选、找一个异类」设计的；多选题正解可能有 4 格，
           预筛会把其中一格直接丢掉 —— 丢一格就必错。
        2. **不取 argmax，取高分簇**。多选要交的是**一整簇**。

        @returns {ok, pick_indices, picks, k, separation, scores, margin, seconds}
        """
        label = str(instruction or "").strip().strip("\u201c\u201d\"' ")
        if not label:
            raise ClickSolverError("题面为空，无法识别")
        if not regions:
            raise ClickSolverError("没有格子坐标")

        started = time.perf_counter()
        img = Image.open(__import__("io").BytesIO(image_bytes)).convert("RGB")

        self.load()
        idx = list(range(len(regions)))
        with self._lock:
            scores = self._clip_scores(img, regions, idx, label)

        picks, bcv, k = otsu_split(scores, min_k=min_k, max_k=max_k)
        order = sorted(range(len(scores)), key=lambda i: -float(scores[i]))
        margin = (float(scores[order[0]] - scores[order[1]])) if len(order) > 1 else float("inf")

        return {
            "ok": True,
            "pick_indices": [int(i) for i in picks],
            "pick_index": int(picks[0]),                 # 兼容单选调用方
            "picks": [int(i) + 1 for i in picks],        # 1-based，方便人看
            "k": int(k),
            "separation": float(bcv),
            "scores": [float(v) for v in scores],
            "ordered": [int(i) + 1 for i in order],
            "margin": margin,
            "seconds": round(time.perf_counter() - started, 2),
            "solver": "local-clip-multi",
        }

    def recognize_cv_only(self, image_bytes: bytes, regions) -> dict:
        """不载模型，纯 CV 兜底（准确率约 48%，模型没好时才用）。"""
        started = time.perf_counter()
        img = Image.open(__import__("io").BytesIO(image_bytes)).convert("RGB")
        rank = self.rank_tiles(img, regions)
        return {
            "ok": True,
            "pick": int(rank[0]) + 1,
            "pick_index": int(rank[0]),
            "candidates": [int(c) + 1 for c in rank],
            "cv_rank": [int(c) + 1 for c in rank],
            "scores": [],
            "margin": None,
            "solver": "cv_only",
            "seconds": round(time.perf_counter() - started, 2),
        }


# 服务侧共用一个常驻识别器（模型载入贵，复用才快）
_shared: ClickRecognizer | None = None
_shared_lock = threading.Lock()


def get_recognizer(top_k: int = DEFAULT_TOP_K) -> ClickRecognizer:
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = ClickRecognizer(top_k=top_k)
        return _shared


def recognizer_status() -> dict:
    """给 /health 用的状态。"""
    import shutil

    d = model_dir()
    size = 0
    for name in MODEL_FILES.values():
        p = d / name
        if p.is_file():
            size += p.stat().st_size
    return {
        "modelReady": model_ready(),
        "modelDir": str(d),
        "modelMB": round(size / 1048576, 1),
        "loaded": bool(_shared and _shared.loaded),
    }
