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


# ── ans 候选格式 ───────────────────────────────────────────────────────
#
# ★ 第 ① 种是**腾讯前端源码确认的正确格式**，默认只用它。
#
# 依据（2026-10-08 实扒腾讯脚本，不再是猜的）：
#   QQ 登录点选真正加载的是 `https://captcha.gtimg.com/1/dy-ele.d10b59c0.js`
#   （由 `t.captcha.qq.com/template/drag_ele.html` 引入；turing 域名那两份
#   dy-ele 里**根本没有 DynAnswerType_UC**，所以之前照它们猜的格式必然错）。
#
#   SelectEl.prototype.addData 原文：
#
#       if ("DynAnswerType_UC" === l) {
#         m.push(h.id)                      // h.id = select_region_list 里的区域编号
#         emit("setData", { namespace: "selectEl",
#           data: [{ elem_id: 1, type: "DynAnswerType_UC", data: m.join(",") }] })
#       }
#
#   → **一条记录、`elem_id` 恒为 1、`data` 是「区域编号的逗号串」**，
#     既不是坐标，也不是「一点一条、elem_id 递增」。
#     单选时 data 就是一个编号（如 `"3"`）。
#
#   对照：同文件里 `DynAnswerType_POS` / `_POS_L`（clickEl）才提交坐标，
#   而服务端给点选下发的是 `data_type: ["DynAnswerType_UC"]`，走的正是上面这支。
#
#   另一条旁证：`DynAnswerType_UC` 只存在于 QQ 那份 dy-ele，且它同文件里
#   还带 `DynAnswerType_ID`（提交 mask 的 id 数组）—— 两者共用 `this.masks`，
#   再次说明 UC 的 data 就是区域编号。
#
# 约定：builder 收 `(pts, tile_ids)`
#   · pts      —— 点击坐标 [(x,y), ...]（喂给 TDC 造轨迹用，不进 ans）
#   · tile_ids —— 对应格子的 1-based 编号（`select_region_list` 里的 id）
ANS_FORMATS: tuple[tuple[str, Any], ...] = (
    # ① 【正确格式】一条记录，elem_id=1，data=区域编号逗号串
    ("uc_region_ids", lambda pts, ids: json.dumps(
        [{"elem_id": 1, "type": "DynAnswerType_UC",
          "data": ",".join(str(i) for i in ids)}], separators=(",", ":"))),
    # 以下三种**仅为排查保留**（QQ_SLIDER_CLICK_PROBE=1 时才会试），
    # 源码已证明它们不对，留着是为了万一腾讯改版时能快速对照。
    # ② 坐标、分号分隔
    ("uc_join_semicolon", lambda pts, ids: json.dumps(
        [{"elem_id": 1, "type": "DynAnswerType_UC",
          "data": ";".join(f"{x},{y}" for x, y in pts)}], separators=(",", ":"))),
    # ③ 每个点击一条记录，elem_id 递增
    ("uc_multi_record", lambda pts, ids: json.dumps(
        [{"elem_id": i + 1, "type": "DynAnswerType_UC", "data": f"{x},{y}"}
         for i, (x, y) in enumerate(pts)], separators=(",", ":"))),
    # ④ 坐标、逗号连着铺开
    ("uc_join_comma", lambda pts, ids: json.dumps(
        [{"elem_id": 1, "type": "DynAnswerType_UC",
          "data": ",".join(f"{x},{y}" for x, y in pts)}], separators=(",", ":"))),
)

# 点选 verify 的 errorCode 语义（扒自 dy-ele 的 verify 分发表）。
#
#   0   verifySuccess          成功
#   9   verifyFailRefresh      验证失败，**换一张题重来**（不是「会话无效」！）
#   12  verifyError            风控/环境异常
#   20  verifySessionTimeout   会话超时
#   50  verifyFail             答案不对
#   51  verifyHybrid           混合验证（答案不对那类）
#   52  verifyError
#   206 verifySessionTimeout
#
# ⚠️ 9 的语义尤其重要：历史记录里把「假 cap_cd 会话下的 ec=9」理解成
# 「会话本身无效、无法验证格式」，于是放弃了从提交实验反推格式。
# 现在源码在手，格式已经确定，不必再依赖那个推断。
EC_SUCCESS = "0"
EC_WRONG_ANSWER = ("50", "51")
EC_REFRESH = "9"          # 换题重来



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


def _parse_challenge(pre: dict) -> dict:
    """从一次 prehandle 的结果里抽出「题面 / 格子坐标 / 格子编号 / 图 URL / 会话」。

    每换一次会话都要重新解析 —— 因为**每张新图的题目和正解位置都不同**，
    旧会话上的识别结果对新会话毫无意义（这是很容易踩的坑）。
    """
    data = pre.get("data") or {}
    dyn = data.get("dyn_show_info") or {}
    ccfg = data.get("comm_captcha_cfg") or {}
    sess = pre.get("sess")
    instruction = str(dyn.get("instruction") or "").strip()

    try:
        payload = json.loads(dyn.get("json_payload") or "{}")
    except (ValueError, TypeError):
        payload = {}
    raw_regions = [r for r in (payload.get("select_region_list") or []) if isinstance(r, dict)]
    regions = [r.get("range") for r in raw_regions]
    regions = [r for r in regions if isinstance(r, (list, tuple)) and len(r) >= 4]
    # 格子的编号（提交区域编号那种格式要用，不能瞎填 1）
    tile_ids = [
        int(r["id"]) if str(r.get("id", "")).isdigit() else i + 1
        for i, r in enumerate(raw_regions)
    ]
    if len(tile_ids) != len(regions):
        tile_ids = list(range(1, len(regions) + 1))

    bg = dyn.get("bg_elem_cfg") or {}
    return {
        "sess": sess,
        "instruction": instruction,
        "regions": regions,
        "tile_ids": tile_ids,
        "img_url": bg.get("img_url"),
        "ccfg": ccfg,
    }


def _prepare_submission(solver, ch: dict, pick_index: int) -> dict:
    """把「选中的那一格」变成可提交的四件套：ans 坐标、格子编号、collect+eks、pow。

    保证**轨迹与答案自洽**：collect 只喂真正点的那一格（单选场景），
    绝不把「看过的 6 格」当成「点过的 6 格」。
    """
    regions = ch["regions"]
    tile_ids = ch["tile_ids"]
    if not 0 <= pick_index < len(regions):
        raise ClickSolveError(f"格子编号越界：{pick_index}（共 {len(regions)} 格）")

    centers = _region_centers(regions)
    pick_pt = centers[pick_index]
    pick_id = tile_ids[pick_index] if pick_index < len(tile_ids) else pick_index + 1

    tdc_path = str(ch["ccfg"].get("tdc_path", ""))
    m = re.search(r"(?:\?|&)app_data=([^&]+)&t=(\d+)", tdc_path)
    if not m:
        raise ClickSolveError("tdc_path 里没有 app_data")
    tdc_source = solver._tdc_download(m.group(1), m.group(2))

    gen = solver._get_collect(tdc_source, _click_events([pick_pt]), None)
    tokenid = gen.get("tokenid") or gen.get("token_id")
    if tokenid:
        solver._tdc_token = str(tokenid)
        try:
            solver.session.cookies.set("TDC_itoken", f"{tokenid}:1", domain=".qcloud.com", path="/")
            solver.session.cookies.set("TDC_itoken", f"{tokenid}:1", domain=".gtimg.com", path="/")
        except Exception:  # noqa: BLE001
            pass

    pow_cfg = ch["ccfg"].get("pow_cfg")
    if not isinstance(pow_cfg, dict):
        raise ClickSolveError("pow_cfg 缺失")
    pow_answer, pow_ms = solver._pow(str(pow_cfg.get("prefix", "")), str(pow_cfg.get("md5", "")))

    return {
        "pts": [pick_pt],
        "ids": [pick_id],
        "collect": str(gen.get("collect", "")),
        "eks": str(gen.get("eks", "")),
        "pow_answer": pow_answer,
        "pow_ms": pow_ms,
    }


def solve_click(
    solver,
    *,
    recognizer,
    top_k: int = 3,
    probe_formats: bool | None = None,
    refresh_attempts: int = 3,
) -> dict[str, Any]:
    """跑一次点选题。

    复用调用方（`SlideSolver`）的会话、TDC worker、pow 等机制 ——
    点选和滑块走的是同一套 verify 通道，只是 ans 不同。

    @param solver        SlideSolver 实例（已 `_ensure_open()`）
    @param recognizer    ClickRecognizer 实例
    @param top_k         CV 预筛保留几格给 CLIP
    @param probe_formats **是否逐个试 ans 格式**。
        默认**只试源码确认的那一种**（快，~5s）。打开后把 4 种都试一遍
        （慢一倍，每次都要换新会话），只在排查「腾讯改版了没有」时用。
        默认值取环境变量 `QQ_SLIDER_CLICK_PROBE`。
    @param refresh_attempts
        默认格式下遇到 `ec=9`（`verifyFailRefresh`）时的重试次数。
        ec=9 的语义是「这道题没过，换一张重来」，所以值得换会话再试；
        每换一次会话就是**另一道题**，必须重新识别。
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

    # 默认只用 `ANS_FORMATS[0]`（腾讯源码确认的格式），但 ec=9 时换题重试；
    # 探格式模式才逐个试 4 种（每换一种都要重新 prehandle 换会话，
    # 而换了会话就是**另一道题**，得重新识别，代价很高）。
    if probe_formats:
        plan: list[tuple[str, Any]] = list(ANS_FORMATS)
    else:
        first = ANS_FORMATS[0]
        plan = [first] * max(1, int(refresh_attempts))

    result: dict[str, Any] = {}
    used_format = ""
    instruction = ""
    last_pick = 0
    last_cands = None
    last_margin = None
    refresh_seen = 0

    for idx, (name, build) in enumerate(plan):
        # 每一轮都从**当前这一张图**重新取题、重新识别 ——
        # 绝不复用上一轮的识别结果：换了会话就是换了题，旧索引是废的。
        if idx > 0:
            LOG.debug("换会话重试（格式 %s，第 %d 次）—— 新题，需重新识别", name, idx + 1)
        pre = solver.prehandle()

        if pre.get("state") != 1:
            LOG.warning("格式 %s：prehandle state=%r", name, pre.get("state"))
            continue
        ch = _parse_challenge(pre)
        if not isinstance(ch["sess"], str) or not ch["sess"]:
            LOG.warning("格式 %s：prehandle 没给 sess", name)
            continue
        if not ch["instruction"] or not ch["regions"]:
            LOG.warning("格式 %s：题面或格子缺失", name)
            continue
        if not isinstance(ch["img_url"], str) or not ch["img_url"]:
            LOG.warning("格式 %s：背景图 URL 缺失", name)
            continue

        if idx == 0:
            LOG.info("点选：题面=%r 格子=%d", ch["instruction"], len(ch["regions"]))

        # 识别（每轮都做）
        from urllib.parse import urljoin

        img_bytes = solver._download_bytes(urljoin(solver.endpoints.captcha_base_url, ch["img_url"]))
        try:
            rec = recognizer.recognize(img_bytes, ch["instruction"], ch["regions"])
        except Exception as err:  # noqa: BLE001
            if idx == 0:
                raise ClickNotSupported(f"点选识别失败：{type(err).__name__}: {err}") from err
            LOG.warning("格式 %s：识别失败 %s", name, err)
            continue

        picked_index = int(rec["pick_index"])
        if idx == 0 or rec.get("margin") is not None:
            LOG.info("点选识别：选第 %d 格（候选 %s，margin=%.1f，%.2fs）",
                     picked_index + 1, rec.get("candidates"),
                     rec.get("margin") or 0, rec.get("seconds", 0))
        instruction = ch["instruction"]
        last_pick = picked_index + 1
        last_cands = rec.get("candidates")
        last_margin = rec.get("margin")

        try:
            sub = _prepare_submission(solver, ch, picked_index)
        except ClickSolveError as err:
            LOG.warning("格式 %s：准备提交失败 %s", name, err)
            continue

        ans_json = build(sub["pts"], sub["ids"])
        LOG.debug("点选提交格式=%s ans=%s", name, ans_json)
        try:
            result = _submit(solver, ch["sess"], sub["collect"], sub["eks"],
                             ans_json, sub["pow_answer"], sub["pow_ms"])
        except Exception as err:  # noqa: BLE001
            LOG.warning("点选提交（%s）异常 %s: %s", name, type(err).__name__, err)
            continue

        ec = str(result.get("errorCode", ""))
        used_format = name
        if ec == EC_SUCCESS and result.get("ticket"):
            LOG.info("点选成功（格式 %s）", name)
            break
        if ec in EC_WRONG_ANSWER:
            # 格式被接受、只是答案不对 → 再换格式也没用（换的只是 ans 拼法）
            LOG.info("点选格式 %s 被接受，但答案不对（ec=%s）→ 停止换格式", name, ec)
            break
        if ec == EC_REFRESH:
            # verifyFailRefresh：这道题没过，换一张题再来（不是会话无效）
            refresh_seen += 1
            LOG.info("点选 ec=9（verifyFailRefresh），换一张题重试（第 %d 次）", refresh_seen)
            continue
        LOG.debug("点选格式 %s 返回 ec=%s，试下一个", name, ec)

    seconds = round(time.perf_counter() - started, 2)
    ec = str(result.get("errorCode", ""))
    ok = ec == EC_SUCCESS and bool(result.get("ticket"))

    # 失败原因要能一眼看懂：只报 `errorCode=9` 谁也判断不出是「认错了」还是
    # 「环境被风控」。所以把**识别结果 + 重试次数 + 格式**一起写进 error，
    # 日志里另有完整行（题面、候选、margin）。
    if ok:
        err_text = ""
    elif ec == EC_REFRESH:
        err_text = f"点选未通过（选第 {last_pick} 格，换题重试 {refresh_seen} 次仍失败）"
    elif ec in EC_WRONG_ANSWER:
        err_text = f"点选答案不对（选第 {last_pick} 格，errorCode={ec}）"
    elif ec == "12":
        err_text = f"点选被风控拦截（errorCode={ec}）"
    elif ec:
        err_text = f"点选失败（选第 {last_pick} 格，errorCode={ec}）"
    else:
        err_text = str(result.get("errMessage") or result.get("errorMessage") or "点选失败")

    return {
        "ok": ok,
        "ticket": result.get("ticket") or "",
        "randstr": result.get("randstr") or "",
        "errorCode": ec,
        "error": err_text,
        # 上层（server.solve_once）读的是 errMessage，两边保持一致
        "errMessage": err_text,
        "seconds": seconds,
        "kind": "click",
        "instruction": instruction,
        "pick": last_pick,
        "candidates": last_cands,
        "margin": last_margin,
        "ans_format": used_format,
        "refresh_retries": refresh_seen,
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
