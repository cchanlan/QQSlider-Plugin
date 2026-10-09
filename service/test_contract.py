"""点选 solver 与 server 的**接口契约**自测。

## 这个测试是为哪次事故写的

`fe6bb5c` 那版 `click_solver` 返回的失败结构是 `{"error": "..."}`，
而 `server.solve_once` 读的是 `result.get("errMessage")` —— **两边字段名不一致**，
于是点选的详细原因（「选第 4 格」这类）被**静默丢掉**，
用户只看到 JS 兜底的一句 `errorCode=9`，把「轨迹与答案不一致」
误判成「会话无效」，白排查了很久。

这类 bug 的特点是：**不抛异常、不报错、只是信息消失**。
所以必须有测试钉住字段名，不能靠人记得。

## 跑法

    cd service
    .venv/Scripts/python.exe test_contract.py     # Windows
    .venv/bin/python test_contract.py             # Linux

不需要网络、不需要模型、不需要腾讯 —— 纯逻辑自测。
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        failures.append(name)


# ══════════════════════════════════════════════════════════════════
print("=" * 78)
print("① 失败原因必须能透出来（★静默丢信息事故）")
print("=" * 78)

import click_solver  # noqa: E402
import click_recognizer  # noqa: E402
import server as srv_mod  # noqa: E402

# ①-a 行为测试：直接调真正的拼装函数，而不是 grep 源码字符串 ——
#     字符串检查会被同文件里别的 `result.get("error")`（比如日志行）
#     误判成「通过」，变异测试 M1 就是这么抓出上一版测试不够狠的。
click_fail = {
    "errorCode": "9",
    "ticket": "",
    "error": "点选未通过（选第 4 格，换题重试 3 次仍失败）",
    "instruction": "冰淇淋",
    "pick": 4,
    "multi": False,
    "solver": "local-clip",
}
pl = srv_mod._build_payload(click_fail, uin="2393395657", kind="click", seconds=5.12)
check("点选的 error 键没被丢掉",
      "选第 4 格" in pl["error"], f"实得 {pl['error']!r}")
check("失败原因不是裸 errorCode",
      pl["error"] != "errorCode=9", f"实得 {pl['error']!r}")
check("ok 为 False", pl["ok"] is False)
check("errorCode 透传", pl["errorCode"] == "9")

# ①-b errMessage 优先于 error（两个都在时取更规范的）
both = {"errorCode": "9", "errMessage": "规范的说明", "error": "老写法"}
check("errMessage 优先", srv_mod._build_payload(both)["error"] == "规范的说明")

# ①-c 只有 errorMessage 时也能读到
check("errorMessage 兜底",
      srv_mod._build_payload({"errorCode": "9", "errorMessage": "x"})["error"] == "x")

# ①-d 三个都没有 → 才退化成裸 errorCode
check("全都没有才退化",
      srv_mod._build_payload({"errorCode": "9"})["error"] == "errorCode=9")

# ①-e 成功时不带 error
check("成功时 error 为空",
      srv_mod._build_payload({"errorCode": "0", "ticket": "t03x"})["error"] == "")

# ①-f 诊断字段必须透传（否则排查时看不到识别结果）
check("诊断字段透传 instruction", pl.get("instruction") == "冰淇淋")
check("诊断字段透传 picks/multi", "multi" in pl)

# ══════════════════════════════════════════════════════════════════
print()
print("=" * 78)
print("①' 两个模块的失败键名必须对得上")
print("=" * 78)
csrc = (HERE / "click_solver.py").read_text(encoding="utf-8")
check("click_solver 返回里带 errMessage（server 首选读它）", '"errMessage"' in csrc)
check("click_solver 返回里带 error（继承兼容）", '"error"' in csrc)

# ══════════════════════════════════════════════════════════════════
print()
print("=" * 78)
print("② errorCode 语义常量")
print("=" * 78)
check("EC_SUCCESS == '0'", click_solver.EC_SUCCESS == "0")
check("51 在 EC_HYBRID 里", "51" in click_solver.EC_HYBRID)
check("30 在 EC_HYBRID 里", "30" in click_solver.EC_HYBRID)
check("51 不在 EC_WRONG_ANSWER 里（★历史 bug）",
      "51" not in click_solver.EC_WRONG_ANSWER)
check("EC_REFRESH == '9'", click_solver.EC_REFRESH == "9")

# ══════════════════════════════════════════════════════════════════
print()
print("=" * 78)
print("③ 单选/多选判定（lang_headers）")
print("=" * 78)

import json  # noqa: E402


def mk(zh: str) -> dict:
    return {"data": {"dyn_show_info": {
        "json_payload": json.dumps({"lang_headers": [{"lang": "zh-cn", "text": zh}]})}}}


CASES = [
    ("选择$%所有%$符合描述的图片", True, "中文多选"),
    ("选择$最$符合描述的图片", False, "中文单选"),
    ("Select all images that match", True, "英文多选"),
    ("Select the image that best matches", False, "英文单选"),
]
for text, want, desc in CASES:
    got = click_solver.is_multi_select(mk(text))
    check(f"{desc}: {text[:22]!r}", got == want, f"→ {got}")

check("空 dict 兜底为单选", click_solver.is_multi_select({}) is False)
check("坏 JSON 兜底为单选",
      click_solver.is_multi_select(
          {"data": {"dyn_show_info": {"json_payload": "{{{"}}}) is False)

# ══════════════════════════════════════════════════════════════════
print()
print("=" * 78)
print("④ Otsu 断层切分（真实分数）")
print("=" * 78)
OTSU = [
    ("湖边",   [42.4, 32.9, 42.6, 36.5, 43.4, 35.5], 3),
    ("海角",   [36.9, 40.4, 33.1, 41.2, 37.3, 35.9], 2),
    ("消防车", [31.3, 44.8, 30.6, 48.5, 46.6, 45.9], 4),
    ("气球",   [31.2, 43.2, 44.7, 31.2, 32.7, 39.6], 3),
    # ★ 关键样本：最大间隔切法给 k=5（错），Otsu 给 k=4（对）
    ("扫雪车", [48.2, 47.3, 46.7, 45.0, 36.2, 25.7], 4),
]
for name, sc, want in OTSU:
    idx, bcv, k = click_recognizer.otsu_split(sc)
    check(f"{name} k={k}（期望 {want}）sep={bcv:.2f}", k == want)

# ══════════════════════════════════════════════════════════════════
print()
print("=" * 78)
print("⑤ 提交轨迹与答案必须自洽（★历史 bug）")
print("=" * 78)
# fe6bb5c 的 bug：ans 用 1 格，collect 却喂了全部 6 格。
# 现在 _prepare_submission_multi 只喂真正选中的格子。
import inspect  # noqa: E402

psm = inspect.getsource(click_solver._prepare_submission_multi)
check("_prepare_submission_multi 用 _click_events(pts)",
      "_click_events(pts)" in psm)
check("_prepare_submission_multi 不再喂 centers（全部格子）",
      "_click_events(centers)" not in psm)
check("_prepare_submission 委托给 multi 版本",
      "_prepare_submission_multi(solver, ch, [pick_index])"
      in inspect.getsource(click_solver._prepare_submission))

# ══════════════════════════════════════════════════════════════════
print()
print("=" * 78)
print("⑥ ans 格式：源码确认的那一种必须排第一")
print("=" * 78)
first_name, first_build = click_solver.ANS_FORMATS[0]
check("ANS_FORMATS[0] 是 uc_region_ids", first_name == "uc_region_ids",
      f"实得 {first_name}")

# 单选：1 个坐标 ↔ 1 个编号
one = json.loads(first_build([(100, 200)], [4]))
check("单选 → 只有一条记录", len(one) == 1)
check("单选 → data='4'", one[0]["data"] == "4", f"{one[0]['data']!r}")
check("elem_id 恒为 1", one[0]["elem_id"] == 1)
check("type 是 DynAnswerType_UC", one[0]["type"] == "DynAnswerType_UC")

# 多选：N 个坐标 ↔ N 个编号，逗号串
multi = json.loads(first_build([(100, 200), (300, 400), (500, 600)], [4, 5, 6]))
check("多选 → 仍然只有一条记录", len(multi) == 1)
check("多选按 ids 拼逗号串", multi[0]["data"] == "4,5,6", f"{multi[0]['data']!r}")
check("多选 elem_id 也是 1", multi[0]["elem_id"] == 1)

# ★ 轨迹条数必须等于编号个数（fe6bb5c 的 bug 是 6 对 1）
check("坐标个数与编号个数一致（单选）", len(one) == 1)
check("坐标个数与编号个数一致（多选）", multi[0]["data"].count(",") + 1 == 3)

# ══════════════════════════════════════════════════════════════════
print()
print("=" * 78)
print("全部通过 ✅" if not failures else f"★ {len(failures)} 项失败：{failures}")
print("=" * 78)
sys.exit(1 if failures else 0)
