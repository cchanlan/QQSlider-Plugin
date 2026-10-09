"""
腾讯 TCaptcha 滑块 —— 全自动过码服务

输入一个滑块验证 URL（QQ 登录时 icqq 弹出来的那个），输出 {ticket, randstr}，
供 icqq 的 `bot.submitSlider(ticket)` 调用。

## 与米游社那套（xhh-TL/service/geetest）的关系

思路一致（纯 HTTP 协议、零浏览器），但**协议完全不同，代码不可复用**：

  | | 米游社体力过码 | 本服务 |
  |---|---|---|
  | 验证码系统 | 极验 Geetest v3 | 腾讯 TCaptcha（防水墙） |
  | 关键参数 | `w` = AES+RSA+私有 base64 | `collect` / `eks` / `ans` / `pow_answer` |
  | 缺口算法 | 模板匹配（Rust 库） | OpenCV 多尺度模板匹配 |
  | 加密参数 | 自己移植 w 生成 | 用 Node 跑腾讯官方 `tdc.js` |

## 链路

  ① prehandle        拿 sess + 三张图 URL + pow_cfg + tdc_path
  ② tdc.js           下载腾讯官方脚本，交给 Node worker 执行
  ③ 下载图片          还原乱序背景 → 模板匹配出缺口坐标
  ④ 造轨迹            拟人拖拽事件序列（贝塞尔 + 抖动 + 过冲回拉）
  ⑤ Node 出参数       把轨迹喂给 tdc.js，得到 collect + eks
  ⑥ pow              解工作量证明
  ⑦ verify           提交，拿 ticket

⚠️ 每个 challenge 只能用一次，失败必须整轮重来。

## 用法

    python server.py                 # 默认监听 127.0.0.1:8767
    python server.py --port 9000

    POST /solve
      { "url": "https://ssl.captcha.qq.com/...", "uin": "123456" }   # 推荐
      { "uin": "123456", "aid": "16" }                               # 无 URL 时
      { "auto": true }                                               # 用内置默认目标

    GET /health      # 健康统计
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from slide_solver import (  # noqa: E402
    ENDPOINTS,
    ENDPOINTS_QQ,
    TDC_SERVER,
    SlideSolver,
    SlideSolverConfig,
    SlideSolverError,
    UnsupportedCaptchaKind,
)

LOG = logging.getLogger("qq_slider_server")

DEFAULT_PORT = int(os.environ.get("QQ_SLIDER_PORT", 8767))
DEFAULT_ROUNDS = int(os.environ.get("QQ_SLIDER_ROUNDS", 3))
HOST = os.environ.get("QQ_SLIDER_HOST", "127.0.0.1")

# 可选的第三方回落（默认关闭）。
#
# 用途：QQ 登录的题型由服务端按 uin + cap_cd 下发，静态探测只会得到「点选」，
# 而本引擎只解「滑块」。真到了点选题型时，可以设这个环境变量回落到现成服务：
#
#   set QQ_SLIDER_FALLBACK=https://GT.928100.xyz/captcha/slider
#
# ⚠️ 打开意味着滑块会话经过别人的服务器，自己权衡。
FALLBACK_URL = os.environ.get("QQ_SLIDER_FALLBACK", "").strip()

_stats_lock = threading.Lock()
_stats = {"ok": 0, "fail": 0, "rounds": 0, "total_s": 0.0}


def _set_default_rounds(value: int) -> None:
    """改模块级默认轮数。

    不能写成 `global DEFAULT_ROUNDS` —— 该名字已被 `solve_once` 的默认参数
    求值引用过，Python 会报 "used prior to global declaration"。
    用 globals() 直接改，效果一样且不触发该限制。
    """
    globals()["DEFAULT_ROUNDS"] = int(value)


# ── 常驻 solver ────────────────────────────────────────────────────────────
#
# 每次请求都新建 solver 的话，Node worker 进程要重新启动、会话要重新预热，
# 单次约 5~7s；复用同一实例则稳定在 ~2.4s。
#
# 按 (端点, aid) 各缓存一个：只缓存「最后一个」的话，QQ 与腾讯云交替请求会
# 互相把对方的实例踢掉、每次都要重建（实测 QQ 请求 4.5s vs 命中缓存 2.1s）。
# 两个端点各一个 Node worker 常驻，内存代价可接受。
#
# 线程安全：solver 内部有状态（会话、指纹、连击计数），必须串行使用。
# 这里用一把大锁把所有 solver 串起来 —— 过码是低频操作（登录才触发），
# 串行完全够用，也不会让多个 challenge 互相污染指纹。
_solver_lock = threading.Lock()
_solvers: dict[tuple, SlideSolver] = {}


def _solver_key_for(endpoints, aid_int: int | None) -> tuple:
    return (getattr(endpoints, "name", repr(endpoints)), aid_int)


def _make_solver(endpoints, aid_int: int | None) -> SlideSolver:
    """建一个 solver。调用方须持有 _solver_lock。"""
    # Node 可执行文件：插件侧会把探测到的路径通过环境变量传进来
    # （nvm / 非默认 PATH 的环境里裸 `node` 可能找不到）。
    node_command = (os.environ.get("QQ_SLIDER_NODE") or "").strip() or "node"
    config = SlideSolverConfig(
        heat_control=False,      # 绝不故意失败
        reuse_tdc_worker=True,   # 复用 Node 进程，热态关键
        node_command=node_command,
    )
    return SlideSolver(config=config, endpoints=endpoints, aid=aid_int)


def _get_solver(endpoints, aid_int: int | None) -> SlideSolver:
    """取常驻 solver，没有就建。调用方须持有 _solver_lock。"""
    key = _solver_key_for(endpoints, aid_int)
    solver = _solvers.get(key)
    if solver is None:
        solver = _make_solver(endpoints, aid_int)
        _solvers[key] = solver
        LOG.info("新建常驻 solver（endpoints=%s aid=%s）", key[0], aid_int)
    return solver


def _drop_solver(endpoints, aid_int: int | None) -> None:
    """丢掉某个 key 的 solver（会话疑似被污染时）。调用方须持有 _solver_lock。"""
    key = _solver_key_for(endpoints, aid_int)
    solver = _solvers.pop(key, None)
    if solver is not None:
        try:
            solver.close()
        except Exception:
            pass
        LOG.info("丢弃 solver（endpoints=%s aid=%s）", key[0], aid_int)


def _reset_solver() -> None:
    """丢掉全部常驻 solver（进程退出、或需要整体重来）。调用方须持有 _solver_lock。"""
    for key, solver in list(_solvers.items()):
        try:
            solver.close()
        except Exception:
            pass
        LOG.info("丢弃 solver（endpoints=%s aid=%s）", key[0], key[1])
    _solvers.clear()


def _bump(ok: bool, seconds: float, rounds: int) -> None:
    with _stats_lock:
        _stats["ok" if ok else "fail"] += 1
        _stats["rounds"] += rounds
        _stats["total_s"] = round(_stats["total_s"] + seconds, 3)


def _delayed_exit(delay: float = 0.3) -> None:
    """稍等片刻再退出 —— 让 /shutdown 的 HTTP 响应先发完。

    用 `os._exit` 而不是 `sys.exit`：这是在**后台线程**里跑的，
    `sys.exit` 只结束该线程，主线程的 `serve_forever()` 还活着。
    这里就是要整个进程立刻走，所以直接用 `os._exit`。

    退出前顺手把常驻 solver 关掉，免得留下 Node worker 孤儿进程。
    """
    import time as _time

    _time.sleep(delay)
    try:
        with _solver_lock:
            _reset_solver()
    except Exception:  # noqa: BLE001
        pass
    os._exit(0)


# 源码指纹缓存（见 _code_hash）。进程存活期间代码不会变，算一次即可。
_CODE_HASH_CACHE: str | None = None


def _code_hash() -> str:
    """service 目录下源码文件的**内容指纹**（16 位十六进制）。

    ## 为什么需要它

    `git pull` 只更新磁盘上的 `.py`，而**已经在跑的 Python 进程还在内存里
    执行旧代码** —— 插件侧原来只做「服务活着吗」的健康检查，
    于是会一直复用那个旧进程，表现为「明明更新了插件，行为还是老的」。

    这个指纹就是给插件侧做**版本比对**用的：算一遍磁盘上的指纹，
    跟 `/health` 报上来的（= 正在跑的那份代码的指纹）一比，
    不一致就说明该重启服务了。

    ## 细节

    - 只算源码（`.py` / `.cjs`），**不算** `.venv` / `.models` / `__pycache__`
      —— 那些不是代码，变了也不代表要重启。
    - 文件名一起进哈希，避免「改了文件名但内容没变」时指纹不变。
    - 结果缓存：进程活着期间这些文件不会变（真变了就该重启，不是热加载），
      所以算一次就够，`/health` 每次都能秒回。
    """
    global _CODE_HASH_CACHE
    if _CODE_HASH_CACHE is not None:
        return _CODE_HASH_CACHE

    import hashlib as _hashlib

    digest = _hashlib.sha256()
    here = Path(__file__).resolve().parent
    try:
        files = sorted(
            (p for p in here.iterdir()
             if p.is_file() and p.suffix in (".py", ".cjs") and not p.name.startswith(".")),
            key=lambda p: p.name,
        )
    except OSError:
        files = []

    for path in files:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            # 读不到也要参与哈希，否则「文件消失」会被算成「没变化」
            digest.update(b"<unreadable>")
        digest.update(b"\0")

    _CODE_HASH_CACHE = digest.hexdigest()[:16]
    return _CODE_HASH_CACHE


def _env_probe() -> dict:
    """报一下运行环境，供插件侧 #滑块过码状态 显示。

    只做**廉价**的检查（不联网、不起进程），所以可以每次 /health 都算。
    """
    import shutil as _shutil

    node_cmd = (os.environ.get("QQ_SLIDER_NODE") or "").strip() or "node"
    deps = {}
    for name in ("cv2", "numpy", "curl_cffi", "requests"):
        try:
            __import__(name)
            deps[name] = True
        except Exception:
            deps[name] = False
    info = {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "node": _shutil.which(node_cmd) or None,
        "nodeCommand": node_cmd,
        "deps": deps,
        "tdcServer": TDC_SERVER.name,
        # 正在运行的这份代码的指纹 —— 插件侧拿它跟磁盘上的比对，
        # 不一致就说明「更新了但没重启」，见 _code_hash 的说明。
        "codeHash": _code_hash(),
        "pid": os.getpid(),
    }
    # 点选识别能力（只看模型文件在不在，不载模型 —— 载入要 0.9s，太重）
    try:
        import click_recognizer as _cr

        info["click"] = _cr.recognizer_status()
    except Exception as err:  # noqa: BLE001
        info["click"] = {"available": False, "reason": f"{type(err).__name__}: {err}"}
    return info


def _solve_click_variant(solver, kind: str) -> dict:
    """点选题型的解题入口。

    走本地 CLIP 识别（见 `click_recognizer` + `click_solver`）。
    识别不了时返回和滑块一致的失败结构，让上层统一处理。
    """
    if kind != "click":
        return {"errorCode": "-1",
                "errMessage": f"服务端下发的是「{kind}」题型，目前只支持滑块与点选"}

    try:
        import click_recognizer
        import click_solver
    except ImportError as err:
        return {"errorCode": "-1", "errMessage": f"点选模块不可用：{err}"}

    top_k = int(os.environ.get("QQ_SLIDER_CLICK_TOPK", "3"))
    # ec=9（verifyFailRefresh）时换题重试几次。默认 3 次；
    # 每次都要重新 prehandle + 重新识别，所以别开太大。
    refresh_attempts = int(os.environ.get("QQ_SLIDER_CLICK_REFRESH", "3"))
    # ec=51/30（verifyHybrid）时带 sess 续做几次。默认 5 次。
    # ★ 点选第一题**必返 51**，续做那一轮才是多选题、才可能拿到 ticket，
    #   所以这个预算不能小（小于 2 就等于永远过不去）。
    hybrid_attempts = int(os.environ.get("QQ_SLIDER_CLICK_HYBRID", "5"))
    # 「断崖太小、这张认不出」时在同一流程内重摇几次。默认 8。
    # ★ 单独一份额度：这类题（典型「包含文字：X」）实测约占多选的 1/3，
    #   并进 refresh 额度会挤掉能认出的题的机会。
    max_unrecognized = int(os.environ.get("QQ_SLIDER_CLICK_REROLL", "8"))
    try:
        rec = click_recognizer.get_recognizer(top_k=top_k)
    except Exception as err:  # noqa: BLE001
        return {"errorCode": "-1", "errMessage": f"点选识别器初始化失败：{err}"}

    try:
        return click_solver.solve_click(solver, recognizer=rec, top_k=top_k,
                                        refresh_attempts=refresh_attempts,
                                        hybrid_attempts=hybrid_attempts,
                                        max_unrecognized=max_unrecognized)
    except click_solver.ClickNotSupported as err:
        # 模型没装好 → 真正回落到纯 CV（准确率约 48%，但总比直接失败好）
        return _solve_click_cv_fallback(solver, rec, err)
    except Exception as err:  # noqa: BLE001
        LOG.exception("点选解题异常")
        return {"errorCode": "-1", "errMessage": f"{type(err).__name__}: {err}"}


def _solve_click_cv_fallback(solver, recognizer, cause) -> dict:
    """纯 CV 兜底：模型没装好时至少把「最像异类」的那格点掉。

    ⚠️ 这条路的准确率只有约 48%（实测 25 张人工核对样本 12/25），
    所以**必须**在返回里标出 `solver=cv_only`、并让插件侧提示用户装模型 ——
    不能让人以为这是正常水准。
    """
    import json as _json

    LOG.warning("CLIP 不可用，回落纯 CV（准确率低）：%s", cause)
    try:
        import re as _re
        from urllib.parse import urljoin

        pre = solver.prehandle()
        data = pre.get("data") or {}
        dyn = data.get("dyn_show_info") or {}
        ccfg = data.get("comm_captcha_cfg") or {}
        sess = pre.get("sess")
        instruction = str(dyn.get("instruction") or "").strip()
        payload = _json.loads(dyn.get("json_payload") or "{}")
        regions = [r.get("range") for r in (payload.get("select_region_list") or [])
                   if isinstance(r, dict)]
        regions = [r for r in regions if isinstance(r, (list, tuple)) and len(r) >= 4]
        bg = dyn.get("bg_elem_cfg") or {}
        if not (sess and regions and bg.get("img_url")):
            return {"errorCode": "-1", "errMessage": f"{cause}（且纯 CV 兜底拿不到题目）"}

        raw = solver._download_bytes(urljoin(solver.endpoints.captcha_base_url, bg["img_url"]))
        rec = recognizer.recognize_cv_only(raw, regions)
        centers = [((int(r[0]) + int(r[2])) // 2, (int(r[1]) + int(r[3])) // 2) for r in regions]
        pick_idx = int(rec["pick_index"])
        pick_pt = centers[pick_idx]
        # 格子编号：格式④要提交它，不能瞎填 1
        raw_regs = [r for r in (payload.get("select_region_list") or []) if isinstance(r, dict)]
        tile_ids = [
            int(r["id"]) if str(r.get("id", "")).isdigit() else i + 1
            for i, r in enumerate(raw_regs)
        ]
        pick_id = tile_ids[pick_idx] if pick_idx < len(tile_ids) else pick_idx + 1

        m = _re.search(r"(?:\?|&)app_data=([^&]+)&t=(\d+)", str(ccfg.get("tdc_path", "")))
        if not m:
            return {"errorCode": "-1", "errMessage": f"{cause}（且 tdc_path 无 app_data）"}
        import click_solver

        tdc_source = solver._tdc_download(m.group(1), m.group(2))
        gen = solver._get_collect(tdc_source, click_solver._click_events([pick_pt]), None)
        collect = str(gen.get("collect", ""))
        eks = str(gen.get("eks", ""))
        pw = ccfg.get("pow_cfg") or {}
        pa, pt = solver._pow(str(pw.get("prefix", "")), str(pw.get("md5", "")))

        # 用**按名字取**而不是 `ANS_FORMATS[0]` —— 以后调整候选顺序时
        # 这里不会跟着悄悄换掉格式（源码确认的是 uc_region_ids）。
        build = dict(click_solver.ANS_FORMATS)["uc_region_ids"]
        ans = build([pick_pt], [pick_id])
        result = click_solver._submit(solver, sess, collect, eks, ans, pa, pt)
        result = dict(result)
        result["solver"] = "cv_only"
        result["instruction"] = instruction
        result["pick"] = pick_idx + 1
        result["candidates"] = rec.get("candidates")
        return result
    except Exception as err:  # noqa: BLE001
        LOG.exception("纯 CV 兜底也失败")
        return {"errorCode": "-1",
                "errMessage": f"点选失败（CLIP 不可用：{cause}；纯 CV 兜底也失败：{err}）",
                "solver": "cv_fallback"}


def parse_slider_url(url: str) -> dict:
    """从 icqq 弹出来的滑块 URL 里抠出参数。

    QQ 滑块 URL 形如（go-cqhttp 源码里的真实形态）：
      https://ssl.captcha.qq.com/template/wireless_mqq_captcha.html?
        uin=123456&aid=16&cap_cd=xxxx&sid=yyyy&...

    除了挑出我们认识的字段，还会把**整个 query 原样留着**（`extra`）——
    无线页面本身就是把 location.search 原样喂给 TCapIframeApi 的，
    服务端认哪些参数由它决定，我们别自作主张丢。
    """
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(parsed.query).items() if v}
    out = {
        "host": parsed.netloc or "ssl.captcha.qq.com",
        "uin": q.get("uin", ""),
        "aid": q.get("aid", ""),
        "cap_cd": q.get("cap_cd", ""),
        "sid": q.get("sid", ""),
        # 原样透传用；去掉我们自己会显式设置的键，避免重复
        "extra": {k: v for k, v in q.items() if k not in ("uin", "aid", "cap_cd", "sid")},
    }
    LOG.info("解析滑块 URL: host=%s uin=%s cap_cd=%s sid=%s extra=%s",
             out["host"], out["uin"], out["cap_cd"], out["sid"], list(out["extra"]))
    return out


def solve_once(
    url: str = "",
    uin: str = "",
    aid: str = "",
    cap_cd: str = "",
    sid: str = "",
    rounds: int = DEFAULT_ROUNDS,
) -> dict:
    """跑一轮（内部按 rounds 重试），返回 {ok, ticket, randstr, seconds, rounds, error}。"""
    started = time.perf_counter()

    parsed = parse_slider_url(url) if url else {}
    uin = uin or parsed.get("uin", "")
    aid = aid or parsed.get("aid", "")
    cap_cd = cap_cd or parsed.get("cap_cd", "")
    sid = sid or parsed.get("sid", "")
    extra = parsed.get("extra") or {}

    # 选端点：给了 QQ 相关参数（url / uin / cap_cd）就走 QQ，否则用腾讯云
    use_qq = bool(url or uin or cap_cd)
    endpoints = ENDPOINTS_QQ if use_qq else ENDPOINTS["tencent"]

    # aid 为空时不要硬塞数字（QQ 那套的 aid 本来就是空的）
    aid_int = int(aid) if str(aid).strip().isdigit() else None

    # 复用常驻 solver —— 这是热态 ~2.4s 与冷启动 ~7s 的差别。
    # 按 (端点, aid) 各缓存一个，两边交替请求也不会互相踢掉。
    # 加锁：solver 有状态，并发调用会让多个 challenge 互相污染指纹。
    with _solver_lock:
        solver = _get_solver(endpoints, aid_int)
        kind = "slide"
        try:
            # QQ 的题型由服务端按 uin + cap_cd 下发，必须原样透传
            if uin:
                solver.qq_uin = str(uin)
            if cap_cd:
                solver.qq_cap_cd = str(cap_cd)
            if sid:
                solver.qq_sid = str(sid)
            # 把 URL 里的其余 query 原样带上（无线页面就是这么喂 TCapIframeApi 的）
            if extra:
                solver.qq_extra_params = dict(extra)
            result = solver.solve(retries=rounds)
        except UnsupportedCaptchaKind as err:
            # 服务端下发的不是滑块 → 交给点选流程（本地 CLIP 识别）
            kind = "click" if "click" in str(err) else "other"
            LOG.info("题型不是滑块（%s），转点选识别", kind)
            result = _solve_click_variant(solver, kind)
            # 点选走的是另一套会话消费方式，这个 solver 的滑块会话已无用
            _drop_solver(endpoints, aid_int)
        except Exception:
            # 其它异常可能是会话被污染（ec=12 之类），下次换新会话更稳
            _drop_solver(endpoints, aid_int)
            raise

    seconds = round(time.perf_counter() - started, 2)
    payload = _build_payload(result, uin=uin, aid=aid, cap_cd=cap_cd,
                             kind=kind, seconds=seconds)
    ok = bool(payload["ok"])

    # 题型不是滑块时，可选回落到第三方（默认关闭，见 FALLBACK_URL）
    if not ok and kind not in ("slide", "click") and FALLBACK_URL:
        fb = _fallback_solve(url=url, uin=uin)
        if fb.get("ok"):
            payload.update(fb)
            payload["kind"] = kind
            payload["solver"] = "fallback"
            LOG.info("↩️ 已回落第三方成功 %.2fs", fb.get("seconds", 0))
    return payload


# 点选特有的诊断字段：题面、选中的格子、候选、置信度、用的是哪种提交格式、
# 重试了几次。**必须显式透传** —— payload 是重新拼的，
# 不带上就在这里丢了，排查时只看得到 errorCode，
# 完全不知道识别结果对不对、是「认错了」还是「环境被风控」。
_PASSTHROUGH_KEYS = (
    "instruction", "pick", "picks", "multi", "candidates", "margin",
    "ans_format", "cv_rank", "refresh_retries", "hybrid_retries",
    "unrecognized_retries", "rounds", "solver",
)


def _build_payload(result: dict, *, uin: str = "", aid: str = "",
                   cap_cd: str = "", kind: str = "slide",
                   seconds: float = 0.0) -> dict:
    """把 solver 的返回拼成 HTTP 响应的 payload。

    抽成独立函数是为了**可测** —— 这里曾经出过一次静默丢信息的事故：

      `click_solver` 返回的失败结构是 `{"error": "选第 4 格…"}`，
      而本函数只读 `result["errMessage"]` → 点选的详细原因被整个丢掉，
      用户只看到 JS 兜底的一句 `errorCode=9`，把「轨迹与答案不一致」
      误判成「会话无效」，白排查了很久。

    这类 bug 不抛异常、不报错，只是信息消失，所以要有测试钉住
    （见 `test_contract.py` 的 ① 与变异测试 M1）。
    """
    ok = str(result.get("errorCode")) == "0" and bool(result.get("ticket"))
    payload = {
        "ok": ok,
        "ticket": result.get("ticket") or "",
        "randstr": result.get("randstr") or "",
        "errorCode": str(result.get("errorCode", "")),
        # 四个键依次兜底：errMessage（各 solver 的现代写法）→ errorMessage
        # → error（点选 solver 用的键）→ 最后才退化成裸 errorCode
        "error": "" if ok else str(
            result.get("errMessage") or result.get("errorMessage")
            or result.get("error") or f"errorCode={result.get('errorCode')}"
        ),
        "seconds": seconds,
        "uin": uin,
        "aid": aid,
        "cap_cd": cap_cd,
        "kind": kind,
    }
    payload["solver"] = result.get("solver") or "local"
    for key in _PASSTHROUGH_KEYS:
        if key in result:
            payload[key] = result[key]
    return payload


def _fallback_solve(url: str = "", uin: str = "") -> dict:
    """把滑块 URL 交给第三方服务，轮询拿 ticket。

    只在 `QQ_SLIDER_FALLBACK` 设了值且本地遇到非滑块题型时才会走到。
    接口形态参照 icqq 插件里「网页」那条通道：
      POST <fallback>  {"url": <滑块 URL>}
      POST <fallback>  {"submit": <uin>}   → {data:{ticket}}
    """
    import urllib.request

    started = time.perf_counter()
    try:
        body = json.dumps({"url": url}).encode()
        req = urllib.request.Request(
            FALLBACK_URL, data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()

        # 轮询取票（第三方要等人工/其算法完成）
        deadline = time.time() + 60
        while time.time() < deadline:
            req = urllib.request.Request(
                FALLBACK_URL,
                data=json.dumps({"submit": uin}).encode(),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read() or b"{}")
            ticket = (data.get("data") or {}).get("ticket") or data.get("ticket")
            if ticket:
                return {
                    "ok": True, "ticket": ticket, "randstr": "",
                    "errorCode": "0", "error": "",
                    "seconds": round(time.perf_counter() - started, 2),
                }
            time.sleep(2)
    except Exception as err:  # noqa: BLE001
        LOG.warning("第三方回落失败: %s: %s", type(err).__name__, err)
    return {"ok": False, "error": "fallback failed"}


class Handler(BaseHTTPRequestHandler):
    server_version = "QQSliderSolver/1.0"

    def log_message(self, fmt, *args):  # noqa: A003
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/health"):
            with _stats_lock:
                snap = dict(_stats)
            total = snap["ok"] + snap["fail"]
            snap["successRate"] = f"{snap['ok'] / total * 100:.0f}%" if total else "-"
            snap["avgSeconds"] = round(snap["total_s"] / total, 2) if total else 0
            snap["avgRounds"] = round(snap["rounds"] / total, 2) if total else 0
            snap["env"] = _env_probe()
            self._json(200, {"status": "ok", **snap})
            return
        if self.path.startswith("/shutdown"):
            # 供插件侧「代码更新了，把旧进程换掉」用。
            #
            # 只允许本机调用：服务默认监听 127.0.0.1，但如果用户把 host 改成
            # 0.0.0.0 暴露出去，这个端点就成了「谁能访问谁就能关服务」。
            # 所以额外校验来源地址必须是回环。
            client = self.client_address[0] if self.client_address else ""
            if client not in ("127.0.0.1", "::1", "localhost"):
                self._json(403, {"error": "shutdown 只允许本机调用"})
                return
            self._json(200, {"status": "shutting-down", "pid": os.getpid()})
            LOG.info("收到 /shutdown（来自 %s），退出以便加载新代码", client)
            # 放到后台线程里退：直接在请求处理线程里 os._exit 会让
            # 上面那个响应还没发完就断连，插件侧会当成「调用失败」。
            threading.Thread(target=_delayed_exit, name="shutdown", daemon=True).start()
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if not self.path.startswith("/solve"):
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw or b"{}")
        except (ValueError, json.JSONDecodeError) as err:
            self._json(400, {"error": f"bad json: {err}"})
            return

        url = str(payload.get("url") or "")
        uin = str(payload.get("uin") or "")
        aid = str(payload.get("aid") or "")
        cap_cd = str(payload.get("cap_cd") or "")
        sid = str(payload.get("sid") or "")
        rounds = int(payload.get("rounds") or DEFAULT_ROUNDS)

        LOG.info("收到过码请求 url=%s uin=%s aid=%s cap_cd=%s rounds=%d",
                 bool(url), uin, aid, cap_cd, rounds)
        try:
            result = solve_once(url=url, uin=uin, aid=aid, cap_cd=cap_cd, sid=sid, rounds=rounds)
        except SlideSolverError as err:
            result = {"ok": False, "ticket": "", "error": f"{type(err).__name__}: {err}", "seconds": 0}
        except Exception as err:  # noqa: BLE001
            LOG.error("未预期异常:\n%s", traceback.format_exc())
            result = {"ok": False, "ticket": "", "error": f"{type(err).__name__}: {err}", "seconds": 0}

        _bump(result.get("ok", False), float(result.get("seconds") or 0), rounds)
        if result.get("ok"):
            LOG.info("✅ 过码成功 %.2fs ticket=%s…", result["seconds"], result["ticket"][:16])
            self._json(200, result)
        else:
            LOG.warning("❌ 过码失败 %.2fs %s", result.get("seconds", 0), result.get("error"))
            self._json(200, result)


def warmup() -> None:
    """启动预热：把 Node worker 拉起来、会话灌好 cookie。

    不预热的话**第一次**过码要 6~7 秒（Node 冷启动 + 会话预热），
    之后才降到 2 秒级。登录是偶发操作，很可能一次登录只过一次码 ——
    那次就是「第一次」。所以启动时先把这两件事做掉。

    预热不碰 challenge（不消耗额度、不产生 verify 请求），只是：
      · 起 Node worker 并等它 ready
      · 按真实浏览器顺序拉一遍 tcaptcha-frame / drag_ele / dy-jy3 / dy-ele
        （`_warm_session` 里做的，会拿到 TDC_itoken 之类的 cookie）

    **两个端点都预热**（QQ 是登录场景，腾讯云是自测/对照场景），
    否则交替请求时总有一边要冷启动。
    """
    with _solver_lock:
        for name, endpoints in (("qq", ENDPOINTS_QQ), ("tencent", ENDPOINTS["tencent"])):
            try:
                solver = _get_solver(endpoints, None)
                solver._ensure_open()
                solver._warm_session(force=True)
                LOG.info("预热完成（%s）：Node worker 与会话已就绪", name)
            except Exception as err:  # noqa: BLE001
                LOG.warning("预热失败（%s，不影响后续请求，只是第一次会慢）：%s: %s",
                            name, type(err).__name__, err)
                _drop_solver(endpoints, None)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="腾讯 TCaptcha 滑块过码服务")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--no-warmup", action="store_true",
                        help="跳过启动预热（第一次过码会慢到 6~7 秒）")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="[%(asctime)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 注意：DEFAULT_ROUNDS 已在 solve_once 的默认参数里被求值过，
    # 这里不能再 global 声明，改用模块属性直接改（见 _set_default_rounds）。
    _set_default_rounds(args.rounds)

    LOG.info("腾讯滑块过码服务已启动 http://%s:%d（最多 %d 轮/次）", args.host, args.port, args.rounds)

    # 预热放后台线程：端口立刻可用，预热期间来的请求会在 _solver_lock 上等它做完
    # （等的是几秒，比让第一次请求自己冷启动更快，也不会失败）。
    if not args.no_warmup:
        threading.Thread(target=warmup, name="warmup", daemon=True).start()

    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
