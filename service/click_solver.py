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

## ★ 两种点选：单选 vs 多选（2026-10-08 打通全流程）

`json_payload.lang_headers` 里的措辞决定了这道题要选几格：

| 语言头 | 含义 | 出没规律 | 正确做法 |
|---|---|---|---|
| `选择$最$符合描述的图片` | 单选 | **第一题恒为单选** | 取最高分 1 格 → 返 `ec=51` |
| `选择$%所有%$符合描述的图片` | **多选** | **51 之后的续做题恒为多选** | 全 6 格打分 → **Otsu 断层切簇** → 整簇提交 |

**`ec=51` 不是「答案对了」，而是「第一题答完，进入混合验证阶段」的流程信号**
—— 实测 9/9 第一题都给 51（连 margin 只有 7.8 的那次也是），
一个 88% 准确率的模型不可能 9/9 全对。

**真正拿到 ticket 的是多选那一轮**：实测用断层切分提交，
返回 `{"errorCode":"0","ticket":"t03tserver3...","randstr":"@fb8"}` ——
`0` 就是源码里的 `verifySuccess`。

## 多选为什么必须按「断层」切，不能固定选 k 格

多选的**正解格数不固定**（实测 2、3、4 格都有）。固定「选前 k 格」
连续 15 次全错，原因都是**少选了一格**。真正的信号是分数断层：

    湖边  42.4 32.9 42.6 36.5 43.4 35.5   → 高分簇 {5,3,1}
    海角  36.9 40.4 33.1 41.2 37.3 35.9   → 高分簇 {4,2}
    消防车 31.3 44.8 30.6 48.5 46.6 45.9   → 高分簇 {4,5,6,2}

切分用 **1-D Otsu**（类间方差最大）而不是「相邻两格最大差值」——
后者会被末尾那个离群低分格抢走最大间隔（实测「扫雪车」
`48.2 47.3 46.7 45.0 36.2 25.7` 最大间隔切出 k=5 是错的，Otsu 切 k=4 才对）。

## ans 提交格式（源码确认 + 实跑验证）

    腾讯源码 SelectEl.prototype.addData：
      m.push(h.id);  data:[{elem_id:1, type:"DynAnswerType_UC", data:m.join(",")}]

    即 **一条记录、elem_id 恒为 1、data 是「区域编号的逗号串」**。
    单选就一个编号，多选是 `"4,5,6,2"`。已用真 ticket 验证过。
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
#   30  verifyHybrid           ★ 混合验证：服务端给了**新 sess**，要带着它续做
#   50  verifyFail             **答案真的不对**
#   51  verifyHybrid           ★ 同 30（实测点选第一题必返 51）
#   52  verifyError
#   206 verifySessionTimeout
#
# ⚠️ 两个曾经踩过的坑，别再踩回去：
#
# ① **9 不是「会话无效」**。历史记录里把「假 cap_cd 会话下的 ec=9」理解成
#    「会话本身无效、无法验证格式」，于是放弃了从提交实验反推格式。
#    它的真实语义是 `verifyFailRefresh` —— 这道题没过，换一道重来。
#    （实测：每次提交都会推进题目，所以「同一道题反复提交试组合」全是无效实验，
#      这也是之前「多选怎么试都错」的根因 —— 第二次提交用的已经是旧题的格子了。）
#
# ② **51/30 不是「答案不对」**。这张表最初被读成「51 = 混合验证（答案不对那类）」，
#    于是代码把 51 和 50 一起当「答案不对」直接放弃，还打出一句
#    「点选答案不对」的误导文案。真实链路（`tcaptcha-frame.js` 原文）是：
#
#        // 内层 dy-ele：verify 拿到 51 → Notify.hybridVerify(resp.sess)
#        // 外层 tcaptcha-frame：收到 messageType 8
#        case 8: e.onHybridVerify(r.sess, k.isDialogHybridScene(r) ? "dialog" : undefined)
#        e.prototype.onHybridVerify = function (e, t) {
#          if ("dialog" === t) return this.hybridForDialog = !0, void this.msgChannel.publish(h.Topics.HybridVerify, e);
#          this.clearContainerAndEl(), this.preCreate(e)          // ← 带着 sess 重建
#        }
#        t.subscribe(c.Topics.HybridVerify, function (t, i) { e.startPreHandle(i) })
#        e.prototype.startPreHandle = function (e) { this.getPreHandleNew({ sess: e, ... }) }
#        e.prototype.getPreHandleNew = function (e) {
#          g({ url: a, data: { sess: e.sess || "", agent_id: ..., agent_auth_sign: ... },
#              success: function (i) {
#                if (i.ticket) ...onSuccess(...)      // 也可能**直接给 ticket**
#                if (217 === i.state) ...preHandleRateLimit()
#              }})
#        }
#
#    → 51 的正确处理是：**拿响应里的新 sess 重新 prehandle，在同一流程里续做**，
#      而不是换新会话、更不是放弃。
#
# 实测（2026-10-08）三条硬证据：
#   · 51 的响应体带 `sess` 字段（`errorCode/randstr/ticket/errMessage/sess`）
#   · 带该 sess 重新 prehandle **能拿到下一道题**，且 `sid` 与发起 51 的那个会话**完全相同**
#     （对照：不带 sess / 带垃圾 sess / 带无关参数，三者 sid 全都不同）
#   · 全程 sid 不变 ⇒ 风控指纹连续，正是「同一流程续做」该有的样子
EC_SUCCESS = "0"
EC_HYBRID = ("30", "51")  # ★ 混合验证：带响应里的 sess 续做，不是答案不对
EC_WRONG_ANSWER = ("50",)  # 只有 50 才是真的「答案不对」
EC_REFRESH = "9"           # 换题重来（源码里 refresh() 不带 sess，是全新会话）



def _img_fingerprint(img_url: Any) -> str:
    """取「这张图」的指纹 —— 只用 `image=` 那个哈希，**不含 sess**。

    为什么不能直接拿整个 `img_url` 当指纹：URL 形如

        /cap_union_new_getcapbysig?img_index=1&image=<哈希>&sess=<会话>

    同一张图在换会话后 `sess` 就变了，整个字符串跟着变 ——
    拿它比对永远不相等，「有没有换题」的判断等于失效。
    （`test_click_retry.py` 用例 ⑥ 就是靠这个抓出来的。）

    取不到 `image=` 时退化成「去掉 sess 参数后的其余部分」，仍比整串可靠。
    """
    s = str(img_url or "")
    m = re.search(r"[?&]image=([^&]+)", s)
    if m:
        return m.group(1)
    return re.sub(r"[?&]sess=[^&]*", "", s)


def _region_centers(regions: Sequence[Sequence[int]]) -> list[tuple[int, int]]:
    """把 [x0,y0,x1,y1] 换成中心点坐标（点击位置）。"""
    out = []
    for r in regions:
        if len(r) < 4:
            continue
        x0, y0, x1, y1 = (int(v) for v in r[:4])
        out.append(((x0 + x1) // 2, (y0 + y1) // 2))
    return out


def is_multi_select(pre: dict) -> bool:
    """这道题是「多选」还是「单选」—— 看 lang_headers 里的措辞。

    ★ 这是本次修复的核心发现。腾讯在 `json_payload.lang_headers` 里
    用**复数词汇标记**区分两种点选：

        zh-cn = "选择$最$符合描述的图片"      → 单选（选 1 格）
        zh-cn = "选择$%所有%$符合描述的图片"  → **多选**（选所有符合的格）

    （`$...$` / `$%...%$` 是前端的高亮标记，不是正则。）

    实测规律（2026-10-08，21 轮完整流程）：

      · **第一题永远是单选**（`$最$`），提交最高分那一格必返 `ec=51`
      · **51 之后的续做题永远是多选**（`$%所有%$`），而且正解**格数不固定**
      · 多选按「固定选前 k 格」提交**必错**（少一格就错），必须按分数断层切

    兜底：`lang_headers` 缺失时按英文 `en` 判断，再不行就当单选。
    """
    dyn = (pre.get("data") or {}).get("dyn_show_info") or {}
    payload = dyn.get("json_payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload or "{}")
        except (ValueError, TypeError):
            payload = {}
    if not isinstance(payload, dict):
        payload = {}

    headers = payload.get("lang_headers") or []
    texts: list[str] = []
    if isinstance(headers, list):
        for h in headers:
            if isinstance(h, dict) and h.get("text"):
                texts.append(str(h["text"]))
    blob = " ".join(texts)
    if not blob:
        return False

    # 中文「所有」是主判据；其它语言用「all / toutes / tutte / todos …」
    if "所有" in blob or "全部" in blob:
        return True
    low = blob.lower()
    for kw in ("select all", "all images", "all the images", "toutes", "todos",
               "tutte", "alle ", "все ", "semua", "ทั้งหมด", "すべて", "모두"):
        if kw in low or kw in blob:
            return True
    return False


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
    """单选：把「选中的那一格」变成可提交的四件套。"""
    return _prepare_submission_multi(solver, ch, [pick_index])


def _prepare_submission_multi(solver, ch: dict, pick_indices: Sequence[int]) -> dict:
    """把「选中的若干格」变成可提交的四件套：点击坐标、格子编号、collect+eks、pow。

    保证**轨迹与答案自洽**：collect 喂的就是真正点的那几格 ——
    多选题就是点了 N 下（`_click_events` 会按顺序生成 N 组按下-抬起-点击），
    绝不把「看过的 6 格」当成「点过的 6 格」（那是很容易被风控识破的作弊特征）。
    """
    regions = ch["regions"]
    tile_ids = ch["tile_ids"]
    idxs = [int(i) for i in pick_indices]
    if not idxs:
        raise ClickSolveError("没有选中任何格子")
    for i in idxs:
        if not 0 <= i < len(regions):
            raise ClickSolveError(f"格子编号越界：{i}（共 {len(regions)} 格）")

    centers = _region_centers(regions)
    pts = [centers[i] for i in idxs]
    ids = [tile_ids[i] if i < len(tile_ids) else i + 1 for i in idxs]

    tdc_path = str(ch["ccfg"].get("tdc_path", ""))
    m = re.search(r"(?:\?|&)app_data=([^&]+)&t=(\d+)", tdc_path)
    if not m:
        raise ClickSolveError("tdc_path 里没有 app_data")
    tdc_source = solver._tdc_download(m.group(1), m.group(2))

    gen = solver._get_collect(tdc_source, _click_events(pts), None)
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
        "pts": pts,
        "ids": ids,
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
    hybrid_attempts: int = 8,
    min_separation: float = 3.0,
    max_unrecognized: int = 10,
    deadline_s: float = 160.0,
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
        遇到 `ec=9`（`verifyFailRefresh`）时**开新会话换题**的重试次数。
        ec=9 的语义是「这道题没过，换一张重来」；源码里它走 `refresh()`
        （**不带 sess**），所以这里也开全新会话。每换一次就是另一道题，
        必须重新识别。
    @param hybrid_attempts
        遇到 `ec=30/51`（`verifyHybrid`）时**带 sess 续做**的次数。

        ★ 这是点选最容易做错的一环：51 曾被当成「答案不对」直接放弃，
        于是每次都在第一题就退出。真实链路是服务端给了**新 sess**，
        要求客户端带着它重新 prehandle 在同一流程里续做（见 `EC_HYBRID`
        上方那段源码注释）。实测点选**第一题必返 51**，
        所以不给足这个预算就等于永远过不去。

        ★★ 而 51 之后的**续做题是多选**（`选择$%所有%$`），
        必须按分数断层切分后**整簇提交** —— 见 `is_multi_select` 与
        `click_recognizer.otsu_split`。这条才是真正能拿到 ticket 的关键：
        实测多选用断层切分 4 次拿到 `ec=0`（带 ticket），
        而固定「选前 k 格」连续 15 次全错。
    @param min_separation
        多选题的置信度下限（`otsu_split` 返回的类间方差）。低于它就认为
        「这题没认出来」，宁可 ec=9 换一道题，也不盲交浪费机会。

        阈值有实测依据（12 组真实分数，见 `click_recognizer.otsu_split`）：
        能过的题 separation 最低 **5.56**，而认不出的（「包含文字：X」）只有
        **1.23** —— 取 3.0 正好卡在中间，不误杀成功样本，又能拦住瞎猜。
        注意「扫雪车」那次分离度高达 55.83 仍然失败，那是**选多了一格**
        （最大间隔切法给了 k=5），Otsu 已修正为 k=4 —— 所以分离度只是
        「有没有认清」的判据，切分方法本身才是修正 k 的关键。
    @param max_unrecognized
        「断崖太小、这张认不出」时**在同一流程内重摇**的次数上限，默认 8。

        ★ 为什么单独给一份预算，而不是并进 `refresh_attempts`：
        实测失败的那两轮都是「续做预算被换题吃光」—— 认不出的多选要换题，
        而换题后新会话的第一题**必返 51 又要占一次续做预算**，两头抢同一份额度。
        所以这类「没认出来」必须有自己的额度，否则认不出的题会把
        「能认出的题」的机会挤掉。
    @param deadline_s
        **墙钟时间预算**（秒），默认 160。

        ★ 为什么需要它：修好「一遇挫就放弃」之后，重试预算从 5 涨到 8，
        实测出现了一轮跑满 **165.9s** 的情况（连续撞上认不出的题）。
        而插件侧单次超时默认 180s —— 再差一点就会变成「过码超时」，
        **明明还能试却被上层掐断**，比提前收工更糟。

        所以这里自己收口：超过预算就带着**当前已有的结果**正常返回
        （最后一轮的 errorCode 会透出去，用户看到的是「点选未通过」
        而不是「过码超时」，日志里也能看出是时间不够而中止）。

        ⚠️ 这个值**跟着插件侧超时走**：`server.py` 读 `QQ_SLIDER_TIMEOUT_MS`
        算出「插件超时 − 20s」。**别在这里写死** —— 两边硬编码一定会漂移。
    @returns 与滑块一致的 result dict（含 ok/ticket/errorCode/...）
    """
    if probe_formats is None:
        probe_formats = (os.environ.get("QQ_SLIDER_CLICK_PROBE") or "").strip() in ("1", "true", "yes")
    started = time.perf_counter()

    # ── ① 首轮 prehandle（不带 sess = 开一道全新题）──────────────────
    #
    # ★ 这里必须和循环内一样**带退避重试**。旧代码裸调 `solver.prehandle()`，
    #   一上来吃 403 就整个抛出去 —— 用户看到的就是「这一次直接失败」，
    #   连一次重试都没有（实测「一半一半」的另一种形态）。
    #   请求失败时返回 None，交给下面的循环去重试，而不是在这里抛。
    pre: dict | None = None
    for attempt in range(4):
        try:
            pre = solver.prehandle()
            break
        except Exception as err:  # noqa: BLE001
            transient = ("403" in str(err) or "429" in str(err)
                         or "Timeout" in type(err).__name__)
            LOG.warning("点选：首轮 prehandle 异常（第 %d 次）%s: %s",
                        attempt + 1, type(err).__name__, str(err)[:120])
            if not transient or attempt == 3:
                break
            time.sleep(3.0 * (attempt + 1))

    refresh_attempts = max(1, int(refresh_attempts))
    hybrid_attempts = max(1, int(hybrid_attempts))
    max_unrecognized = max(1, int(max_unrecognized))

    result: dict[str, Any] = {}
    used_format = ""
    instruction = ""
    last_picks: list[int] = []
    last_is_multi = False
    last_cands = None
    last_margin = None
    refresh_seen = 0
    hybrid_seen = 0
    skipped_seen = 0
    # 上一张「认不出」的图 URL：用来检测「留 sess 到底有没有换题」
    # （用 URL 而不是题面 —— 题面会重复，见下面判断处的注释）
    unrecognized_img = ""
    rounds = 0
    format_idx = 0
    # ★ 续做会话：51/30 之后由服务端下发，带它重新 prehandle 就在同一流程里；
    #   9 之后清空（源码里 refresh() 不带 sess，是开新题）。
    sess_hint = ""
    # 首轮已经取好的题，别浪费（少一次请求 = 少一分风控）
    pending_pre: dict | None = pre

    # 预算：正常模式 = 续做次数 + 换题次数 + 「认不出」重摇次数；
    # 「认不出」单独一份额度，否则认不出的题会挤掉能认出的题的机会
    # （实测：换题后新会话第一题必返 51，又占一次续做预算，两头抢同一份）。
    # 探格式模式 = 每个格式一次。
    max_rounds = (len(ANS_FORMATS) if probe_formats
                  else (hybrid_attempts + refresh_attempts + max_unrecognized))

    while rounds < max_rounds:
        rounds += 1

        # ★ 墙钟预算：超了就带着已有结果收工，绝不让上层按超时掐断
        #   （实测有一轮跑满 165.9s，插件侧 180s 超时线已经很近了）。
        if deadline_s and (time.perf_counter() - started) > float(deadline_s):
            LOG.warning("点选：已用 %.1fs 超过预算 %.0fs，带着当前结果收工（第 %d 轮）",
                        time.perf_counter() - started, float(deadline_s), rounds - 1)
            break

        # 本轮用哪种 ans 拼法：探格式模式逐个试，正常模式恒用源码确认的那种。
        if probe_formats:
            if format_idx >= len(ANS_FORMATS):
                break
            name, build = ANS_FORMATS[format_idx]
            format_idx += 1
        else:
            name, build = ANS_FORMATS[0]

        # ── 取题 ──────────────────────────────────────────────────────
        # 探格式模式每轮都开新会话（要对照不同 ans 拼法，会话必须干净）；
        # 正常模式带 sess_hint —— 51 之后就是靠它续做。
        if pending_pre is not None:
            pre = pending_pre
            pending_pre = None
        else:
            use_sess = "" if probe_formats else sess_hint
            # 说明：这里先用旧 sess 试一次，拿到题后再比对指纹（见下面）。
            # 「先用 sess 试」是有意的 —— 实测同一 sess 再取通常**会**换题，
            # 这样只要 1 次请求就换到新题；一旦发现没换（同指纹），
            # 下一轮才退回「开全新会话」。代价是「恰好没换」时多花 1 次请求，
            # 换来的收益是正常路径省下一次「第一题必返 51」。
            # ★ 403 / 网络抖动要**退避重试**，不能直接放弃整次过码 ——
            #   腾讯对同一 IP 的高频 prehandle 会返 403，而这是**暂时**的
            #   （隔几秒就好）。实测连跑测试时 403 会成片出现，
            #   若直接 break，一次登录就白白失败。
            pre = None
            for attempt in range(4):
                try:
                    pre = solver.prehandle(sess=use_sess)
                    break
                except Exception as err:  # noqa: BLE001
                    transient = "403" in str(err) or "429" in str(err) or "Timeout" in type(err).__name__
                    LOG.warning("点选：prehandle 异常（第 %d 次）%s: %s",
                                attempt + 1, type(err).__name__, str(err)[:120])
                    if not transient or attempt == 3:
                        break
                    # 403 是腾讯对**同 IP 高频请求**的临时拒绝，退避要够长才有意义
                    time.sleep(3.0 * (attempt + 1))

            if pre is None:
                # ★★ 这里**绝不能 break**（2026-10-09 用户实测「一半一半」的根因）。
                #
                # 旧写法是 `pre is None → break`，于是：
                #   第 1 轮答对拿 51 → 第 2 轮带 sess 取续做题时吃 403/超时
                #   → 直接放弃整次过码，而 `result` 里还留着第 1 轮的 51
                #   → 用户看到「混合验证续做 1 次，errorCode=51」，
                #     明明还有一大截预算，换题逻辑**一次都没机会跑**。
                #
                # 正解：续做取不到题就退回「开新会话」，新会话也取不到就
                # 隔几秒再试 —— 由 `max_rounds` 兜底，而不是一遇挫就收工。
                if sess_hint:
                    LOG.info("点选：带 sess 取续做题失败 → 退回开新会话重试（第 %d 轮）", rounds)
                    sess_hint = ""
                else:
                    LOG.info("点选：取题失败，稍后重试（第 %d 轮）", rounds)
                time.sleep(2.5)
                continue

        # ★ prehandle 也可能**直接给 ticket**（源码：`if (i.ticket) ...onSuccess(...)`）——
        #   带 sess 续做时这条路会走通，所以先看有没有。
        if pre.get("ticket"):
            result = {
                "errorCode": EC_SUCCESS,
                "ticket": pre.get("ticket"),
                "randstr": pre.get("randstr") or "",
            }
            used_format = name
            LOG.info("点选：prehandle 直接给了 ticket（第 %d 轮，%s）", rounds, name)
            break

        if pre.get("state") != 1:
            # state=217 是**限频**。同样不能直接放弃 ——
            #   换成全新会话再试（限频是按 IP + 频次算的，隔几秒会自己恢复）。
            LOG.warning("点选：prehandle state=%r（第 %d 轮，sess=%s）",
                        pre.get("state"), rounds, "续做" if sess_hint else "新会话")
            if sess_hint:
                sess_hint = ""
                time.sleep(2.0)
                continue
            # 新会话也拿不到题：多半是限频，等久一点再试一次
            time.sleep(4.0)
            continue

        ch = _parse_challenge(pre)
        if not isinstance(ch["sess"], str) or not ch["sess"]:
            LOG.warning("点选（%s）：prehandle 没给 sess", name)
            sess_hint = ""
            continue
        if not ch["instruction"] or not ch["regions"]:
            LOG.warning("点选（%s）：题面或格子缺失", name)
            sess_hint = ""
            continue
        if not isinstance(ch["img_url"], str) or not ch["img_url"]:
            LOG.warning("点选（%s）：背景图 URL 缺失", name)
            sess_hint = ""
            continue

        # ★ 上一轮「认不出、只留 sess 没提交」时，检查到底换没换题。
        #   服务端对同一 sess 的行为没有稳定保证（实测样本不足，403 打断），
        #   所以这里主动兜底：**图还是同一张**就认为没换题，
        #   清空 sess 重开全新会话（实测必然换题，代价是多一次 51）。
        #
        # ⚠️ 两个都必须注意：
        #   ① 判据用图**不能**用题面 —— 题面会重复（实测「湖边」一次跑里出现 4 次），
        #      拿题面比对会把「换了题但恰好同题面」误判成没换，白花一次 51。
        #   ② 比图也**不能比整个 URL** —— `img_url` 里带着 `&sess=...`，
        #      同一张图换 sess 后字符串就不同了，比对永远不相等、兜底等于没有。
        #      （这个 bug 是 `test_click_retry.py` 用例 ⑥ 抓出来的：
        #      连着 20 次都拿同一张图，却一直用旧 sess 重试、把预算烧光。）
        #      所以只取 `image=` 那个哈希来比。
        if unrecognized_img and _img_fingerprint(ch["img_url"]) == unrecognized_img:
            LOG.info("点选：留 sess 后图还是同一张（%s…）→ 没换题，改为开全新会话",
                     unrecognized_img[:40])
            sess_hint = ""
            unrecognized_img = ""
            continue

        # 这道题是单选还是多选 —— 由 lang_headers 的措辞决定。
        #   第一题恒为单选（`选择$最$`），51 之后的续做题恒为多选（`选择$%所有%$`）。
        multi = is_multi_select(pre)

        if rounds == 1:
            LOG.info("点选：题面=%r 格子=%d 题型=%s",
                     ch["instruction"], len(ch["regions"]), "多选" if multi else "单选")
        elif sess_hint:
            LOG.debug("点选续做（第 %d 轮）：新题面=%r 题型=%s",
                      rounds, ch["instruction"], "多选" if multi else "单选")

        # 识别（每轮都做 —— 换题就是换图，旧索引全废）
        from urllib.parse import urljoin

        img_bytes = solver._download_bytes(urljoin(solver.endpoints.captcha_base_url, ch["img_url"]))
        rec: dict
        try:
            if multi:
                # 多选：全 6 格打分 + Otsu 断层切出高分簇（**不能**做 CV 预筛，
                # 预筛会按「最离群」砍掉 3 格，正解有 4 格时就丢了一格）
                rec = recognizer.recognize_multi(img_bytes, ch["instruction"], ch["regions"])
                if float(rec.get("separation") or 0) < float(min_separation):
                    # 断层不明显 ⇒ 这题没认出来（典型：包含文字：X）。
                    # 盲交只会浪费这个会话，所以**不提交**，换一道题。
                    #
                    # 换题有两种做法，这里先留 sess 再「看起来没换」时退回清空：
                    #   · 留 sess（省一次 51，但**依赖「同一 sess 反复 prehandle
                    #     会换题」这个尚未证实的假设**）—— 若假设不成立，
                    #     下一轮会拿到**完全相同**的题面（`unrecognized_instr` 判据）
                    #   · 清空 sess → 全新会话，**必然**是新题（实测几百次都成立），
                    #     代价是多一次「第一题必返 51」的开销
                    # 下面的 `unrecognized_instr` 检查会自动在这两者间切换。
                    skipped_seen += 1
                    unrecognized_img = _img_fingerprint(ch["img_url"])
                    LOG.info("点选多选：断层太小（%.2f < %.1f），本张认不出，"
                             "同一流程内换一道（第 %d 次，题面=%r）",
                             float(rec.get("separation") or 0), min_separation,
                             skipped_seen, ch["instruction"])
                    continue
            else:
                rec = recognizer.recognize(img_bytes, ch["instruction"], ch["regions"])
        except Exception as err:  # noqa: BLE001
            if rounds == 1 and not multi:
                raise ClickNotSupported(f"点选识别失败：{type(err).__name__}: {err}") from err
            LOG.warning("点选（%s）：识别失败 %s", name, err)
            sess_hint = ""
            continue

        instruction = ch["instruction"]
        last_is_multi = multi
        if multi:
            pick_indices = [int(i) for i in rec["pick_indices"]]
            picked_index = pick_indices[0]
            LOG.info("点选识别（多选）：选 %d 格 %s（断层=%.2f，全部分数 %s，%.2fs）",
                     len(pick_indices), [i + 1 for i in pick_indices],
                     float(rec.get("separation") or 0),
                     [round(float(v), 1) for v in rec.get("scores") or []],
                     rec.get("seconds", 0))
            last_picks = [i + 1 for i in pick_indices]
            last_cands = [i + 1 for i in pick_indices]
            last_margin = rec.get("separation")
        else:
            pick_indices = [int(rec["pick_index"])]
            picked_index = pick_indices[0]
            LOG.info("点选识别（单选）：选第 %d 格（候选 %s，margin=%.1f，%.2fs）",
                     picked_index + 1, rec.get("candidates"),
                     rec.get("margin") or 0, rec.get("seconds", 0))
            last_picks = [picked_index + 1]
            last_cands = rec.get("candidates")
            last_margin = rec.get("margin")

        try:
            sub = _prepare_submission_multi(solver, ch, pick_indices)
        except ClickSolveError as err:
            LOG.warning("点选（%s）：准备提交失败 %s", name, err)
            sess_hint = ""
            continue

        ans_json = build(sub["pts"], sub["ids"])
        LOG.debug("点选提交格式=%s ans=%s", name, ans_json)
        # 这张认出来了、真的提交了 → 清掉「认不出的图」标记
        unrecognized_img = ""
        try:
            result = _submit(solver, ch["sess"], sub["collect"], sub["eks"],
                             ans_json, sub["pow_answer"], sub["pow_ms"])
        except Exception as err:  # noqa: BLE001
            LOG.warning("点选提交（%s）异常 %s: %s", name, type(err).__name__, err)
            sess_hint = ""
            continue

        ec = str(result.get("errorCode", ""))
        used_format = name

        if ec == EC_SUCCESS and result.get("ticket"):
            LOG.info("点选成功（格式 %s，第 %d 轮）", name, rounds)
            break

        if ec in EC_HYBRID:
            # ★ 混合验证：服务端给了新 sess，带着它重新 prehandle 续做。
            #   这才是 51 的正解 —— 不是「答案不对」，更不该放弃。
            new_sess = result.get("sess") or ""
            hybrid_seen += 1
            if not new_sess:
                LOG.info("点选 ec=%s（verifyHybrid）但没给新 sess → 只能换新会话", ec)
                sess_hint = ""
                continue
            LOG.info("点选 ec=%s（verifyHybrid），带服务端给的新 sess 续做（第 %d 次）",
                     ec, hybrid_seen)
            sess_hint = str(new_sess)
            continue

        if ec == EC_REFRESH:
            # verifyFailRefresh：这道题没过 → 源码走 refresh()，不带 sess，开新题。
            refresh_seen += 1
            LOG.info("点选 ec=9（verifyFailRefresh），开新会话换一张题（第 %d 次）", refresh_seen)
            sess_hint = ""
            continue

        if ec in EC_WRONG_ANSWER:
            # 50 = verifyFail，答案真的不对 → 换题再试（可能是识别错了）
            refresh_seen += 1
            LOG.info("点选 ec=%s（答案不对），换一张题重试（第 %d 次）", ec, refresh_seen)
            sess_hint = ""
            continue

        # 12（风控）/ 20 / 52 / 206（会话超时）换题无益，收工。
        LOG.warning("点选（%s）返回 ec=%s，不再重试", name, ec)
        break

    seconds = round(time.perf_counter() - started, 2)
    ec = str(result.get("errorCode", ""))
    ok = ec == EC_SUCCESS and bool(result.get("ticket"))

    # 失败原因要能一眼看懂：只报 `errorCode=9` 谁也判断不出是「认错了」还是
    # 「环境被风控」。所以把**识别结果 + 重试次数 + 格式**一起写进 error，
    # 日志里另有完整行（题面、候选、margin）。
    #
    # ⚠️ 文案只描述**做了什么**，不给服务端错误码下「答案不对」这种断言 ——
    # 之前 51 被写成「点选答案不对」，实际它是 verifyHybrid（要带 sess 续做），
    # 一句错文案把排查方向带偏了很久。
    if ok:
        err_text = ""
    else:
        # 多选时报「选了几格 + 是哪几格」，单选时报「选第几格」——
        # 单选写成「选 3 格」会让人以为连点了三次，排查时反而误导。
        if last_is_multi:
            what = f"选 {len(last_picks)} 格 {last_picks}"
        elif last_picks:
            what = f"选第 {last_picks[0]} 格"
        else:
            what = "未选出格子"
        if ec in EC_HYBRID:
            err_text = f"点选未通过（{what}，混合验证续做 {hybrid_seen} 次，errorCode={ec}）"
        elif ec == EC_REFRESH:
            err_text = f"点选未通过（{what}，换题重试 {refresh_seen} 次仍失败）"
        elif ec in EC_WRONG_ANSWER:
            err_text = f"点选答案不对（{what}，errorCode={ec}）"
        elif ec == "12":
            err_text = f"点选被风控拦截（errorCode={ec}）"
        elif ec:
            err_text = f"点选失败（{what}，errorCode={ec}）"
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
        "pick": last_picks[0] if last_picks else 0,   # 兼容旧字段（单选时的格号）
        "picks": last_picks,                          # 真正提交的格子编号列表
        "multi": last_is_multi,
        "candidates": last_cands,
        "margin": last_margin,
        "ans_format": used_format,
        "refresh_retries": refresh_seen,
        "hybrid_retries": hybrid_seen,
        "unrecognized_retries": skipped_seen,
        "rounds": rounds,
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
