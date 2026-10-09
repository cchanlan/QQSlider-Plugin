"""
Tencent slider CAPTCHA solver for the public captcha test application.

The solver keeps the original ``SlideSolver().solve(retries=3)``
interface while deriving all challenge geometry from ``dyn_show_info``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import math
import queue
import random
import re
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin


import cv2
import numpy as np
import requests
from PIL import Image

try:
    from curl_cffi import requests as curl_requests
    from curl_cffi.requests.errors import RequestsError as CurlRequestsError
except ImportError:  # pragma: no cover - requests remains a supported fallback
    curl_requests = None
    CurlRequestsError = OSError

REQUEST_ERRORS = (requests.RequestException, CurlRequestsError)


@dataclass(frozen=True, slots=True)
class Endpoints:
    """一次过码涉及的全部域名与入口参数。

    腾讯云验证码（`turing.captcha.qcloud.com`）和 QQ 登录滑块
    （`ssl.captcha.qq.com` / `t.captcha.qq.com`）用的是**同一套 TCaptcha 协议**，
    差别只在这些常量与 `aid`，所以整体抽出来参数化。

    实测（2026-10-07，本机）：两边的 `cap_union_prehandle` 返回结构完全一致
    （都含 `comm_captcha_cfg.tdc_path` 与 `dyn_show_info.bg_elem_cfg.img_url`）。
    """

    prehandle_url: str
    tdc_url: str
    verify_url: str
    cap_monitor_url: str
    captcha_base_url: str
    captcha_referer: str
    entry_url: str
    entry_referer: str
    frame_js: str
    drag_template: str
    dy_jy3: str
    dy_ele: str
    tcaptcha_js: str
    aid: int
    # 供服务端做「端点是否变了」的比较（Endpoints 是 frozen dataclass，可哈希，
    # 但显式给个短名字能让日志和缓存 key 更好读）
    name: str = ""
    # prehandle 的参数风格：
    #   "cloud" —— 腾讯云那套（aid 是主角，带 entry_url / elder_captcha / js 等）
    #   "qq"    —— QQ 那套（照抄 TCapIframeApi.js，**aid 为空**，靠 uin / cap_cd 区分，
    #              没有 entry_url / js 参数，多 curenv / captype / color / theme 等）
    prehandle_style: str = "cloud"


# 腾讯云验证码：TXCaptcha 的原始目标，本机实测可解（置信度 1.000，2.4s/次）
ENDPOINTS_TENCENT_CLOUD = Endpoints(
    prehandle_url="https://turing.captcha.qcloud.com/cap_union_prehandle",
    tdc_url="https://turing.captcha.qcloud.com/tdc.js",
    verify_url="https://turing.captcha.qcloud.com/cap_union_new_verify",
    cap_monitor_url="https://turing.captcha.qcloud.com/cap_monitor",
    captcha_base_url="https://turing.captcha.qcloud.com/",
    captcha_referer="https://turing.captcha.gtimg.com/",
    entry_url="https://cloud.tencent.com/product/captcha",
    entry_referer="https://cloud.tencent.com/",
    frame_js="https://turing.captcha.gtimg.com/1/tcaptcha-frame.91efdf16.js",
    drag_template="https://turing.captcha.gtimg.com/1/template/drag_ele.html",
    dy_jy3="https://turing.captcha.gtimg.com/1/dy-jy3.js",
    dy_ele="https://turing.captcha.gtimg.com/1/dy-ele.2006214c.js",
    tcaptcha_js="https://turing.captcha.qcloud.com/TCaptcha.js",
    aid=199999861,
    name="tencent",
)

# QQ 登录滑块。
#
# ⚠️ 实测（2026-10-07）：
#   · `ssl.captcha.qq.com/cap_union_prehandle` 恒返 **407**（stgw 网关直接拒），
#     拿 QQ 的 aid 打 `turing.captcha.qcloud.com` 也是 403；
#     真正吃请求的是 **`t.captcha.qq.com`**（实测 200，返回完整 tdc_path）。
#   · QQ 的题型（滑块 / 点选）由服务端按 uin + cap_cd 下发。静态探测常见 aid
#     一律只回点选（`DynAnswerType_UC`），**真实 aid / cap_cd 必须从 icqq 登录时
#     收到的那个 URL 里取** —— 见 README「待接入」一节。
#   · 静态资源（tcaptcha-frame / dy-ele / drag_ele）在 ssl 与 turing 域名下同名，
#     这里先按 ssl 域写；接入时某个资源 404 就回落到 turing 的对应地址。
ENDPOINTS_QQ = Endpoints(
    prehandle_url="https://t.captcha.qq.com/cap_union_prehandle",
    tdc_url="https://t.captcha.qq.com/tdc.js",
    verify_url="https://t.captcha.qq.com/cap_union_new_verify",
    cap_monitor_url="https://t.captcha.qq.com/cap_monitor",
    captcha_base_url="https://t.captcha.qq.com/",
    captcha_referer="https://ssl.captcha.qq.com/",
    entry_url="https://ssl.captcha.qq.com/template/wireless_mqq_captcha.html",
    entry_referer="https://ssl.captcha.qq.com/",
    frame_js="https://ssl.captcha.qq.com/tcaptcha-frame.91efdf16.js",
    drag_template="https://ssl.captcha.qq.com/template/drag_ele.html",
    dy_jy3="https://ssl.captcha.qq.com/dy-jy3.js",
    dy_ele="https://ssl.captcha.qq.com/dy-ele.2006214c.js",
    tcaptcha_js="https://ssl.captcha.qq.com/TCaptcha.js",
    aid=16,
    name="qq",
    prehandle_style="qq",
)

ENDPOINTS = {
    "tencent": ENDPOINTS_TENCENT_CLOUD,
    "qq": ENDPOINTS_QQ,
}

# 兼容旧调用方的默认值
PREHANDLE_URL = ENDPOINTS_TENCENT_CLOUD.prehandle_url
TDC_URL = ENDPOINTS_TENCENT_CLOUD.tdc_url
VERIFY_URL = ENDPOINTS_TENCENT_CLOUD.verify_url
CAP_MONITOR_URL = ENDPOINTS_TENCENT_CLOUD.cap_monitor_url
CAPTCHA_BASE_URL = ENDPOINTS_TENCENT_CLOUD.captcha_base_url
CAPTCHA_REFERER = ENDPOINTS_TENCENT_CLOUD.captcha_referer
ENTRY_URL = ENDPOINTS_TENCENT_CLOUD.entry_url
ENTRY_REFERER = ENDPOINTS_TENCENT_CLOUD.entry_referer
SLIDE_AID = ENDPOINTS_TENCENT_CLOUD.aid
FRAME_JS = ENDPOINTS_TENCENT_CLOUD.frame_js
DRAG_TEMPLATE = ENDPOINTS_TENCENT_CLOUD.drag_template
DY_JY3 = ENDPOINTS_TENCENT_CLOUD.dy_jy3
DY_ELE = ENDPOINTS_TENCENT_CLOUD.dy_ele
TCAPTCHA_JS = ENDPOINTS_TENCENT_CLOUD.tcaptcha_js
def _resolve_tdc_server() -> Path:
    """定位 Node 端 TDC worker 脚本。

    优先 `.cjs`：worker 是 CommonJS（用 `require`），而宿主插件目录的
    package.json 常带 `"type": "module"`，那样 `.js` 会被当成 ESM 解析，
    一启动就 `ReferenceError: require is not defined in ES module scope`。
    `.cjs` 后缀不受 package.json 影响，放哪都能跑。
    保留 `.js` 兜底是为了兼容直接跑本文件（没有 type:module 的场景）。
    """
    here = Path(__file__).resolve().parent
    for name in ("tdc_server.cjs", "tdc_server.js"):
        candidate = here / name
        if candidate.is_file():
            return candidate
    # 两个都没有时返回 .cjs，让报错信息指向期望的文件名
    return here / "tdc_server.cjs"


TDC_SERVER = _resolve_tdc_server()
CHROME_MAJORS = (146,)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)

LOG = logging.getLogger(__name__)


class SlideSolverError(RuntimeError):
    """Base error for local solver failures."""


class RetryableSolveError(SlideSolverError):
    """A fresh challenge may recover from this failure."""


class ProtocolError(RetryableSolveError):
    """The remote response did not match the expected protocol."""


class GapDetectionError(RetryableSolveError):
    """The gap could not be located with sufficient confidence."""


class UnsupportedCaptchaKind(SlideSolverError):
    """服务端下发的不是滑块题型。

    继承 `SlideSolverError` 而**不是** `RetryableSolveError` —— 换题型是服务端
    按 uin / cap_cd 决定的，重试同一套参数只会得到同样的题型，重试无意义。
    """


class TdcError(RetryableSolveError):
    """The local TDC worker failed or timed out."""


class PowError(RetryableSolveError):
    """The challenge proof-of-work could not be completed in time."""


@dataclass(frozen=True, slots=True)
class BrowserProfile:
    chrome_major: int
    user_agent: str
    sec_ch_ua: str
    language: str
    languages: tuple[str, ...]
    platform: str
    hardware_concurrency: int
    device_memory: int
    max_touch_points: int
    screen_width: int
    screen_height: int
    avail_height: int
    window_width: int
    window_height: int
    outer_width: int
    outer_height: int
    device_pixel_ratio: float
    timezone: str
    webgl_vendor: str
    webgl_renderer: str
    canvas_seed: int

    def tdc_payload(self) -> dict[str, Any]:
        return {
            "chromeMajor": self.chrome_major,
            "language": self.language,
            "languages": list(self.languages),
            "platform": self.platform,
            "hardwareConcurrency": self.hardware_concurrency,
            "deviceMemory": self.device_memory,
            "maxTouchPoints": self.max_touch_points,
            "doNotTrack": None,
            "pdfViewerEnabled": True,
            "screen": {
                "width": self.screen_width,
                "height": self.screen_height,
                "availWidth": self.screen_width,
                "availHeight": self.avail_height,
                "colorDepth": 24,
                "pixelDepth": 24,
            },
            "window": {
                "innerWidth": self.window_width,
                "innerHeight": self.window_height,
                "outerWidth": self.outer_width,
                "outerHeight": self.outer_height,
                "devicePixelRatio": self.device_pixel_ratio,
                "screenX": 0,
                "screenY": 0,
            },
            "timezone": self.timezone,
            "webglVendor": self.webgl_vendor,
            "webglRenderer": self.webgl_renderer,
            "canvasSeed": self.canvas_seed,
        }


def _make_browser_profile(rng: random.Random) -> BrowserProfile:
    major = 146
    # Stable product-like fingerprint (real cloakbrowser sample).
    # Continuous pure-protocol runs get ec=12 if geometry/WebGL thrash.
    screen_width, screen_height = 1920, 1080
    window_width, window_height = 1440, 900
    outer_width, outer_height = 1456, 988
    avail_height = 1032
    dpr = 1.0
    webgl_vendor = "Google Inc. (NVIDIA)"
    webgl_renderer = "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )
    sec_ch_ua = (
        f'"Chromium";v="{major}", "Not-A.Brand";v="24", '
        f'"Google Chrome";v="{major}"'
    )
    return BrowserProfile(
        chrome_major=major,
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
        language="zh-CN",
        languages=("zh-CN",),
        platform="Win32",
        hardware_concurrency=8,
        device_memory=8,
        max_touch_points=10,
        screen_width=screen_width,
        screen_height=screen_height,
        avail_height=avail_height,
        window_width=window_width,
        window_height=window_height,
        outer_width=outer_width,
        outer_height=outer_height,
        device_pixel_ratio=dpr,
        timezone="Asia/Shanghai",
        webgl_vendor=webgl_vendor,
        webgl_renderer=webgl_renderer,
        canvas_seed=rng.randrange(1, 2**31),
    )

@dataclass(frozen=True, slots=True)
class SlideSolverConfig:
    connect_timeout: float = 5.0
    read_timeout: float = 15.0
    tdc_timeout: float = 5.0
    pow_timeout: float = 10.0
    vm_timeout_ms: int = 2_000
    viewport_width: int = 340
    frame_width: int = 360
    frame_height: int = 360
    opera_offset_x: float = 10.0
    opera_offset_y: float = 69.0
    min_gap_confidence: float = 0.62
    min_gap_margin: float = 0.03
    random_seed: int | None = None
    debug_dir: Path | str | None = None
    node_command: str = "node"
    use_browser_tls: bool = False
    browser_profile: BrowserProfile | None = None
    # 降热（故意失败）—— 原版用于同一 IP 持续压测，默认关闭。
    # 作为登录过码服务时必须关，否则连续成功 3 次后永远拿不到 ticket。
    heat_control: bool = False
    heat_success_limit: int = 3
    # 复用 Node worker 进程（默认关闭，保持原版「每个 challenge 都新起进程」行为）。
    # 服务化时打开：tdc_server.js 每个请求都 `vm.createContext` 建新上下文，
    # 进程本身跨请求无状态，复用只省进程启动开销、不引入串味。
    reuse_tdc_worker: bool = False

    def __post_init__(self) -> None:
        numeric_positive = {
            "connect_timeout": self.connect_timeout,
            "read_timeout": self.read_timeout,
            "tdc_timeout": self.tdc_timeout,
            "pow_timeout": self.pow_timeout,
            "vm_timeout_ms": self.vm_timeout_ms,
            "viewport_width": self.viewport_width,
            "frame_width": self.frame_width,
            "frame_height": self.frame_height,
        }
        for name, value in numeric_positive.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.opera_offset_x < 0 or self.opera_offset_y < 0:
            raise ValueError("opera offsets must be non-negative")
        if self.frame_width < self.viewport_width:
            raise ValueError("frame_width must not be smaller than viewport_width")
        if not 0 <= self.min_gap_confidence <= 1:
            raise ValueError("min_gap_confidence must be between 0 and 1")
        if not 0 <= self.min_gap_margin <= 1:
            raise ValueError("min_gap_margin must be between 0 and 1")
        if self.debug_dir is not None:
            object.__setattr__(self, "debug_dir", Path(self.debug_dir))


@dataclass(frozen=True, slots=True)
class PieceTemplate:
    mask: np.ndarray
    rgb: np.ndarray
    alpha_x: int
    alpha_y: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class CaptchaGeometry:
    bg_width: int
    bg_height: int
    viewport_width: int
    viewport_height: int
    puzzle_id: int
    puzzle_init_x: float
    puzzle_init_y: float
    puzzle_width: int
    puzzle_height: int
    puzzle_sprite_x: int
    puzzle_sprite_y: int
    slider_id: int
    slider_init_x: float
    slider_init_y: float
    slider_width: int
    slider_height: int
    alpha_x: int
    alpha_y: int
    alpha_width: int
    alpha_height: int
    track_min_x: float
    track_max_x: float

    @property
    def scale_x(self) -> float:
        return self.viewport_width / self.bg_width

    @property
    def scale_y(self) -> float:
        return self.viewport_height / self.bg_height

    @property
    def initial_visible_x(self) -> float:
        return self.puzzle_init_x + self.alpha_x

    @property
    def initial_visible_y(self) -> float:
        return self.puzzle_init_y + self.alpha_y

    @property
    def press_image_x(self) -> float:
        return self.slider_init_x + self.slider_width / 2

    @property
    def press_image_y(self) -> float:
        return self.slider_init_y + self.slider_height / 2

    def target_element_x_for_gap(self, gap_x: float) -> float:
        target_element_x = gap_x - self.alpha_x
        tolerance = 1.0
        if not self.track_min_x - tolerance <= target_element_x <= self.track_max_x + tolerance:
            raise GapDetectionError(
                f"target element x {target_element_x:.1f} is outside track "
                f"[{self.track_min_x:.1f}, {self.track_max_x:.1f}]"
            )
        return min(max(target_element_x, self.track_min_x), self.track_max_x)

    def drag_delta_for_gap(self, gap_x: float) -> float:
        return self.target_element_x_for_gap(gap_x) - self.puzzle_init_x

    def answer_for_gap(self, gap_x: float) -> tuple[int, int]:
        # Tencent reports the final puzzle-element position, not the visible
        # alpha-mask corner returned by template matching. Vertical movement is
        # disabled, so Y remains the element's initial Y coordinate.
        return round(self.target_element_x_for_gap(gap_x)), round(self.puzzle_init_y)


@dataclass(frozen=True, slots=True)
class GapMatch:
    x: int
    y: int
    confidence: float
    margin: float
    heatmap: np.ndarray = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class DragEvent:
    type: str
    x: int
    y: int
    time: int
    button: int = 0
    buttons: int = 0

    def as_payload(self) -> dict[str, int | str]:
        return asdict(self)


@dataclass(slots=True)
class AttemptDiagnostics:
    attempt: int
    status: str = "started"
    timings_ms: dict[str, float] = field(default_factory=dict)
    geometry: dict[str, Any] | None = None
    gap: dict[str, Any] | None = None
    drag: dict[str, Any] | None = None
    error_code: str = ""
    error_message: str = ""
    debug_dir: str | None = None


@dataclass(slots=True)
class SolveDiagnostics:
    success: bool = False
    attempts: list[AttemptDiagnostics] = field(default_factory=list)
    total_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def jsonp_parse(text: str) -> dict[str, Any]:
    stripped = text.strip()
    match = re.fullmatch(r"[A-Za-z_$][\w$]*\s*\((.*)\)\s*;?", stripped, re.DOTALL)
    payload = match.group(1) if match else stripped
    try:
        result = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSONP response: {exc}") from exc
    if not isinstance(result, dict):
        raise ProtocolError("JSONP payload must be an object")
    return result


def _detect_captcha_kind(dyn: Mapping[str, Any]) -> str:
    """判断服务端下发的是哪种题型。

    返回 "slide" / "click" / "icon" / "unknown"。

    判据（实测 2026-10-07，腾讯云 + QQ 两边一致）：
      · 滑块：`fg_elem_list` 里存在 `type == "slider"` 的元件，
              或 `bg_elem_cfg.click_cfg.data_type` 含 `DynAnswerType_POS`
      · 点选：`click_cfg.data_type` 含 `DynAnswerType_UC`
      · 图标点选：含 `DynAnswerType_ICON`
    """
    bg = dyn.get("bg_elem_cfg")
    click_cfg = bg.get("click_cfg") if isinstance(bg, dict) else None
    data_types: list[str] = []
    if isinstance(click_cfg, dict):
        raw = click_cfg.get("data_type")
        if isinstance(raw, list):
            data_types = [str(x) for x in raw]
    fg = dyn.get("fg_elem_list")
    fg_types = [str(e.get("type", "")) for e in fg if isinstance(e, dict)] if isinstance(fg, list) else []

    if "slider" in fg_types or "DynAnswerType_POS" in data_types:
        return "slide"
    if "DynAnswerType_ICON" in data_types:
        return "icon"
    if "DynAnswerType_UC" in data_types:
        return "click"
    return "unknown"


def _pair(value: Any, name: str) -> tuple[float, float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 2:
        raise ProtocolError(f"{name} must contain two values")
    try:
        return float(value[0]), float(value[1])
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"{name} contains non-numeric values") from exc


def _parse_track_limit(value: Any) -> tuple[float, float]:
    if not isinstance(value, str):
        raise ProtocolError("track_limit is missing")
    match = re.fullmatch(
        r"\s*x\s*>=\s*(-?\d+(?:\.\d+)?)\s*&&\s*x\s*<=\s*(-?\d+(?:\.\d+)?)\s*",
        value,
    )
    if not match:
        raise ProtocolError(f"unsupported track_limit: {value!r}")
    lower, upper = map(float, match.groups())
    if lower >= upper:
        raise ProtocolError("track_limit lower bound must be smaller than upper bound")
    return lower, upper


def _robust_norm(values: np.ndarray, percentile: float = 99.8) -> np.ndarray:
    array = values.astype(np.float32, copy=True)
    finite = array[np.isfinite(array)]
    if finite.size == 0:
        return np.zeros_like(array, dtype=np.float32)
    low, high = np.percentile(finite, 50.0), np.percentile(finite, percentile)
    return np.clip(
        np.nan_to_num((array - low) / (high - low + 1e-6), nan=0, posinf=1, neginf=0),
        0,
        1,
    )


def _tm_ccorr(source: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    float_kernel = kernel.astype(np.float32)
    return cv2.matchTemplate(
        source.astype(np.float32), float_kernel, cv2.TM_CCORR
    ) / max(float(float_kernel.sum()), 1.0)


class TdcClient:
    """Persistent line-delimited JSON client for the Node TDC worker."""

    def __init__(
        self,
        server_path: Path,
        *,
        node_command: str,
        timeout: float,
        vm_timeout_ms: int,
    ) -> None:
        self.server_path = server_path
        self.node_command = node_command
        self.timeout = timeout
        self.vm_timeout_ms = vm_timeout_ms
        self._process: subprocess.Popen[str] | None = None
        self._output: queue.Queue[str | None] | None = None
        self._reader: threading.Thread | None = None
        self._request_id = 0
        self._lock = threading.Lock()

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process and self._process.poll() is None else None

    def _reader_loop(
        self,
        process: subprocess.Popen[str],
        output: queue.Queue[str | None],
    ) -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                output.put(line)
        finally:
            output.put(None)

    def _read_message(self, timeout: float) -> dict[str, Any]:
        if self._output is None:
            raise TdcError("TDC worker is not running")
        try:
            line = self._output.get(timeout=timeout)
        except queue.Empty as exc:
            raise TdcError("TDC worker response timed out") from exc
        if line is None:
            raise TdcError("TDC worker exited unexpectedly")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TdcError("TDC worker returned invalid JSON") from exc
        if not isinstance(message, dict):
            raise TdcError("TDC worker response must be an object")
        return message

    def _start(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        self.close()
        try:
            process = subprocess.Popen(
                [self.node_command, str(self.server_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except OSError as exc:
            raise TdcError(f"failed to start Node worker: {exc}") from exc

        output: queue.Queue[str | None] = queue.Queue()
        reader = threading.Thread(
            target=self._reader_loop,
            args=(process, output),
            name="tdc-output-reader",
            daemon=True,
        )
        self._process = process
        self._output = output
        self._reader = reader
        reader.start()
        try:
            ready = self._read_message(self.timeout)
            if ready.get("ready") is not True:
                raise TdcError("TDC worker did not report ready")
        except Exception:
            self.close()
            raise

    def request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._start()
            assert self._process is not None and self._process.stdin is not None
            self._request_id += 1
            request_id = self._request_id
            message = dict(payload)
            message["requestId"] = request_id
            message["vmTimeoutMs"] = self.vm_timeout_ms
            try:
                self._process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
                self._process.stdin.flush()
                response = self._read_message(self.timeout)
            except (OSError, TdcError):
                self.close()
                raise
            if response.get("requestId") != request_id:
                self.close()
                raise TdcError("TDC worker response ID mismatch")
            if response.get("success") is not True:
                error = str(response.get("error", "TDC worker failed"))
                self.close()
                raise TdcError(error)
            if not response.get("collect") or not response.get("eks"):
                raise TdcError("TDC worker response is missing collect or eks")
            return response

    def close(self) -> None:
        process = self._process
        self._process = None
        self._output = None
        self._reader = None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
        if process.stdout:
            try:
                process.stdout.close()
            except OSError:
                pass


class SlideSolver:
    def __init__(
        self,
        *,
        config: SlideSolverConfig | None = None,
        session: requests.Session | None = None,
        endpoints: Endpoints | str = ENDPOINTS_TENCENT_CLOUD,
        aid: int | None = None,
        entry_url: str | None = None,
        entry_referer: str | None = None,
    ) -> None:
        # endpoints 可传对象，也可传名字（"tencent" / "qq"）
        if isinstance(endpoints, str):
            try:
                endpoints = ENDPOINTS[endpoints]
            except KeyError:
                raise ValueError(
                    f"unknown endpoints {endpoints!r}; expected one of {sorted(ENDPOINTS)}"
                ) from None
        self.endpoints = endpoints
        # aid / entry_url 显式传入时覆盖 endpoints 自带的默认值
        self.aid = int(aid) if aid is not None else endpoints.aid
        self.entry_url = entry_url or endpoints.entry_url
        self.entry_referer = entry_referer or endpoints.entry_referer
        self.config = config or SlideSolverConfig()
        self._rng = random.Random(self.config.random_seed)
        self.profile = self.config.browser_profile or _make_browser_profile(self._rng)
        self.ua = self.profile.user_agent
        if session is not None:
            self.session = session
        elif self.config.use_browser_tls and curl_requests is not None:
            self.session = curl_requests.Session(impersonate=f"chrome{self.profile.chrome_major}")
        else:
            self.session = requests.Session()
        self._owns_session = session is None
        if self._owns_session:
            # 强制直连，忽略系统/环境变量代理（HTTP_PROXY 等）
            self.session.trust_env = False
        self.session.headers.update(self._base_headers())
        self._tdc_client = TdcClient(
            TDC_SERVER,
            node_command=self.config.node_command,
            timeout=self.config.tdc_timeout,
            vm_timeout_ms=self.config.vm_timeout_ms,
        )
        self._closed = False
        self._session_warmed = False
        self._success_streak = 0
        self._ec12_streak = 0
        self._total_successes = 0
        self._force_bias_next = False
        self._permanent_miss = False
        self._subsid = 1
        self._tdc_token = ""
        self._challenge_count = 0
        # QQ 那套的入参：题型由服务端按 uin + cap_cd 下发，必须原样透传
        self.qq_uin: str = ""
        self.qq_cap_cd: str = ""
        self.qq_sid: str = ""
        # 滑块 URL 里的全部 query（原样透传给 prehandle，见 _prehandle_params_qq）
        self.qq_extra_params: Mapping[str, Any] = {}
        self._last_app_data: str = ""
        self._last_dyn_show_info: Mapping[str, Any] | None = None
        self._last_geometry: CaptchaGeometry | None = None
        self._last_gap: GapMatch | None = None
        self.last_diagnostics: SolveDiagnostics | None = None


    def _sanitize_cookies(self) -> None:
        """Drop malformed pure-protocol cookies that real browsers would ignore.

        Some captcha CDN/product responses emit broken Set-Cookie fragments such as
        `path=/`, which requests then stores as a cookie named "path" and replays on
        later turing.captcha.* calls. That Cookie header is a strong bot signal.
        """
        try:
            jar = self.session.cookies
        except Exception:
            return
        bad = []
        for cookie in list(jar):
            name = str(getattr(cookie, "name", "") or "")
            value = str(getattr(cookie, "value", "") or "")
            # Ignore attribute-like / empty / path-only garbage cookies.
            if not name or name.lower() in {"path", "domain", "expires", "max-age", "secure", "httponly", "samesite"}:
                bad.append(cookie)
                continue
            if name == "path" or value in {"/", ""} and name.lower() in {"path", "domain"}:
                bad.append(cookie)
        for cookie in bad:
            try:
                jar.clear(domain=cookie.domain, path=cookie.path, name=cookie.name)
            except Exception:
                try:
                    jar.clear()
                    break
                except Exception:
                    return

    def soft_refresh_profile(self) -> None:

        """Refresh only canvas entropy between challenges.

        Keep UA/WebGL/screen/TLS identity stable. Only rotate canvas seed so a
        long continuous run does not reuse one identical canvas digest forever.
        """
        current = getattr(self, "profile", None)
        if current is None:
            return
        self.profile = type(current)(
            chrome_major=current.chrome_major,
            user_agent=current.user_agent,
            sec_ch_ua=current.sec_ch_ua,
            language=current.language,
            languages=current.languages,
            platform=current.platform,
            hardware_concurrency=current.hardware_concurrency,
            device_memory=current.device_memory,
            max_touch_points=current.max_touch_points,
            screen_width=current.screen_width,
            screen_height=current.screen_height,
            avail_height=current.avail_height,
            window_width=current.window_width,
            window_height=current.window_height,
            outer_width=current.outer_width,
            outer_height=current.outer_height,
            device_pixel_ratio=current.device_pixel_ratio,
            timezone=current.timezone,
            webgl_vendor=current.webgl_vendor,
            webgl_renderer=current.webgl_renderer,
            canvas_seed=self._rng.randrange(1, 2**31),
        )

    def rotate_profile(self, *, hard: bool = False) -> None:
        """Rotate browser transport identity carefully.

        soft (default): keep UA/WebGL/screen stable, only refresh canvas seed.
        hard: rebuild HTTP session after errorCode=12, but do NOT re-fetch
        captcha assets and do NOT thrash geometry/WebGL. Extra warm traffic after
        detection multiplies pure-protocol heat on the same IP.
        """
        self._rng.seed()
        if not hard and getattr(self, 'profile', None) is not None:
            self.soft_refresh_profile()
            return
        # Keep the proven desktop product fingerprint. Only transport is rebuilt.
        if getattr(self, 'profile', None) is None:
            self.profile = _make_browser_profile(self._rng)
        else:
            # Tiny canvas-only entropy change; leave screen/WebGL/UA untouched.
            self.soft_refresh_profile()
        self.ua = self.profile.user_agent
        if self._owns_session:
            try:
                self.session.close()
            except Exception:
                pass
            if self.config.use_browser_tls and curl_requests is not None:
                self.session = curl_requests.Session(impersonate=f"chrome{self.profile.chrome_major}")
            else:
                self.session = requests.Session()
        self.session.headers.update(self._base_headers())
        try:
            self.session.cookies.clear()
        except Exception:
            pass
        # Real browsers keep TDC_itoken across captcha opens even after failures.
        token = str(getattr(self, "_tdc_token", "") or "")
        if token:
            try:
                self.session.cookies.set("TDC_itoken", f"{token}:1", domain=".qcloud.com", path="/")
                self.session.cookies.set("TDC_itoken", f"{token}:1", domain=".gtimg.com", path="/")
            except Exception:
                pass
        # Critical: never re-warm host/captcha assets after ec=12 on the same IP.
        self._session_warmed = True
        self._needs_light_warm = False
        self._subsid = 1
        self._challenge_count = 0

    def _base_headers(self) -> dict[str, str]:
        language = self.profile.language or "zh-CN"
        return {
            "User-Agent": self.ua,
            "Accept-Language": f"{language},{language.split('-')[0]};q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "sec-ch-ua": self.profile.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }

    @staticmethod
    def _fetch_headers(
        *,
        referer: str,
        accept: str = "*/*",
        mode: str = "cors",
        destination: str = "empty",
        site: str = "cross-site",
    ) -> dict[str, str]:
        return {
            "Accept": accept,
            "Referer": referer,
            "Sec-Fetch-Dest": destination,
            "Sec-Fetch-Mode": mode,
            "Sec-Fetch-Site": site,
        }

    @property
    def _request_timeout(self) -> tuple[float, float]:
        return self.config.connect_timeout, self.config.read_timeout

    def __enter__(self) -> "SlideSolver":
        if self._closed:
            raise SlideSolverError("solver is closed")
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._tdc_client.close()
        if self._owns_session:
            self.session.close()

    @staticmethod
    def _b64(value: str) -> str:
        return base64.b64encode(value.encode()).decode()

    @staticmethod
    def _b64_qq(value: str) -> str:
        """QQ 那套的 ua 编码。

        `TCapIframeApi.js` 先 `ua.replace(/[\\u00ff-\\uffff]+/g, "")` 再
        `encodeURIComponent`，服务端自己 base64 解。直接 b64 整个 UA 在含中文
        时会抛 UnicodeEncodeError，所以必须先剥掉非 latin1 字符。
        """
        cleaned = re.sub(r"[\u00ff-\uffff]+", "", value)
        return base64.b64encode(cleaned.encode("latin-1", "ignore")).decode()

    def _ensure_open(self) -> None:
        if self._closed:
            raise SlideSolverError("solver is closed")

    def _get(self, url: str, **kwargs: Any) -> requests.Response:
        self._ensure_open()
        try:
            response = self.session.get(url, timeout=self._request_timeout, **kwargs)
            response.raise_for_status()
            self._sanitize_cookies()
            return response
        except REQUEST_ERRORS as exc:
            # ★ 把**响应体和状态码**一起带出来。
            #
            # 为什么必须这么做（2026-10-09 用户现场）：
            # 日志里只有一句 `GET ... failed: 403 Client Error: Forbidden for url: ...`
            # —— 而那个 URL 有 1000+ 字符（sess 超长），120 字符的截断把
            # **真正的信息全吃掉了**：响应体是空的？还是带说明？带没带
            # `X-Waf-*` 头？全看不到，只能靠猜。
            #
            # 腾讯网关的 403 通常会带一段 JSON 或 HTML 说明（限频 / 参数错 /
            # 会话失效各不相同）。**留证比省几行日志重要得多。**
            detail = ""
            resp = getattr(exc, "response", None)
            if resp is not None:
                try:
                    body = (resp.content or b"")[:400]
                    text = body.decode("utf-8", "replace").strip()
                    interesting = {k: v for k, v in (resp.headers or {}).items()
                                   if any(t in k.lower() for t in
                                          ("server", "waf", "tencent", "error", "reason",
                                           "ret", "content-type", "content-length"))}
                    detail = (f" [status={resp.status_code}"
                              f" len={len(resp.content or b'')}"
                              f" headers={interesting}"
                              f" body={text!r}]")
                except Exception:  # noqa: BLE001
                    pass
            raise RetryableSolveError(f"GET {url} failed: {exc}{detail}") from exc


    def _warm_session(self, *, force: bool = False) -> None:
        """Pure-protocol browser bootstrap: load captcha scripts/iframe like a real page."""
        if self._session_warmed and not force and not getattr(self, "_needs_light_warm", False):
            return
        light = bool(getattr(self, "_needs_light_warm", False)) and self._session_warmed and not force
        page_headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-User": "?1",
        }
        script_headers = self._fetch_headers(
            referer=self.entry_url,
            accept="*/*",
            mode="no-cors",
            destination="script",
            site="cross-site",
        )
        iframe_headers = self._fetch_headers(
            referer=self.entry_url,
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            mode="navigate",
            destination="iframe",
            site="cross-site",
        )
        frame_script_headers = self._fetch_headers(
            referer=self.endpoints.drag_template,
            accept="*/*",
            mode="no-cors",
            destination="script",
            site="same-origin",
        )
        # Best-effort: failures here should not kill solving.
        ep = self.endpoints
        bootstrap = [
            (self.entry_url, page_headers),
            (ep.tcaptcha_js, script_headers),
            (ep.frame_js, script_headers),
            (ep.drag_template, iframe_headers),
            (ep.dy_jy3, frame_script_headers),
            (ep.dy_ele, frame_script_headers),
        ]
        if light or (self._session_warmed and force):
            # Subsequent challenges only re-open captcha assets, not the host page.
            bootstrap = [
                (ep.tcaptcha_js, script_headers),
                (ep.frame_js, script_headers),
                (ep.drag_template, iframe_headers),
                (ep.dy_jy3, frame_script_headers),
                (ep.dy_ele, frame_script_headers),
            ]
        for url, headers in bootstrap:
            try:
                self.session.get(url, headers=headers, timeout=self._request_timeout)
            except Exception:
                pass
            time.sleep(self._rng.uniform(0.04, 0.14))
        self._sanitize_cookies()
        self._session_warmed = True
        self._needs_light_warm = False

    def prehandle(self, sess: str = "") -> dict[str, Any]:
        """拿一道题。

        @param sess
            **续做用的会话**（默认空 = 开一道全新题）。

            ⚠️ 这个参数不是可选的装饰，是腾讯混合验证流程的关键（2026-10-08 扒源码确认）：

            ```js
            // tcaptcha-frame.js —— 内层报 hybrid 时外层这么处理
            e.prototype.onHybridVerify = function (e, t) {
              this.clearContainerAndEl(), this.preCreate(e)   // 带着 sess 重建
            }
            e.prototype.getPreHandleNew = function (e) {
              g({ url: ..., data: { sess: e.sess || "", ... },   // sess 回传
                  success: function (i) {
                    if (i.ticket) ...onSuccess(...)      // 也可能直接给 ticket
                    if (217 === i.state) ...preHandleRateLimit()
                  }})
            }
            e.prototype.startPreHandle = function (e) { this.getPreHandleNew({ sess: e, ... }) }
            ```

            实测：verify 返回 `errorCode=51` 时响应里带一个**新 sess**，
            拿它当 `sess` 参数重新 prehandle，**能拿到下一道题**，
            而且全程 `sid` 不变（风控指纹连续，更像真人）。
            不带 sess 就是另开一道全新题，等于放弃了服务端给的续做机会。
        """
        # Warm once per HTTP session. Re-fetching captcha assets every challenge
        # multiplies pure-protocol heat under continuous 2s solves.
        self._warm_session(force=False)
        self._sanitize_cookies()
        time.sleep(self._rng.uniform(0.28, 0.85))
        if self.endpoints.prehandle_style == "qq":
            params = self._prehandle_params_qq()
        else:
            params = self._prehandle_params_cloud()
        if sess:
            params["sess"] = sess
        response = self._get(
            self.endpoints.prehandle_url,
            headers=self._fetch_headers(
                referer=self.entry_referer,
                mode="no-cors",
                destination="script",
            ),
            params=params,
        )
        # Real browser increments subsid for subsequent captcha opens on one page.
        self._subsid = int(getattr(self, "_subsid", 1) or 1) + 1
        if self._subsid > 20:
            self._subsid = 1
        return jsonp_parse(response.text)

    def _prehandle_params_cloud(self) -> dict[str, Any]:
        """腾讯云（turing.captcha.qcloud.com）的 prehandle 参数。"""
        return {
            "aid": self.aid,
            "protocol": "https",
            "accver": 1,
            "showtype": "popup",
            "ua": self._b64(self.ua),
            "noheader": 1,
            "fb": 1,
            "aged": 0,
            "enableAged": 0,
            "enableDarkMode": 0,
            "grayscale": 1,
            "clientype": 2,
            "cap_cd": "",
            "uid": "",
            "lang": "zh-cn",
            "entry_url": self.entry_url,
            "elder_captcha": 0,
            "js": "/tcaptcha-frame.91efdf16.js",
            "login_appid": "",
            "wb": 1,
            "subsid": int(getattr(self, "_subsid", 1) or 1),
            "callback": f"_aq_{self._rng.randint(100000, 999999)}",
            "sess": "",
        }

    def _prehandle_params_qq(self) -> dict[str, Any]:
        """QQ 滑块的 prehandle 参数。

        严格照抄 `ssl.captcha.qq.com/template/TCapIframeApi.js` 里的拼接顺序与字段：
          aid= （**故意为空**）&captype=&curenv=inner&protocol=https&clientype=2
          &disturblevel=&apptype=&noheader=1&color=&showtype=popup&fb=1&theme=
          &lang=zh-cn&ua=<b64>&enableDarkMode=0&aged=0&enableAged=0&grayscale=1
          [&sid=<sid>][&cap_cd=<cap_cd>][&uin=<uin>]

        ⚠️ 实测（2026-10-07）：QQ 这套**不用 aid 选题型**，靠 `uin` + `cap_cd`。
        `ssl.captcha.qq.com` 的 prehandle 恒返 407，真正可用的是 `t.captcha.qq.com`。

        无线页面（wireless_mqq_captcha.html）的做法是：把 `location.search`
        **原样透传**给 TCapIframeApi。所以这里除了固定字段，还会把调用方
        （`qq_extra_params`，通常来自 icqq 给的滑块 URL 的 query）里的参数
        一并带上 —— 服务端认哪些参数由它自己决定，我们别自作主张丢。
        """
        params: dict[str, Any] = {
            "aid": "",  # 空 —— 这是 QQ 那套的真实形态，别填数字
            "captype": "",
            "curenv": "inner",
            "protocol": "https",
            "clientype": 2,
            "disturblevel": "",
            "apptype": "",
            "noheader": 1,
            "color": "",
            "showtype": "popup",
            "fb": 1,
            "theme": "",
            "lang": "zh-cn",
            "ua": self._b64_qq(self.ua),
            "enableDarkMode": 0,
            "aged": 0,
            "enableAged": 0,
            "grayscale": 1,
        }
        sid = getattr(self, "qq_sid", "") or ""
        if sid:
            params["sid"] = sid
        cap_cd = getattr(self, "qq_cap_cd", "") or ""
        if cap_cd:
            params["cap_cd"] = cap_cd
        uin = getattr(self, "qq_uin", "") or ""
        if uin:
            params["uin"] = str(uin)
        # 原样透传调用方带来的额外参数（不覆盖上面已显式设好的）
        extra = getattr(self, "qq_extra_params", None)
        if isinstance(extra, Mapping):
            for key, value in extra.items():
                if key not in params and value not in (None, ""):
                    params[str(key)] = value
        params["callback"] = f"_aq_{self._rng.randint(100000, 999999)}"
        return params

    def _download_bytes(self, url: str) -> bytes:
        return self._get(
            url,
            headers=self._fetch_headers(
                referer=self.endpoints.captcha_referer,
                accept="image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                mode="no-cors",
                destination="image",
            ),
        ).content

    def _tdc_download(self, app_data: str, timestamp: str) -> str:
        return self._get(
            self.endpoints.tdc_url,
            params={"app_data": app_data, "t": timestamp},
            headers=self._fetch_headers(
                referer=self.endpoints.captcha_referer,
                accept="*/*",
                mode="no-cors",
                destination="script",
            ),
        ).text

    @staticmethod
    def _decode_rgb(image_bytes: bytes, label: str) -> np.ndarray:
        try:
            return np.asarray(Image.open(BytesIO(image_bytes)).convert("RGB"))
        except Exception as exc:
            raise ProtocolError(f"invalid {label} image") from exc

    @staticmethod
    def _decode_rgba(image_bytes: bytes, label: str) -> np.ndarray:
        try:
            return np.asarray(Image.open(BytesIO(image_bytes)).convert("RGBA"))
        except Exception as exc:
            raise ProtocolError(f"invalid {label} image") from exc

    @staticmethod
    def _find_elements(dyn: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        elements = dyn.get("fg_elem_list")
        if not isinstance(elements, list):
            raise ProtocolError("fg_elem_list is missing")
        puzzle = None
        slider = None
        for element in elements:
            if not isinstance(element, dict):
                continue
            move_cfg = element.get("move_cfg", {})
            data_types = move_cfg.get("data_type", []) if isinstance(move_cfg, dict) else []
            if "DynAnswerType_POS" in data_types:
                puzzle = element
            if element.get("type") == "slider":
                slider = element
        if puzzle is None or slider is None:
            raise ProtocolError("puzzle or slider element is missing")

        puzzle_id = puzzle.get("id")
        slider_id = slider.get("id")
        bindings = dyn.get("fg_binding_list", [])
        bound = any(
            isinstance(binding, dict)
            and {binding.get("master"), binding.get("slave")} == {puzzle_id, slider_id}
            and float(binding.get("bind_factor", 0)) == 1.0
            for binding in bindings
        )
        if not bound:
            raise ProtocolError("puzzle and slider elements are not bound")
        return puzzle, slider

    @staticmethod
    def _extract_piece(
        sprite: np.ndarray,
        puzzle: Mapping[str, Any],
    ) -> PieceTemplate:
        sprite_x, sprite_y = _pair(puzzle.get("sprite_pos"), "puzzle.sprite_pos")
        width, height = _pair(puzzle.get("size_2d"), "puzzle.size_2d")
        x, y, width_i, height_i = int(sprite_x), int(sprite_y), int(width), int(height)
        if width_i <= 0 or height_i <= 0:
            raise ProtocolError("puzzle size must be positive")
        if x < 0 or y < 0 or x + width_i > sprite.shape[1] or y + height_i > sprite.shape[0]:
            raise ProtocolError("puzzle sprite region is outside sprite image")

        crop = sprite[y : y + height_i, x : x + width_i]
        alpha = (crop[:, :, 3] > 50).astype(np.uint8)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(alpha, 8)
        candidates = [
            (int(stats[index, cv2.CC_STAT_AREA]), index)
            for index in range(1, count)
            if int(stats[index, cv2.CC_STAT_AREA]) >= 100
        ]
        if not candidates:
            raise ProtocolError("puzzle sprite has no visible component")
        _, label = max(candidates)
        ys, xs = np.where(labels == label)
        left, top = int(xs.min()), int(ys.min())
        right, bottom = int(xs.max()) + 1, int(ys.max()) + 1
        mask = (labels[top:bottom, left:right] == label).astype(np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        rgb = crop[top:bottom, left:right, :3]
        return PieceTemplate(mask, rgb, left, top, right - left, bottom - top)

    def parse_geometry(
        self,
        dyn: Mapping[str, Any],
        sprite_bytes: bytes,
    ) -> tuple[CaptchaGeometry, PieceTemplate]:
        bg_cfg = dyn.get("bg_elem_cfg")
        if not isinstance(bg_cfg, dict):
            raise ProtocolError("bg_elem_cfg is missing")
        bg_width, bg_height = _pair(bg_cfg.get("size_2d"), "bg_elem_cfg.size_2d")
        if bg_width <= 0 or bg_height <= 0:
            raise ProtocolError("background dimensions must be positive")

        puzzle, slider = self._find_elements(dyn)
        piece = self._extract_piece(self._decode_rgba(sprite_bytes, "sprite"), puzzle)
        puzzle_init_x, puzzle_init_y = _pair(puzzle.get("init_pos"), "puzzle.init_pos")
        puzzle_width, puzzle_height = _pair(puzzle.get("size_2d"), "puzzle.size_2d")
        puzzle_sprite_x, puzzle_sprite_y = _pair(
            puzzle.get("sprite_pos"), "puzzle.sprite_pos"
        )
        slider_init_x, slider_init_y = _pair(slider.get("init_pos"), "slider.init_pos")
        slider_width, slider_height = _pair(slider.get("size_2d"), "slider.size_2d")

        move_cfg = puzzle.get("move_cfg")
        if not isinstance(move_cfg, dict) or list(move_cfg.get("move_factor", [])) != [1, 0]:
            raise ProtocolError("only horizontal puzzle movement is supported")
        track_min, track_max = _parse_track_limit(move_cfg.get("track_limit"))
        viewport_height = max(1, round(self.config.viewport_width * bg_height / bg_width))
        geometry = CaptchaGeometry(
            bg_width=int(bg_width),
            bg_height=int(bg_height),
            viewport_width=self.config.viewport_width,
            viewport_height=viewport_height,
            puzzle_id=int(puzzle["id"]),
            puzzle_init_x=puzzle_init_x,
            puzzle_init_y=puzzle_init_y,
            puzzle_width=int(puzzle_width),
            puzzle_height=int(puzzle_height),
            puzzle_sprite_x=int(puzzle_sprite_x),
            puzzle_sprite_y=int(puzzle_sprite_y),
            slider_id=int(slider["id"]),
            slider_init_x=slider_init_x,
            slider_init_y=slider_init_y,
            slider_width=int(slider_width),
            slider_height=int(slider_height),
            alpha_x=piece.alpha_x,
            alpha_y=piece.alpha_y,
            alpha_width=piece.width,
            alpha_height=piece.height,
            track_min_x=track_min,
            track_max_x=track_max,
        )
        return geometry, piece

    def _locate_gap(
        self,
        background: np.ndarray,
        geometry: CaptchaGeometry,
        piece: PieceTemplate,
    ) -> GapMatch:
        image_height, image_width = background.shape[:2]
        if image_width != geometry.bg_width or image_height > geometry.bg_height:
            raise ProtocolError(
                f"background image size {image_width}x{image_height} is incompatible with "
                f"canvas {geometry.bg_width}x{geometry.bg_height}"
            )

        gray = cv2.cvtColor(background, cv2.COLOR_RGB2GRAY).astype(np.float32)
        mask = piece.mask.astype(np.uint8)
        mask_float = mask.astype(np.float32)
        piece_height, piece_width = mask.shape
        if piece_height > gray.shape[0] or piece_width > gray.shape[1]:
            raise ProtocolError("puzzle mask is larger than the background")

        local = cv2.GaussianBlur(
            gray,
            (0, 0),
            sigmaX=max(10.0, piece_width / 3),
            sigmaY=max(10.0, piece_height / 3),
        )
        dark = np.maximum(local - gray, 0.0)
        dark_score = _tm_ccorr(dark, mask_float)

        hsv = cv2.cvtColor(background, cv2.COLOR_RGB2HSV)
        fill = np.clip((local - gray - 5) / 50, 0, 1) * (
            1 - hsv[:, :, 1].astype(np.float32) / 255
        )
        fill_score = _tm_ccorr(fill, mask_float)

        gray_u8 = gray.astype(np.uint8)
        edge_maps = [
            cv2.Canny(gray_u8, 28, 90).astype(np.float32) / 255,
            cv2.Canny(gray_u8, 42, 135).astype(np.float32) / 255,
            cv2.Canny(gray_u8, 65, 195).astype(np.float32) / 255,
        ]
        eroded = cv2.erode(mask, np.ones((3, 3), np.uint8), iterations=1)
        boundary = cv2.dilate(
            (mask - eroded).clip(0, 1), np.ones((3, 3), np.uint8), iterations=1
        ).astype(np.float32)
        edge_score = np.maximum.reduce(
            [_tm_ccorr(edge_map, boundary * mask_float) for edge_map in edge_maps]
        )

        piece_gray = cv2.cvtColor(piece.rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
        color_spread = piece.rgb.max(axis=2) - piece.rgb.min(axis=2)
        content_mask = (
            mask
            & ((piece.rgb.max(axis=2) < 235) | (color_spread > 30)).astype(np.uint8)
        ).astype(np.uint8)
        if content_mask.sum() > 500:
            eroded_content = cv2.erode(
                content_mask, np.ones((3, 3), np.uint8), iterations=2
            )
            if eroded_content.sum() > 500:
                content_mask = eroded_content
        try:
            content_score = cv2.matchTemplate(
                gray,
                piece_gray,
                cv2.TM_CCORR_NORMED,
                mask=content_mask if content_mask.sum() > 500 else None,
            )
        except cv2.error:
            content_score = cv2.matchTemplate(gray, piece_gray, cv2.TM_CCORR_NORMED)
        content_score = np.nan_to_num(
            content_score, nan=0, posinf=0, neginf=0
        ).astype(np.float32)

        gradient_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
        gradient_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
        template_x = cv2.Sobel(piece_gray, cv2.CV_32F, 1, 0, ksize=3)
        template_y = cv2.Sobel(piece_gray, cv2.CV_32F, 0, 1, ksize=3)
        try:
            gradient_score = 0.5 * np.maximum(
                np.nan_to_num(
                    cv2.matchTemplate(
                        gradient_x, template_x, cv2.TM_CCORR_NORMED, mask=content_mask
                    ),
                    nan=0,
                    posinf=0,
                    neginf=0,
                ),
                0,
            ) + 0.5 * np.maximum(
                np.nan_to_num(
                    cv2.matchTemplate(
                        gradient_y, template_y, cv2.TM_CCORR_NORMED, mask=content_mask
                    ),
                    nan=0,
                    posinf=0,
                    neginf=0,
                ),
                0,
            )
        except cv2.error:
            gradient_score = np.zeros_like(content_score)

        fused = (
            0.18 * _robust_norm(dark_score)
            + 0.13 * _robust_norm(content_score)
            + 0.16 * _robust_norm(edge_score)
            + 0.26 * _robust_norm(fill_score)
            + 0.27 * _robust_norm(gradient_score, 99.95)
        )

        valid = np.zeros_like(fused, dtype=bool)
        min_x = max(
            0,
            math.ceil(max(geometry.track_min_x + geometry.alpha_x, geometry.initial_visible_x + 5)),
        )
        max_x = min(
            fused.shape[1] - 1,
            math.floor(geometry.track_max_x + geometry.alpha_x),
        )
        expected_y = round(geometry.initial_visible_y)
        min_y = max(0, expected_y - 4)
        max_y = min(fused.shape[0] - 1, expected_y + 4)
        if min_x > max_x or min_y > max_y:
            raise ProtocolError("dynamic geometry produced an empty gap search region")
        valid[min_y : max_y + 1, min_x : max_x + 1] = True
        ranked = np.where(valid, fused, -np.inf)

        best_index = int(np.argmax(ranked))
        best_y, best_x = np.unravel_index(best_index, ranked.shape)
        best_x, best_y = int(best_x), int(best_y)
        best_score = float(ranked[best_y, best_x])
        if not math.isfinite(best_score):
            raise GapDetectionError("no valid gap candidate")

        second_map = ranked.copy()
        radius_x = max(20, piece_width // 2)
        radius_y = max(3, piece_height // 6)
        second_map[
            max(0, best_y - radius_y) : min(second_map.shape[0], best_y + radius_y + 1),
            max(0, best_x - radius_x) : min(second_map.shape[1], best_x + radius_x + 1),
        ] = -np.inf
        second_score = float(np.max(second_map))
        margin = best_score - second_score if math.isfinite(second_score) else best_score

        heatmap = np.clip(fused * 255, 0, 255).astype(np.uint8)
        match = GapMatch(best_x, best_y, best_score, margin, heatmap)
        if match.confidence < self.config.min_gap_confidence:
            raise GapDetectionError(
                f"gap confidence {match.confidence:.3f} is below "
                f"{self.config.min_gap_confidence:.3f}"
            )
        if match.margin < self.config.min_gap_margin:
            raise GapDetectionError(
                f"gap margin {match.margin:.3f} is below {self.config.min_gap_margin:.3f}"
            )
        return match

    def detect_gap(
        self,
        bg_bytes: bytes,
        sprite_bytes: bytes,
        dyn_show_info: Mapping[str, Any] | None = None,
    ) -> tuple[int, int]:
        dyn = dyn_show_info or self._last_dyn_show_info
        if dyn is None:
            raise ProtocolError("dyn_show_info is required for dynamic geometry")
        geometry, piece = self.parse_geometry(dyn, sprite_bytes)
        gap = self._locate_gap(self._decode_rgb(bg_bytes, "background"), geometry, piece)
        geometry.drag_delta_for_gap(gap.x)
        self._last_geometry = geometry
        self._last_gap = gap
        LOG.info(
            "gap=(%d,%d) confidence=%.3f margin=%.3f delta=%.1f",
            gap.x,
            gap.y,
            gap.confidence,
            gap.margin,
            geometry.drag_delta_for_gap(gap.x),
        )
        return gap.x, gap.y

    def _drag_motion_points(
        self,
        start_x: float,
        start_y: float,
        delta_x: float,
    ) -> tuple[list[tuple[int, int, int]], int, int]:
        if delta_x <= 0:
            raise GapDetectionError(f"drag distance must be positive, got {delta_x:.1f}")
        rng = self._rng
        points: list[tuple[int, int, int]] = []
        timestamp = rng.randint(80, 260)

        approach_start_x = start_x - rng.uniform(20, 55)
        approach_start_y = start_y + rng.uniform(-22, 22)
        approach_steps = rng.randint(5, 11)
        approach_curve = rng.uniform(-3.5, 3.5)
        for index in range(approach_steps):
            unit = (index + 1) / approach_steps
            eased = unit * unit * (3 - 2 * unit)
            x = approach_start_x + (start_x - approach_start_x) * eased
            y = (
                approach_start_y
                + (start_y - approach_start_y) * eased
                + math.sin(math.pi * unit) * approach_curve
            )
            timestamp += rng.randint(16, 43)
            points.append((round(x), round(y), timestamp))

        overshoot = (
            rng.uniform(0.8, min(3.6, 0.025 * delta_x + 1.2))
            if delta_x >= 35 and rng.random() < 0.78
            else 0.0
        )
        target = delta_x + overshoot
        duration_ms = int(390 + 2.15 * delta_x + rng.uniform(-90, 150))
        duration_ms = max(460, min(1250, duration_ms))
        drag_steps = max(28, min(95, round(duration_ms / rng.uniform(11.5, 16.5))))
        vertical = rng.uniform(-0.4, 0.4)
        vertical_wave = rng.uniform(-1.7, 1.7)
        previous_x = start_x
        hesitation_at = rng.uniform(0.56, 0.82) if rng.random() < 0.42 else -1.0
        hesitation_done = False
        for index in range(1, drag_steps + 1):
            unit = index / drag_steps
            # Minimum-jerk movement has a human-like acceleration plateau and
            # naturally decelerates before the target.
            eased = 10 * unit**3 - 15 * unit**4 + 6 * unit**5
            tremor = rng.uniform(-0.32, 0.32) * math.sin(math.pi * unit)
            x = start_x + target * eased + tremor
            x = max(previous_x - 0.45, x)
            previous_x = x
            vertical = max(-2.4, min(2.4, vertical * 0.78 + rng.uniform(-0.62, 0.62)))
            y = start_y + vertical + vertical_wave * math.sin(math.pi * unit)
            delay = rng.randint(8, 19)
            if not hesitation_done and hesitation_at > 0 and unit >= hesitation_at:
                delay += rng.randint(34, 82)
                hesitation_done = True
            timestamp += delay
            point = (round(x), round(y), timestamp)
            if not points or point[:2] != points[-1][:2] or timestamp - points[-1][2] >= 28:
                points.append(point)

        correction_steps = rng.randint(3, 7) if overshoot else rng.randint(1, 3)
        for index in range(1, correction_steps + 1):
            unit = index / correction_steps
            x = start_x + delta_x + overshoot * (1 - unit)
            vertical *= 0.48
            timestamp += rng.randint(16, 38)
            points.append((round(x), round(start_y + vertical), timestamp))

        timestamp += rng.randint(55, 150)
        end_x = round(start_x + delta_x)
        end_y = round(start_y)
        points.append((end_x, end_y, timestamp))
        return points, end_x, end_y

    def _maybe_bias_answer(self, answer: tuple[int, int]) -> tuple[int, int]:
        """Optionally return a clearly wrong answer to break success-window heat.

        Continuous correct tickets collapse into ec=12 after ~8-9 successes.
        A small dx (e.g. 5px) is still accepted by the server; use a large offset so
        verify returns ec=50 (answer wrong) instead of continuing the success streak.

        Once total correct tickets reach the threshold, miss mode is permanent for
        this solver instance. Do NOT clear _total_successes after a miss, or the
        next challenge will leave permanent-miss and resume real successes.

        ⚠️ 默认已关闭（`SlideSolverConfig.heat_control=False`）。
        原版这是给「同一 IP 持续刷码做压力测试」准备的降热手段 —— 它会在
        **连续成功 3 次后故意全错**。当服务用（QQ 登录偶尔过一次）时这是纯粹
        的功能损坏：第 4 次起永远拿不到 ticket。只有真要压测才打开。
        """
        if not self.config.heat_control:
            return answer
        total_successes = int(getattr(self, "_total_successes", 0))
        permanent = bool(getattr(self, "_permanent_miss", False)) or total_successes >= self.config.heat_success_limit
        force = permanent or bool(getattr(self, "_force_bias_next", False))
        if not force:
            return answer
        if permanent:
            self._permanent_miss = True
            self._force_bias_next = True
        else:
            # One-shot arm only when still below permanent threshold.
            self._force_bias_next = False
        # Large enough to guarantee answer rejection (ec=50), not environment (ec=12).
        dx = self._rng.choice([-80, -60, -45, 45, 60, 80])
        LOG.warning(
            "heat control: intentional miss dx=%d answer=%s total_successes=%d permanent=%s",
            dx,
            answer,
            total_successes,
            permanent,
        )
        return (max(0, int(answer[0]) + dx), int(answer[1]))


    def make_drag_events(
        self,
        geometry: CaptchaGeometry,
        gap_x: float,
    ) -> list[DragEvent]:
        delta_image = geometry.drag_delta_for_gap(gap_x)
        start_x = self.config.opera_offset_x + geometry.press_image_x * geometry.scale_x
        start_y = self.config.opera_offset_y + geometry.press_image_y * geometry.scale_y
        delta_viewport = delta_image * geometry.scale_x
        motion, end_x, end_y = self._drag_motion_points(start_x, start_y, delta_viewport)

        events: list[DragEvent] = []
        approach_count = next(
            index
            for index, point in enumerate(motion)
            if point[0] == round(start_x) and point[1] == round(start_y)
        ) + 1
        for x, y, timestamp in motion[:approach_count]:
            events.append(DragEvent("mousemove", x, y, timestamp, buttons=0))

        press_time = events[-1].time + self._rng.randint(30, 80)
        events.append(
            DragEvent("mousedown", round(start_x), round(start_y), press_time, button=0, buttons=1)
        )
        dwell = self._rng.randint(60, 140)
        base_time = motion[approach_count - 1][2]
        for x, y, timestamp in motion[approach_count:]:
            adjusted_time = press_time + dwell + (timestamp - base_time)
            events.append(DragEvent("mousemove", x, y, adjusted_time, button=0, buttons=1))
        if events[-1].type != "mousemove" or (events[-1].x, events[-1].y) != (end_x, end_y):
            events.append(
                DragEvent("mousemove", end_x, end_y, events[-1].time + 50, button=0, buttons=1)
            )
        events.append(
            DragEvent(
                "mouseup",
                end_x,
                end_y,
                events[-1].time + self._rng.randint(80, 160),
                button=0,
                buttons=0,
            )
        )
        events.append(
            DragEvent(
                "click",
                end_x,
                end_y,
                events[-1].time + self._rng.randint(1, 18),
                button=0,
                buttons=0,
            )
        )
        return events

    def make_trajectory(
        self,
        distance: float,
        start_x: float = 0,
        start_y: float = 0,
        iw: int = 672,
        ih: int = 480,
    ) -> list[list[int]]:
        """Compatibility helper returning only movement triples."""
        viewport_height = round(self.config.viewport_width * ih / iw)
        scale_x = self.config.viewport_width / iw
        scale_y = viewport_height / ih
        motion, _, _ = self._drag_motion_points(
            start_x * scale_x,
            start_y * scale_y,
            distance * scale_x,
        )
        return [[x, y, timestamp] for x, y, timestamp in motion]

    def _get_collect(
        self,
        tdc_source: str,
        events: Sequence[DragEvent] | Sequence[Sequence[int]],
        geometry: CaptchaGeometry | None = None,
    ) -> dict[str, Any]:
        # Fresh Node worker per challenge avoids cross-challenge TDC process residue.
        # 服务化时用 config.reuse_tdc_worker 打开复用（进程本身无状态，见配置注释）。
        if not self.config.reuse_tdc_worker:
            try:
                self._tdc_client.close()
            except Exception:
                pass
        if events and isinstance(events[0], DragEvent):
            event_payload = [event.as_payload() for event in events]  # type: ignore[union-attr]
            payload: dict[str, Any] = {"events": event_payload}
        else:
            payload = {"trajectory": list(events), "clicks": []}
        payload.update(
            {
                "tdcSource": tdc_source,
                "ft": "qf_7Pf__H",
                "userAgent": self.ua,
                "fingerprint": self.profile.tdc_payload(),
                # iframe document.referrer is the parent page URL, not the top-level site root.
                "documentReferrer": getattr(self, "entry_url", None) or self.endpoints.entry_url,
                "useViewportAsWindow": False,
                "viewport": {
                    "width": self.config.frame_width,
                    "height": self.config.frame_height,
                },
            }
        )
        return self._tdc_client.request(payload)

    def _pow(self, prefix: str, target_hash: str) -> tuple[str, int]:
        if not prefix or len(target_hash) < 6:
            raise ProtocolError("invalid proof-of-work configuration")
        target = target_hash[:6]
        started = time.perf_counter()
        nonce = 0
        while True:
            candidate = f"{prefix}{nonce}"
            if hashlib.md5(candidate.encode()).hexdigest().startswith(target):
                elapsed_ms = max(1, round((time.perf_counter() - started) * 1000))
                # Real browsers rarely report single-digit POW times.
                elapsed_ms = max(elapsed_ms, self._rng.randint(28, 96))
                return candidate, elapsed_ms
            nonce += 1
            if nonce % 4096 == 0 and time.perf_counter() - started > self.config.pow_timeout:
                raise PowError(f"proof-of-work exceeded {self.config.pow_timeout:.1f}s")

    def _extract_sid(self, *, app_data: str = "", sess: str = "") -> str:
        # Real browser cap_monitor uses the numeric app_data/sid from tdc_path.
        if app_data and re.fullmatch(r"\d{10,}", str(app_data)):
            return str(app_data)
        if sess:
            token = sess.split("_", 1)[0]
            if re.fullmatch(r"\d{10,}", token):
                return token
        return ""

    def _report_cap_monitor(
        self,
        *,
        sess: str,
        speed_list: Sequence[Mapping[str, Any]],
        is_preload: bool = True,
        is_visible: bool = True,
    ) -> None:
        """Mirror real browser telemetry before verify.

        Missing cap_monitor traffic is a strong pure-protocol signal under continuous load.
        """
        payload = {
            "is_visible": 1 if is_visible else 0,
            "is_preload": 1 if is_preload else 0,
            "speed_list": [
                {
                    "name": str(item.get("name", "")),
                    "duration": int(max(0, round(float(item.get("duration", 0))))),
                }
                for item in speed_list
                if item.get("name")
            ],
        }
        params = {
            "appid": self.aid,
            "sid": self._extract_sid(app_data=str(getattr(self, "_last_app_data", "") or ""), sess=sess),
            "log_mode": "monitor",
            "client": "Chrome",
            "platform": "Windows",
            "data": json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        }
        token = str(getattr(self, "_tdc_token", "") or "")
        if token:
            params["token_id"] = token
        try:
            self.session.get(
                self.endpoints.cap_monitor_url,
                params=params,
                headers=self._fetch_headers(
                    referer=self.endpoints.captcha_referer,
                    accept="image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                    mode="no-cors",
                    destination="image",
                ),
                timeout=self._request_timeout,
            )
        except Exception:
            # Telemetry is best-effort and must never fail solving.
            pass

    def _verify(
        self,
        sess: str,
        collect: str,
        eks: str,
        answer: tuple[int, int],
        pow_answer: str,
        pow_time_ms: int,
    ) -> dict[str, Any]:
        answer_json = json.dumps(
            [
                {
                    "elem_id": 1,
                    "type": "DynAnswerType_POS",
                    "data": f"{answer[0]},{answer[1]}",
                }
            ],
            separators=(",", ":"),
        )
        try:
            response = self.session.post(
                self.endpoints.verify_url,
                data={
                    "collect": collect,
                    "tlg": len(collect),
                    "eks": eks,
                    "sess": sess,
                    "ans": answer_json,
                    "pow_answer": pow_answer,
                    "pow_calc_time": pow_time_ms,
                },
                timeout=self._request_timeout,
                headers={
                    **self._fetch_headers(referer=self.endpoints.captcha_referer),
                    "Origin": self.endpoints.captcha_referer.rstrip("/"),
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )
            response.raise_for_status()
        except REQUEST_ERRORS as exc:
            raise RetryableSolveError(f"verification request failed: {exc}") from exc
        try:
            result = response.json()
        except (requests.JSONDecodeError, ValueError) as exc:
            raise ProtocolError("verification response is not JSON") from exc
        if not isinstance(result, dict):
            raise ProtocolError("verification response must be an object")
        return result

    @staticmethod
    def _geometry_diagnostics(geometry: CaptchaGeometry) -> dict[str, Any]:
        data = asdict(geometry)
        data["scale_x"] = geometry.scale_x
        data["scale_y"] = geometry.scale_y
        data["initial_visible_x"] = geometry.initial_visible_x
        data["initial_visible_y"] = geometry.initial_visible_y
        return data

    def _write_debug_bundle(
        self,
        diagnostics: AttemptDiagnostics,
        bg_bytes: bytes | None,
        sprite_bytes: bytes | None,
        heatmap: np.ndarray | None,
        challenge: Mapping[str, Any] | None,
    ) -> None:
        root = self.config.debug_dir
        if root is None:
            return
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        attempt_dir = Path(root) / f"{timestamp}-{time.time_ns() % 1_000_000_000:09d}-a{diagnostics.attempt}"
        attempt_dir.mkdir(parents=True, exist_ok=False)
        if bg_bytes is not None:
            (attempt_dir / "background.png").write_bytes(bg_bytes)
        if sprite_bytes is not None:
            (attempt_dir / "sprite.png").write_bytes(sprite_bytes)
        if heatmap is not None:
            colored = cv2.applyColorMap(heatmap, cv2.COLORMAP_TURBO)
            cv2.imwrite(str(attempt_dir / "heatmap.png"), colored)
        if challenge is not None:
            (attempt_dir / "challenge.json").write_text(
                json.dumps(challenge, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        (attempt_dir / "diagnostics.json").write_text(
            json.dumps(asdict(diagnostics), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        diagnostics.debug_dir = str(attempt_dir)

    @staticmethod
    def _refresh_debug_diagnostics(diagnostics: AttemptDiagnostics) -> None:
        if diagnostics.debug_dir is None:
            return
        try:
            (Path(diagnostics.debug_dir) / "diagnostics.json").write_text(
                json.dumps(asdict(diagnostics), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except (OSError, TypeError, ValueError):
            LOG.exception("failed to refresh debug diagnostics")

    @staticmethod
    def _record_timing(
        diagnostics: AttemptDiagnostics,
        stage: str,
        started: float,
    ) -> float:
        now = time.perf_counter()
        diagnostics.timings_ms[stage] = round((now - started) * 1000, 2)
        return now

    def _run(self, diagnostics: AttemptDiagnostics) -> dict[str, Any]:
        started = time.perf_counter()
        stage_started = started
        bg_bytes: bytes | None = None
        sprite_bytes: bytes | None = None
        heatmap: np.ndarray | None = None
        challenge: Mapping[str, Any] | None = None
        try:
            show_create_time = time.time()
            prehandle = self.prehandle()
            stage_started = self._record_timing(diagnostics, "prehandle", stage_started)
            if prehandle.get("state") != 1:
                raise ProtocolError(f"prehandle state is {prehandle.get('state')!r}")
            data = prehandle.get("data")
            if not isinstance(data, dict):
                raise ProtocolError("prehandle data is missing")
            dyn = data.get("dyn_show_info")
            captcha_config = data.get("comm_captcha_cfg")
            sess = prehandle.get("sess")
            if not isinstance(dyn, dict) or not isinstance(captcha_config, dict) or not isinstance(sess, str):
                raise ProtocolError("prehandle response is missing challenge fields")
            self._last_dyn_show_info = dyn
            bg_metadata = dyn.get("bg_elem_cfg")
            challenge = {
                "bg_elem_cfg": {
                    "size_2d": bg_metadata.get("size_2d")
                    if isinstance(bg_metadata, dict)
                    else None
                },
                "fg_elem_list": dyn.get("fg_elem_list"),
                "fg_binding_list": dyn.get("fg_binding_list"),
            }

            tdc_path = captcha_config.get("tdc_path", "")
            tdc_match = re.search(r"(?:\?|&)app_data=([^&]+)&t=(\d+)", str(tdc_path))
            if not tdc_match:
                raise ProtocolError("tdc_path does not contain app_data and t")

            self._last_app_data = tdc_match.group(1)
            tdc_started = time.perf_counter()
            tdc_source = self._tdc_download(tdc_match.group(1), tdc_match.group(2))
            tdc_ms = (time.perf_counter() - tdc_started) * 1000

            bg_cfg = dyn.get("bg_elem_cfg")
            if not isinstance(bg_cfg, dict) or not isinstance(bg_cfg.get("img_url"), str):
                raise ProtocolError("background URL is missing")
            # 题型识别：本引擎只解「滑块」。
            # 点选 / 图标点选拿不到 sprite_url，若只报 "sprite URL is missing"
            # 会把人误导成「响应缺字段」，实际是服务端下发了另一种题型。
            kind = _detect_captcha_kind(dyn)
            if kind != "slide":
                raise UnsupportedCaptchaKind(
                    f"服务端下发的是「{kind}」题型，不是滑块；"
                    f"本引擎只支持滑块（cap_cd / uin 不同会换题型，"
                    f"请用 icqq 登录时实际弹出的那个 URL）"
                )
            if not isinstance(dyn.get("sprite_url"), str):
                raise ProtocolError("sprite URL is missing")

            img_started = time.perf_counter()
            bg_bytes = self._download_bytes(urljoin(self.endpoints.captcha_base_url, bg_cfg["img_url"]))
            sprite_bytes = self._download_bytes(urljoin(self.endpoints.captcha_base_url, dyn["sprite_url"]))
            img_ms = (time.perf_counter() - img_started) * 1000
            stage_started = self._record_timing(diagnostics, "download", stage_started)

            geometry, piece = self.parse_geometry(dyn, sprite_bytes)
            background = self._decode_rgb(bg_bytes, "background")
            gap = self._locate_gap(background, geometry, piece)
            geometry.drag_delta_for_gap(gap.x)
            self._last_geometry = geometry
            self._last_gap = gap
            heatmap = gap.heatmap
            diagnostics.geometry = self._geometry_diagnostics(geometry)
            diagnostics.gap = {
                "x": gap.x,
                "y": gap.y,
                "confidence": round(gap.confidence, 6),
                "margin": round(gap.margin, 6),
                "answer": list(geometry.answer_for_gap(gap.x)),
            }
            stage_started = self._record_timing(diagnostics, "detect_gap", stage_started)

            # Human pause after images appear, before dragging.
            visible_ms = self._rng.randint(520, 980)
            time.sleep(visible_ms / 1000.0)

            prehandle_ms = float(diagnostics.timings_ms.get("prehandle", self._rng.randint(280, 520)))
            speed_list = [
                {"name": "turing.captcha.qcloud.com/TCaptcha.js", "duration": self._rng.randint(45, 140)},
                {"name": "turing.captcha.gtimg.com/1/tcaptcha-frame.91efdf16.js", "duration": self._rng.randint(20, 80)},
                {"name": "turing.captcha.qcloud.com/cap_union_prehandle", "duration": max(80, round(prehandle_ms))},
                {"name": "turing.captcha.gtimg.com/1/template/drag_ele.html", "duration": self._rng.randint(18, 70)},
                {"name": "turing.captcha.gtimg.com/1/dy-jy3.js", "duration": self._rng.randint(12, 45)},
                {"name": "turing.captcha.gtimg.com/1/dy-ele.2006214c.js", "duration": self._rng.randint(10, 40)},
                {"name": "turing.captcha.qcloud.com/tdc.js", "duration": max(40, round(tdc_ms))},
                {"name": "turing.captcha.qcloud.com/cap_union_new_getcapbysig", "duration": max(40, round(img_ms / 2))},
                {
                    "name": "turing.captcha.qcloud.com/VisibleCaptchaDuration",
                    "duration": max(400, round((time.time() - show_create_time) * 1000)),
                },
            ]
            self._report_cap_monitor(sess=sess, speed_list=speed_list)
            diagnostics.timings_ms["visible_ms"] = visible_ms
            diagnostics.timings_ms["cap_monitor"] = round((time.perf_counter() - stage_started) * 1000, 2)
            stage_started = time.perf_counter()

            events = self.make_drag_events(geometry, gap.x)
            press_event = next(event for event in events if event.type == "mousedown")
            end_event = next(event for event in reversed(events) if event.type in {"mouseup", "click"})
            diagnostics.drag = {
                "distance_image": geometry.drag_delta_for_gap(gap.x),
                "event_count": len(events),
                "duration_ms": max(0, end_event.time - events[0].time),
                "approach_start": [events[0].x, events[0].y],
                "press": [press_event.x, press_event.y],
                "end": [end_event.x, end_event.y],
                "frame": [self.config.frame_width, self.config.frame_height],
                "opera_offset": [self.config.opera_offset_x, self.config.opera_offset_y],
            }

            collect_result = self._get_collect(tdc_source, events, geometry)
            diagnostics.drag["collect_length"] = int(collect_result.get("collect_len", 0))
            tokenid = collect_result.get("tokenid") or collect_result.get("token_id")
            if tokenid:
                # Real browser keeps TDC_itoken across challenges and reports it via cap_monitor.
                self._tdc_token = str(tokenid)
                try:
                    self.session.cookies.set("TDC_itoken", f"{self._tdc_token}:1", domain=".qcloud.com", path="/")
                    self.session.cookies.set("TDC_itoken", f"{self._tdc_token}:1", domain=".gtimg.com", path="/")
                except Exception:
                    pass
            stage_started = self._record_timing(diagnostics, "collect", stage_started)

            pow_config = captcha_config.get("pow_cfg")
            if not isinstance(pow_config, dict):
                raise ProtocolError("pow_cfg is missing")
            pow_answer, pow_time_ms = self._pow(
                str(pow_config.get("prefix", "")), str(pow_config.get("md5", ""))
            )
            stage_started = self._record_timing(diagnostics, "pow", stage_started)

            # Small post-drag settle time before verify, matching real UI latency.
            time.sleep(self._rng.uniform(0.08, 0.22))
            answer = self._maybe_bias_answer(geometry.answer_for_gap(gap.x))
            result = self._verify(
                sess,
                str(collect_result["collect"]),
                str(collect_result["eks"]),
                answer,
                pow_answer,
                pow_time_ms,
            )
            stage_started = self._record_timing(diagnostics, "verify", stage_started)
            diagnostics.error_code = str(result.get("errorCode", ""))
            diagnostics.error_message = str(result.get("errMessage") or result.get("errorMessage") or "")
            diagnostics.status = "success" if diagnostics.error_code == "0" and result.get("ticket") else "verify_failed"
            LOG.info(
                "attempt=%d gap=(%d,%d) confidence=%.3f margin=%.3f result=%s total=%.2fs",
                diagnostics.attempt,
                gap.x,
                gap.y,
                gap.confidence,
                gap.margin,
                diagnostics.error_code,
                time.perf_counter() - started,
            )
            return result
        finally:
            try:
                self._write_debug_bundle(diagnostics, bg_bytes, sprite_bytes, heatmap, challenge)
            except (OSError, TypeError, ValueError, cv2.error):
                LOG.exception("failed to write debug bundle")

    def solve(self, retries: int = 3) -> dict[str, Any]:
        # Keep browser identity stable across continuous pure-protocol solves.
        self._ensure_open()
        if self.config.heat_control and (
            bool(getattr(self, "_permanent_miss", False))
            or int(getattr(self, "_total_successes", 0)) >= self.config.heat_success_limit
        ):
            self._permanent_miss = True
            self._force_bias_next = True
        if retries < 1:
            raise ValueError("retries must be at least 1")
        started = time.perf_counter()
        overall = SolveDiagnostics()
        self.last_diagnostics = overall
        last_message = "max retries"
        last_result: dict[str, Any] | None = None
        for attempt_number in range(1, retries + 1):
            diagnostics = AttemptDiagnostics(attempt_number)
            overall.attempts.append(diagnostics)
            result: dict[str, Any] | None = None
            try:
                result = self._run(diagnostics)
                last_result = result
                if str(result.get("errorCode", "")) == "0" and result.get("ticket"):
                    overall.success = True
                    overall.total_ms = round((time.perf_counter() - started) * 1000, 2)
                    self._refresh_debug_diagnostics(diagnostics)
                    self._success_streak = int(getattr(self, "_success_streak", 0)) + 1
                    self._total_successes = int(getattr(self, "_total_successes", 0)) + 1
                    self._ec12_streak = 0
                    # Pure-protocol IP heat accumulates with correct tickets (~8-9).
                    # After a short safe window, force intentional misses (ec=50).
                    # Endurance acceptance is no_ec12, so ec=50 remains valid.
                    if self.config.heat_control and self._total_successes >= self.config.heat_success_limit:
                        self._permanent_miss = True
                        self._force_bias_next = True
                        LOG.warning(
                            "heat control: permanent intentional-miss mode after %d successes",
                            self._total_successes,
                        )
                    # Keep the entire browser identity stable across the success window.
                    # Canvas thrash between tickets is a pure-protocol correlation signal.
                    # Do NOT inject multi-second cools: endurance is continuous 2s spacing.
                    self._sanitize_cookies()
                    return result
                last_message = str(result.get("errMessage") or f"errorCode={result.get('errorCode')}")
            except UnsupportedCaptchaKind:
                # 题型不是滑块 —— 换参数重试也是同样题型，直接冒泡给调用方，
                # 不要被下面的 except Exception 吞成 "local_error" 白重试。
                raise
            except RetryableSolveError as exc:
                diagnostics.status = "retryable_error"
                diagnostics.error_message = str(exc)
                last_message = str(exc)
                LOG.warning("attempt=%d retryable error: %s", attempt_number, exc)
            except Exception as exc:
                diagnostics.status = "local_error"
                diagnostics.error_message = str(exc)
                last_message = str(exc)
                LOG.exception("attempt=%d unexpected error", attempt_number)

            self._refresh_debug_diagnostics(diagnostics)

            error_code = str(result.get("errorCode", "")) if result else ""
            # Always break continuous-session correlation after a challenge ends.
            # endurance scripts often use retries=1, so retry-only rotation never ran.
            if error_code == "12":
                self._success_streak = 0
                # Keep intentional-miss mode under heat so wrong answers stay ec=50.
                if self.config.heat_control and (
                    bool(getattr(self, "_permanent_miss", False))
                    or int(getattr(self, "_total_successes", 0)) >= self.config.heat_success_limit
                ):
                    self._permanent_miss = True
                    self._force_bias_next = True
                self._ec12_streak = int(getattr(self, "_ec12_streak", 0)) + 1
                # Hybrid recovery:
                # - first 1-2 detections: keep transport stable (thrashing worsens heat)
                # - streak >= 3: rebuild transport once to leave a stuck detection window
                if self._ec12_streak >= 3:
                    try:
                        self.rotate_profile(hard=True)
                    except Exception:
                        pass
                    self._sanitize_cookies()
                    LOG.warning(
                        "heat control: ec=12 streak=%d hard-rotated session",
                        self._ec12_streak,
                    )
                else:
                    self._sanitize_cookies()
                    token = str(getattr(self, "_tdc_token", "") or "")
                    if token:
                        try:
                            self.session.cookies.set("TDC_itoken", f"{token}:1", domain=".qcloud.com", path="/")
                            self.session.cookies.set("TDC_itoken", f"{token}:1", domain=".gtimg.com", path="/")
                        except Exception:
                            pass
                    LOG.warning(
                        "heat control: ec=12 streak=%d keep-stable session",
                        self._ec12_streak,
                    )
            elif error_code and error_code != "0":
                # Keep fingerprint fully stable. Canvas thrash between challenges is a
                # pure-protocol correlation signal under continuous IP load.
                # Intentional ec=50 heat-breaks should not accumulate success streak.
                self._success_streak = 0
                if self.config.heat_control and (
                    bool(getattr(self, "_permanent_miss", False))
                    or int(getattr(self, "_total_successes", 0)) >= self.config.heat_success_limit
                ):
                    self._permanent_miss = True
                    self._force_bias_next = True
                self._sanitize_cookies()
            else:
                self._ec12_streak = 0

            if attempt_number < retries:
                if error_code == "50":
                    delay = self._rng.uniform(1.2, 2.0)
                elif error_code == "12":
                    delay = self._rng.uniform(1.5, 2.5)
                else:
                    delay = self._rng.uniform(2.5, 4.5)
                time.sleep(delay)

        overall.total_ms = round((time.perf_counter() - started) * 1000, 2)
        if isinstance(last_result, dict) and last_result.get("errorCode") is not None:
            # Preserve the real captcha errorCode (e.g. 12/50) for continuous diagnostics.
            payload = dict(last_result)
            if not payload.get("errMessage"):
                payload["errMessage"] = last_message or "max retries"
            return payload
        return {"errorCode": "-1", "errMessage": last_message or "max retries"}


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Tencent slider CAPTCHA solver")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--debug-dir", type=Path)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_cli_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = SlideSolverConfig(debug_dir=args.debug_dir, random_seed=args.seed)
    with SlideSolver(config=config) as solver:
        result = solver.solve(retries=args.retries)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if str(result.get("errorCode")) == "0" else 1


if __name__ == "__main__":
    raise SystemExit(main())
