"""点选题型的解题流程（识别 → 提交）。

## 与滑块的区别

| | 滑块 | 点选（本模块）|
|---|---|---|
| 答案来源 | OpenCV 找缺口 | **CLIP 看图认语义**（见 `click_recognizer`）|
| 操作 | 拖拽轨迹 | 点击若干格 |
| ans 类型 | `DynAnswerType_POS` | `DynAnswerType_UC` |
| 题面 | 无 | **`instruction` 中文词** |

## 协议实测事实（60 张真题 + 24 次重复采样）

    instruction      = "百香果"        # 题面，唯一的语义线索
    json_payload     = {"select_region_list":[{"id":1,"range":[0,34,220,254]}, ...],
                        "prompt_id":166767, "picture_ids":[1,2,3,4,5,6], ...}
    show_type        = "click_image_uncheck"
    watermark        = "混元AI生成"

- **`select_region_list` 只有坐标，没有任何答案字段** —— 必须看图
- `prompt_id` 与题面**严格一一对应**（同一 id 永远是同一题面），
  但**每次的图和正解位置都不同**（实测同一 prompt_id 两次，梨分别在 #6 和 #3）
  → 所以「按题面缓存答案」行不通，只能每次现场认
- `fg_elem_list` 不存在（点选没有前台元件）

## ⚠️ 提交格式未能实测验证（诚实说明）

用静态探测的假 `cap_cd` 无法验证答案格式：6 种截然不同的 ans 格式
**全部返回 ec=9**，说明服务端在「解析 ans」之前就拒了（会话本身无效）。

所以本模块把候选格式**按可能性排序依次重试**，并把命中结果记进日志 ——
等到有真实登录 URL（真 cap_cd）时，日志会直接告诉我们哪种对。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Sequence

LOG = logging.getLogger(__name__)


class ClickSolveError(RuntimeError):
    """点选解题失败。"""


class ClickNotSupported(ClickSolveError):
    """点选识别能力不可用（模型没装好）。"""


# ── ans 候选格式（按可能性排序）────────────────────────────────────────
#
# 依据：滑块用的是 [{"elem_id":1,"type":"DynAnswerType_POS","data":"x,y"}]，
# 点选是同一个 ans 容器，只是 type 换成 UC、data 是点击坐标。
# 多选时 data 怎么分隔有几种可能，所以都给出来依次试。
ANS_FORMATS: tuple[tuple[str, Any], ...] = (
    # ① 最像滑块：一条记录，多点用分号串
    ("uc_join_semicolon", lambda pts: json.dumps(
        [{"elem_id": 1, "type": "DynAnswerType_UC",
          "data": ";".join(f"{x},{y}" for x, y in pts)}], separators=(",", ":"))),
    # ② 每个点击一条记录，elem_id 递增
    ("uc_multi_record", lambda pts: json.dumps(
        [{"elem_id": i + 1, "type": "DynAnswerType_UC", "data": f"{x},{y}"}
         for i, (x, y) in enumerate(pts)], separators=(",", ":"))),
    # ③ 一条记录，坐标用逗号连着铺开
    ("uc_join_comma", lambda pts: json.dumps(
        [{"elem_id": 1, "type": "DynAnswerType_UC",
          "data": ",".join(f"{x},{y}" for x, y in pts)}], separators=(",", ":"))),
    # ④ elem_id 直接填区域 id（有些实现用区域编号而不是坐标）
    ("uc_region_ids", lambda pts: json.dumps(
        [{"elem_id": 1, "type": "DynAnswerType_UC",
          "data": ",".join(str(i + 1) for i in range(len(pts)))}], separators=(",", ":"))),
)


def _region_centers(regions: Sequence[Sequence[int]]) -> list[tuple[int, int]]:
    """把 [x0,y0,x1,y1] 换成中心点坐标（点击位置）。"""
    out = []
    for r in regions:
        if len(r) < 4:
            continue
        x0, y0, x1, y1 = (int(v) for v in r[:4])
        out.append(((x0 + x1) // 2, (y0 + y1) // 2))
    return out


def _click_events(pts: Sequence[tuple[int, int]]):
    """构造点选的输入事件序列（mousedown→mouseup→click，每格一组）。

    为什么不是一条 mouseup 了事：TDC 的 collect 记录的是真实交互节奏，
    点选里「按下-抬起-点击」是三次独立事件，少一个都会让 collect 看起来不像人。
    """
    from slide_solver import DragEvent  # 延迟 import，避免循环依赖

    events: list[DragEvent] = []
    t = 0
    for i, (x, y) in enumerate(pts):
        events.append(DragEvent("mousemove", x, y, t, button=0, buttons=0))
        events.append(DragEvent("mousedown", x, y, t + 40 + i * 15, button=0, buttons=1))
        events.append(DragEvent("mouseup", x, y, t + 110 + i * 15, button=0, buttons=0))
        events.append(DragEvent("click", x, y, t + 125 + i * 15, button=0, buttons=0))
        t += 520 + int((i % 3) * 180)   # 每次点击之间留人味间隔
    return events


def solve_click(
    solver,
    *,
    recognizer,
    top_k: int = 3,
    max_picks: int = 3,
    probe_formats: bool | None = None,
    debug: bool = False,
) -> dict[str, Any]:
    """跑一次点选题。

    复用调用方（`SlideSolver`）的会话、TDC worker、pow 等机制 ——
    点选和滑块走的是同一套 verify 通道，只是 ans 不同。

    @param solver        SlideSolver 实例（已 `_ensure_open()`）
    @param recognizer    ClickRecognizer 实例
    @param top_k         CV 预筛保留几格给 CLIP
    @param probe_formats **是否逐个试 ans 格式**。
        默认只试最可能的那一种（快，~4s）。打开后会把 4 种都试一遍
        （慢一倍，每次都要换新会话），只在排查「提交格式到底哪个对」时用。
        默认值取环境变量 `QQ_SLIDER_CLICK_PROBE`。
    @returns 与滑块一致的 result dict（含 ok/ticket/errorCode/...）
    """
    if probe_formats is None:
        probe_formats = (os.environ.get("QQ_SLIDER_CLICK_PROBE") or "").strip() in ("1", "true", "yes")
    started = time.perf_counter()

    # ── ① prehandle 拿题面、格子、图 ──────────────────────────────────
    pre = solver.prehandle()
    if pre.get("state") != 1:
        raise ClickSolveError(f"prehandle state={pre.get('state')!r}")
    data = pre.get("data") or {}
    dyn = data.get("dyn_show_info") or {}
    ccfg = data.get("comm_captcha_cfg") or {}
    sess = pre.get("sess")
    if not isinstance(sess, str) or not sess:
        raise ClickSolveError("prehandle 没给 sess")

    instruction = str(dyn.get("instruction") or "").strip()
    try:
        payload = json.loads(dyn.get("json_payload") or "{}")
    except (ValueError, TypeError):
        payload = {}
    raw_regions = payload.get("select_region_list") or []
    regions = [r.get("range") for r in raw_regions if isinstance(r, dict)]
    regions = [r for r in regions if isinstance(r, (list, tuple)) and len(r) >= 4]

    if not instruction:
        raise ClickSolveError("服务端没给题面（instruction 为空），无法识别")
    if not regions:
        raise ClickSolveError("服务端没给可选区域（select_region_list 为空）")

    bg = dyn.get("bg_elem_cfg") or {}
    img_url = bg.get("img_url")
    if not isinstance(img_url, str) or not img_url:
        raise ClickSolveError("背景图 URL 缺失")

    LOG.info("点选：题面=%r 格子=%d", instruction, len(regions))

    # ── ② 下载图 + 识别 ────────────────────────────────────────────────
    from urllib.parse import urljoin

    img_bytes = solver._download_bytes(urljoin(solver.endpoints.captcha_base_url, img_url))
    try:
        rec = recognizer.recognize(img_bytes, instruction, regions)
    except Exception as err:  # noqa: BLE001
        raise ClickNotSupported(f"点选识别失败：{type(err).__name__}: {err}") from err

    picked_index = int(rec["pick_index"])
    LOG.info("点选识别：选第 %d 格（候选 %s，margin=%.1f，%.2fs）",
             picked_index + 1, rec.get("candidates"), rec.get("margin") or 0, rec.get("seconds", 0))

    # ── ③ 生成 collect/eks（喂入点击事件，让轨迹像人点的）──────────────
    tdc_path = str(ccfg.get("tdc_path", ""))
    m = re.search(r"(?:\?|&)app_data=([^&]+)&t=(\d+)", tdc_path)
    if not m:
        raise ClickSolveError("tdc_path 里没有 app_data")
    tdc_source = solver._tdc_download(m.group(1), m.group(2))

    centers = _region_centers(regions)
    if len(centers) != len(regions):
        raise ClickSolveError("区域坐标不合法")
    pick_pts = [centers[picked_index]]
    gen = solver._get_collect(tdc_source, _click_events(centers), None)
    collect = str(gen.get("collect", ""))
    eks = str(gen.get("eks", ""))
    tokenid = gen.get("tokenid") or gen.get("token_id")
    if tokenid:
        solver._tdc_token = str(tokenid)
        try:
            solver.session.cookies.set("TDC_itoken", f"{tokenid}:1", domain=".qcloud.com", path="/")
            solver.session.cookies.set("TDC_itoken", f"{tokenid}:1", domain=".gtimg.com", path="/")
        except Exception:  # noqa: BLE001
            pass

    # ── ④ pow ─────────────────────────────────────────────────────────
    pow_cfg = ccfg.get("pow_cfg")
    if not isinstance(pow_cfg, dict):
        raise ClickSolveError("pow_cfg 缺失")
    pow_answer, pow_ms = solver._pow(str(pow_cfg.get("prefix", "")), str(pow_cfg.get("md5", "")))

    # ── ⑤ 提交 ────────────────────────────────────────────────────────
    #
    # 默认只试第一种格式（`uc_join_semicolon`，与滑块 ans 结构最接近）：
    # 每换一种格式都要**重新 prehandle 换会话**（贵，实测 +2.5s/次），
    # 常规路径不该为不确定的猜测付这个代价。
    # 要探明哪种格式对时设 QQ_SLIDER_CLICK_PROBE=1。
    formats = ANS_FORMATS if probe_formats else ANS_FORMATS[:1]

    result: dict[str, Any] = {}
    used_format = ""
    for idx, (name, build) in enumerate(formats):
        if idx > 0:
            # 换会话重来：上一次的 sess 已经被消费
            try:
                pre2 = solver.prehandle()
                d2 = pre2.get("data") or {}
                dyn2 = d2.get("dyn_show_info") or {}
                sess = pre2.get("sess") or sess
                c2 = d2.get("comm_captcha_cfg") or {}
                pj2 = json.loads(dyn2.get("json_payload") or "{}")
                r2 = [r.get("range") for r in (pj2.get("select_region_list") or []) if isinstance(r, dict)]
                r2 = [r for r in r2 if isinstance(r, (list, tuple)) and len(r) >= 4]
                if r2:
                    regions = r2
                    centers = _region_centers(regions)
                    pick_pts = [centers[min(picked_index, len(centers) - 1)]]
                m2 = re.search(r"(?:\?|&)app_data=([^&]+)&t=(\d+)", str(c2.get("tdc_path", "")))
                if m2:
                    tdc_source = solver._tdc_download(m2.group(1), m2.group(2))
                gen = solver._get_collect(tdc_source, _click_events(centers), None)
                collect = str(gen.get("collect", ""))
                eks = str(gen.get("eks", ""))
                pw2 = c2.get("pow_cfg") or {}
                if pw2:
                    pow_answer, pow_ms = solver._pow(str(pw2.get("prefix", "")), str(pw2.get("md5", "")))
            except Exception as err:  # noqa: BLE001
                LOG.debug("换会话重试失败（沿用旧会话）：%s", err)

        ans_json = build(pick_pts)
        LOG.debug("点选提交格式=%s ans=%s", name, ans_json)
        try:
            result = _submit(solver, sess, collect, eks, ans_json, pow_answer, pow_ms)
        except Exception as err:  # noqa: BLE001
            LOG.warning("点选提交（%s）异常 %s: %s", name, type(err).__name__, err)
            continue

        ec = str(result.get("errorCode", ""))
        used_format = name
        # 0 = 成功；其它码里 50/51 是「答案错/被拒」，说明格式被接受了
        if ec == "0" and result.get("ticket"):
            LOG.info("点选成功（格式 %s）", name)
            break
        if ec in ("50", "51"):
            LOG.info("点选格式 %s 被接受，但答案不对（ec=%s）→ 停止换格式", name, ec)
            break
        LOG.debug("点选格式 %s 返回 ec=%s，试下一个", name, ec)

    seconds = round(time.perf_counter() - started, 2)
    ok = str(result.get("errorCode")) == "0" and bool(result.get("ticket"))
    return {
        "ok": ok,
        "ticket": result.get("ticket") or "",
        "randstr": result.get("randstr") or "",
        "errorCode": str(result.get("errorCode", "")),
        "error": "" if ok else str(result.get("errMessage") or result.get("errorMessage")
                                   or f"errorCode={result.get('errorCode')}"),
        "seconds": seconds,
        "kind": "click",
        "instruction": instruction,
        "pick": picked_index + 1,
        "candidates": rec.get("candidates"),
        "margin": rec.get("margin"),
        "ans_format": used_format,
        "solver": "local-clip",
    }


def _submit(solver, sess, collect, eks, ans_json, pow_answer, pow_ms) -> dict:
    """把点选的 ans 提交到 verify 端点。

    不走 solver._verify —— 那个方法是给滑块写死的（它自己拼 POS 的 ans）。
    """
    resp = solver.session.post(
        solver.endpoints.verify_url,
        data={
            "collect": collect,
            "tlg": len(collect),
            "eks": eks,
            "sess": sess,
            "ans": ans_json,
            "pow_answer": pow_answer,
            "pow_calc_time": pow_ms,
        },
        timeout=solver._request_timeout,
        headers={
            **solver._fetch_headers(referer=solver.endpoints.captcha_referer),
            "Origin": solver.endpoints.captcha_referer.rstrip("/"),
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    resp.raise_for_status()
    return resp.json()
