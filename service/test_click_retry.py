"""点选重试逻辑自测（mock solver，不联网、不碰腾讯）。

## 这个测试是为哪次事故写的

用户实测「一半一半：第一次过，二三次没过，第四次过」，失败文案是
**「点选未通过（选第 4 格，混合验证续做 1 次，errorCode=51）」**。

`续做 1 次` 是铁证 —— 预算明明有十几次，却只续做了 1 次就收工。
根因：第 1 轮答对拿 51 后，第 2 轮要 `prehandle(sess)` 取续做题，
**取题一旦失败（403 / state=217）旧代码直接 `break` 整个放弃**，
而 `result` 里还留着第 1 轮的 51 → 用户看到的正是那句文案。
**换题逻辑一次都没机会跑**（用户问「不是会自动换题吗」—— 对，但轮不到它）。

修法：取题失败**不再 break**，而是
  · 带 sess 取不到 → 退回「开新会话」重试
  · 新会话也取不到 → 隔几秒重试
  · state≠1（限频）同样如此，由 `max_rounds` 兜底

## 覆盖的行为

  ① 续做取题抛异常（403）→ **不放弃**，继续跑并最终成功
  ② 续做 state=217（限频）→ 不放弃，退回新会话
  ③ 新会话取题也失败几次 → 仍不放弃
  ④ ec=9 → **自动换题**（开新会话、拿到新题面）
  ⑤ 一直失败 → 由 max_rounds 收口，不会死循环
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import click_recognizer  # noqa: E402
import click_solver  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        failures.append(name)


# ══════════════════════════════════════════════════════════════════
# mock 基础设施
# ══════════════════════════════════════════════════════════════════
REGIONS = [[0, 34, 220, 254], [226, 34, 446, 254], [452, 34, 672, 254],
           [0, 260, 220, 480], [226, 260, 446, 480], [452, 260, 672, 480]]

LANGS_SINGLE = [{"lang": "zh-cn", "text": "选择$最$符合描述的图片"}]
LANGS_MULTI = [{"lang": "zh-cn", "text": "选择$%所有%$符合描述的图片"}]


def make_pre(*, instruction: str, langs, img_seed: str, sess: str,
             state: int = 1, ticket: str = "") -> dict:
    payload = {
        "select_region_list": [{"id": i + 1, "range": r} for i, r in enumerate(REGIONS)],
        "prompt_id": 1000,
        "picture_ids": [1, 2, 3, 4, 5, 6],
        "lang_headers": langs,
    }
    return {
        "state": state,
        "ticket": ticket,
        "randstr": "@aaa" if ticket else "",
        "sess": sess,
        "sid": "mock-sid",
        "subcapclass": "1603",
        "data": {
            "comm_captcha_cfg": {
                "tdc_path": "/tdc.js?app_data=APP123&t=456",
                "pow_cfg": {"prefix": "ab#", "md5": "cd"},
            },
            "dyn_show_info": {
                "instruction": instruction,
                "json_payload": json.dumps(payload),
                # ⚠️ 跟真实 URL 一样：sess 是**查询参数的一部分**，
                #    同一张图换会话后整串就不同了（这正是「比整个 URL」会失效的原因）
                "bg_elem_cfg": {
                    "img_url": f"/cap_union_new_getcapbysig?img_index=1&image={img_seed}&sess={sess}"
                },
                "show_type": "click_image_uncheck",
            },
            "html": {},
        },
    }


class FakeResp:
    def __init__(self, body: dict):
        self._body = body
        self.status_code = 200
        self.text = json.dumps(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        return None


class FakeCookies:
    def set(self, *a, **k):
        return None


class FakeSession:
    def __init__(self, outer):
        self.outer = outer
        self.cookies = FakeCookies()

    def post(self, url, data=None, timeout=None, headers=None):
        return FakeResp(self.outer.on_verify(data or {}))


class FakeSolver:
    """按脚本吐 prehandle 结果，并记录每一次调用。

    `fail_when_sess=True` 时**所有带 sess 的请求都失败** —— 这是复现用户现场
    （2026-10-09：IP 被前几次失败登录打成限频，带 sess 续做恒 403）所必需的：
    真实情况下 4 次重试**全部** 403，才会走到「带 sess 取续做题失败」那个分支。
    只塞一两个 `RuntimeError` 是不够的 —— 重试第 2 次就成功了，分支根本不跑。
    """

    def __init__(self, prehandle_script, verify_script, fail_when_sess=False):
        self.prehandle_script = list(prehandle_script)
        self.verify_script = list(verify_script)
        self.fail_when_sess = fail_when_sess
        self.prehandle_calls: list[str] = []
        self.verify_calls: list[dict] = []
        self._tdc_token = None
        self.session = FakeSession(self)
        self._request_timeout = 15
        self.endpoints = type("E", (), {
            "captcha_base_url": "https://example.invalid",
            "verify_url": "https://example.invalid/verify",
            "captcha_referer": "https://example.invalid/",
        })()

    def prehandle(self, sess=""):
        self.prehandle_calls.append(sess)
        if self.fail_when_sess and sess:
            raise RuntimeError("403 Client Error: Forbidden")
        if not self.prehandle_script:
            raise RuntimeError("mock: prehandle 脚本用完了")
        step = self.prehandle_script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def on_verify(self, data):
        self.verify_calls.append(data)
        if not self.verify_script:
            return {"errorCode": "9", "ticket": "", "sess": "s-fallback"}
        step = self.verify_script.pop(0)
        return step

    def _download_bytes(self, url):
        return b"\xff\xd8\xff\xe0fake-jpeg"

    def _tdc_download(self, app_data, t):
        return "// tdc source"

    def _get_collect(self, source, events, extra):
        return {"collect": "COLLECT", "eks": "EKS", "tokenid": "tok"}

    def _pow(self, prefix, md5):
        return ("POWANS", 12)

    def _fetch_headers(self, referer=None):
        return {}


class FakeRecognizer:
    """单选返回 pick_index；多选返回 pick_indices + separation。"""

    def __init__(self, *, single_pick=3, multi_picks=(0, 1), separation=20.0):
        self.single_pick = single_pick
        self.multi_picks = list(multi_picks)
        self.separation = separation

    def recognize(self, img, instruction, regions):
        return {"ok": True, "pick_index": self.single_pick, "pick": self.single_pick + 1,
                "candidates": [self.single_pick + 1], "margin": 12.0, "seconds": 0.1}

    def recognize_multi(self, img, instruction, regions):
        return {"ok": True, "pick_indices": self.multi_picks,
                "picks": [i + 1 for i in self.multi_picks],
                "k": len(self.multi_picks), "separation": self.separation,
                "scores": [40.0] * len(REGIONS), "seconds": 0.1}


# ══════════════════════════════════════════════════════════════════
print("=" * 78)
print("① 续做取题抛异常（403）→ 绝不能放弃整次过码")
print("=" * 78)
solver = FakeSolver(
    prehandle_script=[
        make_pre(instruction="冰淇淋", langs=LANGS_SINGLE, img_seed="A", sess="S0"),
        RuntimeError("403 Client Error: Forbidden"),          # ← 续做取题炸了
        make_pre(instruction="湖边", langs=LANGS_SINGLE, img_seed="B", sess="S1"),
        make_pre(instruction="消防车", langs=LANGS_MULTI, img_seed="C", sess="S2"),
    ],
    verify_script=[
        {"errorCode": "51", "ticket": "", "sess": "S1"},       # 第 1 题答对 → 续做
        {"errorCode": "51", "ticket": "", "sess": "S2"},       # 换新会话后第 1 题又 51
        {"errorCode": "0", "ticket": "t03tserverMOCK", "randstr": "@zzz"},
    ],
)
res = click_solver.solve_click(solver, recognizer=FakeRecognizer(), hybrid_attempts=8,
                               refresh_attempts=3, max_unrecognized=10)
check("取题 403 后仍然跑到了成功", res["ok"] is True, f"errorCode={res['errorCode']!r}")
check("拿到了 ticket", res["ticket"] == "t03tserverMOCK")
check("没有因为 403 就收工", res["rounds"] >= 3, f"rounds={res['rounds']}")
print(f"        prehandle 调用序列 = {solver.prehandle_calls}")

# ══════════════════════════════════════════════════════════════════
print()
print("=" * 78)
print("② 续做 state=217（限频）→ 退回新会话，不放弃")
print("=" * 78)
solver = FakeSolver(
    prehandle_script=[
        make_pre(instruction="冰淇淋", langs=LANGS_SINGLE, img_seed="A", sess="S0"),
        make_pre(instruction="", langs=LANGS_SINGLE, img_seed="B", sess="S1", state=217),
        make_pre(instruction="湖边", langs=LANGS_SINGLE, img_seed="C", sess="S2"),
        make_pre(instruction="气球", langs=LANGS_MULTI, img_seed="D", sess="S3"),
    ],
    verify_script=[
        {"errorCode": "51", "ticket": "", "sess": "S1"},
        {"errorCode": "51", "ticket": "", "sess": "S3"},
        {"errorCode": "0", "ticket": "t03tserverMOCK2", "randstr": "@zzz"},
    ],
)
res = click_solver.solve_click(solver, recognizer=FakeRecognizer(), hybrid_attempts=8,
                               refresh_attempts=3, max_unrecognized=10)
check("限频后仍然跑到了成功", res["ok"] is True, f"errorCode={res['errorCode']!r}")
check("拿到了 ticket", res["ticket"] == "t03tserverMOCK2")
print(f"        prehandle 调用序列 = {solver.prehandle_calls}")

# ══════════════════════════════════════════════════════════════════
print()
print("=" * 78)
print("③ ec=9 → 自动换题（且新题面确实变了）")
print("=" * 78)
solver = FakeSolver(
    prehandle_script=[
        make_pre(instruction="冰淇淋", langs=LANGS_SINGLE, img_seed="A", sess="S0"),
        make_pre(instruction="湖边", langs=LANGS_SINGLE, img_seed="B", sess="S1"),
        make_pre(instruction="海角", langs=LANGS_SINGLE, img_seed="C", sess="S2"),
        make_pre(instruction="栏杆", langs=LANGS_MULTI, img_seed="D", sess="S3"),
    ],
    verify_script=[
        {"errorCode": "51", "ticket": "", "sess": "S1"},   # 第 1 题对
        {"errorCode": "9", "ticket": "", "sess": "S2"},    # 多选答错 → 换题
        {"errorCode": "51", "ticket": "", "sess": "S3"},   # 新会话第 1 题又对
        {"errorCode": "0", "ticket": "t03tserverMOCK3", "randstr": "@zzz"},
    ],
)
rec = FakeRecognizer()
res = click_solver.solve_click(solver, recognizer=rec, hybrid_attempts=8,
                               refresh_attempts=3, max_unrecognized=10)
check("答错后自动换题并最终成功", res["ok"] is True, f"errorCode={res['errorCode']!r}")
check("确实重试过（换题计数 ≥1）", res["refresh_retries"] >= 1,
      f"refresh_retries={res['refresh_retries']}")
check("最后提交的是多选", res["multi"] is True)
print(f"        prehandle 调用序列 = {solver.prehandle_calls}")
print(f"        （能看到第 2 次 prehandle 没带 sess = 换新会话）")

# ══════════════════════════════════════════════════════════════════
print()
print("=" * 78)
print("④ 一直失败 → 由 max_rounds 收口，不会死循环")
print("=" * 78)
solver = FakeSolver(
    prehandle_script=[RuntimeError("403")] * 60,
    verify_script=[],
)
res = click_solver.solve_click(solver, recognizer=FakeRecognizer(), hybrid_attempts=2,
                               refresh_attempts=2, max_unrecognized=2)
check("取题全失败时能收口返回", isinstance(res, dict) and res["ok"] is False)
check("轮数受 max_rounds 约束（6）", res["rounds"] <= 6, f"rounds={res['rounds']}")
check("prehandle 不会无限重试", len(solver.prehandle_calls) <= 60,
      f"调了 {len(solver.prehandle_calls)} 次")

# ══════════════════════════════════════════════════════════════════
print()
print("=" * 78)
print("⑤ 多选断层太小 → 不盲交，换题")
print("=" * 78)
solver = FakeSolver(
    prehandle_script=[
        make_pre(instruction="冰淇淋", langs=LANGS_SINGLE, img_seed="A", sess="S0"),
        make_pre(instruction='包含文字："川"', langs=LANGS_MULTI, img_seed="B", sess="S1"),
        make_pre(instruction='包含文字："久"', langs=LANGS_MULTI, img_seed="C", sess="S2"),
        make_pre(instruction="气球", langs=LANGS_MULTI, img_seed="D", sess="S3"),
    ],
    verify_script=[
        {"errorCode": "51", "ticket": "", "sess": "S1"},   # 第 1 题对
        {"errorCode": "0", "ticket": "t03tserverMOCK4", "randstr": "@zzz"},
    ],
)
# 前两次断层极小（认不出），第三次正常
class FlakyRec(FakeRecognizer):
    def __init__(self):
        super().__init__()
        self.seps = [1.0, 1.2, 30.0]
    def recognize_multi(self, img, instruction, regions):
        r = super().recognize_multi(img, instruction, regions)
        r["separation"] = self.seps.pop(0) if self.seps else 30.0
        return r

res = click_solver.solve_click(solver, recognizer=FlakyRec(), hybrid_attempts=8,
                               refresh_attempts=3, max_unrecognized=10,
                               min_separation=3.0)
check("认不出的题被跳过、最终答对", res["ok"] is True, f"errorCode={res['errorCode']!r}")
check("记录了跳过次数", res["unrecognized_retries"] >= 1,
      f"unrecognized_retries={res['unrecognized_retries']}")
check("认不出的那两次没有提交", len(solver.verify_calls) == 2,
      f"实际提交 {len(solver.verify_calls)} 次（应为 2）")

# ══════════════════════════════════════════════════════════════════
print()
print("=" * 78)
print("⑥ 续做题换题后图没变 → 改开全新会话（unrecognized_img 兜底）")
print("=" * 78)
same_seed = "SAME"
solver = FakeSolver(
    prehandle_script=[
        make_pre(instruction="冰淇淋", langs=LANGS_SINGLE, img_seed="A", sess="S0"),
        make_pre(instruction='包含文字："川"', langs=LANGS_MULTI, img_seed=same_seed, sess="S1"),
        # 留 sess 再取，拿到**同一张图** → 应判定没换题、清 sess
        make_pre(instruction='包含文字："川"', langs=LANGS_MULTI, img_seed=same_seed, sess="S2"),
        make_pre(instruction="气球", langs=LANGS_SINGLE, img_seed="C", sess="S3"),
        make_pre(instruction="栏杆", langs=LANGS_MULTI, img_seed="D", sess="S4"),
    ],
    verify_script=[
        {"errorCode": "51", "ticket": "", "sess": "S1"},
        {"errorCode": "51", "ticket": "", "sess": "S4"},
        {"errorCode": "0", "ticket": "t03tserverMOCK5", "randstr": "@zzz"},
    ],
)
res = click_solver.solve_click(solver, recognizer=FakeRecognizer(separation=1.0),
                               hybrid_attempts=8, refresh_attempts=3,
                               max_unrecognized=10, min_separation=3.0)
print(f"        prehandle 调用序列 = {solver.prehandle_calls}")
# 序列含义（索引 → 带什么 sess）：
#   0: ''   首轮，开全新题
#   1: 'S1' 拿 51 给的 sess 取续做题 → 拿到「同一张图 SAME」，断层太小 → 记下指纹
#   2: 'S1' 还带旧 sess 再取 → **又拿到同一张图** → 判定「没换题」→ 清 sess
#   3: ''   已经清空 → 这次才是真正的「开新会话」
check("检测到同图后，下一次取题改用新会话（sess 已清空）",
      len(solver.prehandle_calls) >= 4 and solver.prehandle_calls[3] == "",
      f"第 4 次带的 sess={solver.prehandle_calls[3] if len(solver.prehandle_calls) > 3 else 'N/A'!r}（应为空）")
check("前两次确实还在用旧 sess（说明检测要花一次请求）",
      solver.prehandle_calls[1] == "S1" and solver.prehandle_calls[2] == "S1",
      f"{solver.prehandle_calls[1:3]}")
check("最终换到新图并跑完", res["ok"] is True or res["rounds"] >= 4,
      f"ok={res['ok']} rounds={res['rounds']}")

# ══════════════════════════════════════════════════════════════════
print()
print("=" * 78)
print("⑦ 墙钟预算：超时了要带着结果收工，不能被上层掐断")
print("=" * 78)
import time as _time  # noqa: E402

solver = FakeSolver(
    prehandle_script=[
        make_pre(instruction="冰淇淋", langs=LANGS_SINGLE, img_seed="A", sess="S0"),
        make_pre(instruction='包含文字："川"', langs=LANGS_MULTI, img_seed="B1", sess="S1"),
    ] + [
        make_pre(instruction='包含文字："川"', langs=LANGS_MULTI,
                 img_seed=f"B{i}", sess=f"S{i}") for i in range(2, 40)
    ],
    verify_script=[{"errorCode": "51", "ticket": "", "sess": "S1"}],
)
# 每次取题都拖一会儿，好让墙钟预算生效
_orig_pre = solver.prehandle


def _slow_prehandle(sess=""):
    _time.sleep(0.25)
    return _orig_pre(sess)


solver.prehandle = _slow_prehandle
t0 = _time.time()
res = click_solver.solve_click(solver, recognizer=FakeRecognizer(separation=1.0),
                               hybrid_attempts=30, refresh_attempts=5,
                               max_unrecognized=30, min_separation=3.0,
                               deadline_s=2.0)
cost = _time.time() - t0
check("到点就收工，没有跑满 max_rounds(65)", res["rounds"] < 30, f"rounds={res['rounds']}")
check("耗时贴近预算而不是无限跑", cost < 8, f"实际 {cost:.1f}s（预算 2s）")
check("返回了正常结果结构（不是异常）", isinstance(res, dict) and "errorCode" in res,
      f"errorCode={res.get('errorCode')!r}")
check("失败文案仍是人话", bool(res.get("error"))
      and not str(res["error"]).startswith("errorCode="),
      f"{res.get('error')!r}")

# ══════════════════════════════════════════════════════════════════
print()
print("=" * 78)
print("⑧ ★ 死循环：带 sess 取题「稳定」失败时要止损（用户 2026-10-09 现场）")
print("=" * 78)
# 现场日志（本机、uin=0、IP 被前几次失败登录打成限频）：
#   单选→51→带 sess 取题 403→退回新会话→又是单选→51→又 403 …
#   连续 8 轮、烧掉 160 秒预算才被墙钟拦住。
# 「带 sess 必定 403」是**稳定**的，重试再多次也没用 → 必须止损。
solver = FakeSolver(
    prehandle_script=[
        # 首轮：成功（不带 sess）
        make_pre(instruction="冰淇淋", langs=LANGS_SINGLE, img_seed="A", sess="S0"),
        # 之后**所有不带 sess 的**都成功（换题正常），带 sess 的一律 403
        make_pre(instruction="湖边", langs=LANGS_SINGLE, img_seed="B", sess="S2"),
        make_pre(instruction="海角", langs=LANGS_SINGLE, img_seed="C", sess="S4"),
        make_pre(instruction="栏杆", langs=LANGS_SINGLE, img_seed="D", sess="S5"),
        make_pre(instruction="气球", langs=LANGS_MULTI, img_seed="E", sess="S6"),
        make_pre(instruction="火车", langs=LANGS_MULTI, img_seed="F", sess="S7"),
    ],
    verify_script=[
        {"errorCode": "51", "ticket": "", "sess": "S1"},   # 第1题对 → 想续做（必失败）
        {"errorCode": "51", "ticket": "", "sess": "S3"},   # 换新会话又答对 → 再想续做（又失败）
        {"errorCode": "51", "ticket": "", "sess": "S5"},   # 第3次51 → 应**直接换题**（skip_hybrid）
        {"errorCode": "0", "ticket": "t03tserverMOCK8", "randstr": "@zzz"},
    ],
    fail_when_sess=True,     # ★ 复现用户现场：带 sess 的请求全 403
)
t0 = _time.time()
res = click_solver.solve_click(solver, recognizer=FakeRecognizer(separation=30.0),
                               hybrid_attempts=8, refresh_attempts=3,
                               max_unrecognized=10, deadline_s=160.0)
cost = _time.time() - t0
print(f"        prehandle 调用序列 = {solver.prehandle_calls}")
print(f"        耗时 {cost:.2f}s  rounds={res['rounds']}  续做={res['hybrid_retries']}")
check("最终仍然成功（止损后靠换题拿到 ticket）", res["ok"] is True,
      f"errorCode={res['errorCode']!r}")
check("★ 续做被判定不可用（不再死循环）", res.get("hybrid_disabled") is True,
      f"hybrid_disabled={res.get('hybrid_disabled')}")
# ★★ 止损的**核心语义**：判定不可用之后，**不再反复尝试带 sess 的取题**。
#
# 为什么不能只断言「耗时 < N 秒」：实测「有止损」41s、「没止损」82s ——
# 两边都远小于 160s 上限，用绝对阈值根本分不开（变异测试 D1 就是这么漏掉的）。
# 真正的判据是**带 sess 的请求次数**：没止损时会一直重试（16 次），
# 有止损时只在前两轮试满退避（8 次）。所以这里卡「次数」而不是「时间」。
sess_calls = sum(1 for c in solver.prehandle_calls if c)
check("★ 带 sess 的取题次数被压住（不是反复撞 403）", sess_calls <= 10,
      f"带 sess 调用 {sess_calls} 次（无止损时会到 16 次）")
check("★ 也没把 160 秒预算烧光", cost < 90, f"实际 {cost:.1f}s")
check("轮数正常（不是跑满 21 轮）", res["rounds"] <= 12, f"rounds={res['rounds']}")
check("止损只在连续失败 2 次后触发（不是一失败就放弃）",
      solver.prehandle_calls.count("S1") >= 4,
      f"第一次续做重试了 {solver.prehandle_calls.count('S1')} 次（应≥4，即试满退避）")

# ══════════════════════════════════════════════════════════════════
print()
print("=" * 78)
print("⑨ 403 的响应体必须留证（否则现场查不出原因）")
print("=" * 78)
# ★ 行为测试：造一个真会抛 403 的 HTTP 层，看错误串里有没有响应体。
#   只 grep 源码字符串是不够的（变异 D3 证明：把拼装语句改成 `"" or (...)` 
#   字符串仍在、但**运行时 detail 是空的**）。
from slide_solver import SlideSolver as _SS, RetryableSolveError as _RSE  # noqa: E402

class _FakeResp403:
    status_code = 403
    content = b'{"message":"rate limited","code":-508}'
    headers = {"Server": "Trpc httpd", "Content-Type": "application/json"}

class _FakeHTTP:
    """最小 requests 替身：get 抛一个带 response 的 HTTPError。"""
    def get(self, url, **kw):
        import requests as _rq
        err = _rq.HTTPError("403 Client Error: Forbidden")
        err.response = _FakeResp403()
        raise err

_solver = _SS.__new__(_SS)          # 不走 __init__（要网络/随机）
_solver._closed = False
_solver.session = _FakeHTTP()
_solver._sanitize_cookies = lambda: None
# `_request_timeout` 是只读 property，内部读 `self.config` —— 得给个替身
_solver.config = type("C", (), {"connect_timeout": 5, "read_timeout": 15})()

try:
    _solver._get("https://example.invalid/x")
    check("_get 在 403 时应该抛异常", False)
except _RSE as e:
    msg = str(e)
    check("★ 错误串里带上了响应体内容", "rate limited" in msg,
          f"{msg[-120:]!r}")
    check("★ 带上了状态码", "status=403" in msg, f"{msg[-120:]!r}")
    check("★ 带上了关键响应头", "Trpc httpd" in msg or "Server" in msg)
except Exception as e:  # noqa: BLE001
    check("_get 抛的是 RetryableSolveError", False, f"实得 {type(e).__name__}")

click_src = (HERE / "click_solver.py").read_text(encoding="utf-8")
check("prehandle 异常日志会剥掉超长 URL（留证不截断）",
      '" [status="' in click_src and "short = " in click_src)
check("有 sess 续做失败计数", "sess_fail_streak" in click_src)
check("有跳过续做的开关", "skip_hybrid" in click_src)

# ══════════════════════════════════════════════════════════════════
print()
print("=" * 78)
print("全部通过 ✅" if not failures else f"★ {len(failures)} 项失败：{failures}")
print("=" * 78)
sys.exit(1 if failures else 0)
