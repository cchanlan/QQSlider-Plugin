# QQ 滑块过码服务（Python 端）

这是 [QQSlider-Plugin](../README.md) 的 Python 过码服务本体。

**正常使用不需要手动跑这个** —— 插件会自动建 venv、装依赖、拉起服务。
本文档是给排障和单独调试用的。

## 手动跑（排障用）

```bash
# 建环境
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt      # Windows
# .venv/bin/python -m pip install -r requirements.txt            # Linux

# 起服务
.venv\Scripts\python.exe server.py            # 默认 127.0.0.1:8767
```

## 接口

### `POST /solve`

```json
{ "url": "https://ssl.captcha.qq.com/template/wireless_mqq_captcha.html?uin=123&cap_cd=xxx&sid=yyy" }
```

也可以不传 `url`，直接给参数：`{ "uin": "123", "cap_cd": "xxx", "aid": "16" }`。

返回：

```json
{
  "ok": true,
  "ticket": "tr03...",
  "randstr": "@7Y9",
  "errorCode": "0",
  "seconds": 2.14,
  "kind": "slide",
  "solver": "local"
}
```

失败时 `ok=false`，`error` 里是原因。**`kind` 说明服务端下发的题型**：
`slide` 滑块 / `click` 点选 / `icon` 图标点选 / `other`。
**`slide` 和 `click` 都会走本地引擎自动解**（点选需要模型，见下）。

点选成功时 payload 还会多几个字段：

```json
{ "ok": true, "kind": "click", "instruction": "百香果",
  "multi": false, "pick": 2, "picks": [2], "candidates": [2, 6, 4],
  "margin": 10.6, "solver": "local-clip" }
```

- **`multi`** —— 这道题是多选还是单选（由 `lang_headers` 判定，见下文）
- **`picks`** —— 真正提交的格子编号列表（多选会有多个）
- **`pick`** —— 兼容旧字段，取 `picks[0]`
- **`margin`** —— 单选时是 CLIP 前两名分差；**多选时是 Otsu 的类间方差**
  （≥3.0 才算「认出来了」，低于这个值会当作没认出、换题重试）

> icqq 的 `submitSlider(ticket)` 内部会 `String(ticket).trim()`，
> **只要纯 ticket、不要拼 randstr**。

### `GET /health`

```json
{ "status": "ok", "ok": 12, "fail": 1, "successRate": "92%", "avgSeconds": 2.4,
  "env": {
    "python": "3.12.0", "node": "/usr/bin/node",
    "deps": { "cv2": true, "numpy": true, "curl_cffi": true, "requests": true },
    "tdcServer": "tdc_server.cjs",
    "click": { "modelReady": true, "modelMB": 182.4, "loaded": false,
               "modelDir": ".../service/.models/Xenova__chinese-clip-vit-base-patch16" }
  } }
```

`env.click` 就是点选能力的自检结果（插件 `#滑块过码状态` 直接读它）。

## 过码链路

### 滑块（`slide`）

| 步骤 | 做什么 |
|---|---|
| ① prehandle | 拿 `sess`、图片 URL、pow 配置、`tdc_path` |
| ② tdc.js | 下载腾讯官方脚本，交给 Node worker 在 `vm` 沙箱里跑 |
| ③ 图片 | 还原乱序背景 → OpenCV 多尺度模板匹配出缺口坐标 |
| ④ 轨迹 | 拟人拖拽事件（贝塞尔 + 抖动 + 过冲回拉） |
| ⑤ 参数 | 把轨迹喂给 `tdc.js`，拿到 `collect` + `eks` |
| ⑥ pow | 解工作量证明 |
| ⑦ verify | 提交，拿 ticket |

### 点选（`click`）

| 步骤 | 做什么 |
|---|---|
| ① prehandle | 拿 `instruction`（题面）、`select_region_list`（6 格坐标）、图片 |
| ② 预筛 | 按图像特征排序，取最「离群」的 3 格（`click_recognizer.rank_tiles`）|
| ③ 识别 | CLIP 给候选格与题面打匹配分，取最高（`recognize`）|
| ④ 轨迹 | 每格生成 mousedown→mouseup→click 事件 |
| ⑤ 参数 | 把点击事件喂给 `tdc.js`（它原生支持 `clicks` 参数）|
| ⑥ pow | 解工作量证明 |
| ⑦ verify | 提交 `ans`（`DynAnswerType_UC`，**内容是区域编号**），拿 ticket |

**为什么必须用模型**：协议里只有题面，**没有任何答案字段**（实测 60 张真题）：

    instruction = "百香果"
    select_region_list = [{"id":1,"range":[0,34,220,254]}, ...]   # 纯坐标
    prompt_id = 166767      # 与题面一一对应，但每次图和正解位置都变
    img_url                 # 随机哈希

`prompt_id` 虽然稳定对应题面，但**同一 id 两次采样，正解分别在 #6 和 #3**
—— 所以「按题面缓存答案」行不通，只能每次现场认图。

**没有逆向 jsvmp，也没有浏览器** —— `collect`/`eks` 是腾讯自己的 `tdc.js`
在 Node 的 `vm` 里算出来的，腾讯改算法也不用跟着改。

## ✅ 点选提交格式（已从腾讯源码确认 + 实测对照）

**`ans` 的正确形态是「一条记录、`elem_id` 恒为 1、`data` 是区域编号的逗号串」**：

```json
[{"elem_id":1,"type":"DynAnswerType_UC","data":"3"}]
```

单选就是一个编号（`"3"`），多选是逗号串（`"2,5"`）。**不是坐标**。

### 依据一：腾讯前端源码（决定性）

QQ 登录的点选由 `t.captcha.qq.com/template/drag_ele.html` 加载
**`https://captcha.gtimg.com/1/dy-ele.d10b59c0.js`**（QQ 域名这份，
跟 turing 域名的 `dy-ele.bf9c9389.js` 不是同一个文件）。
其中 `SelectEl.prototype.addData` 原文：

```js
if ("DynAnswerType_UC" === l) {
  m.push(h.id)                      // h.id = select_region_list 里的区域编号
  emit("setData", { namespace: "selectEl",
    data: [{ elem_id: 1, type: "DynAnswerType_UC", data: m.join(",") }] })
}
```

对照：同文件里 `DynAnswerType_POS` / `_POS_L`（`ClickEl`，即 dy-ele 那套老点选）
才提交坐标，而**服务端给点选下发的是 `data_type: ["DynAnswerType_UC"]`**
（实测 `t.captcha.qq.com` 的 prehandle 确认），走的正是上面这支。

⚠️ **踩过的坑**：turing 域名那两份 dy-ele（`dy-ele.js` / `dy-ele.bf9c9389.js`）
里**根本没有 `DynAnswerType_UC`** —— 只看它们会以为「UC 不存在」，
进而去猜坐标格式。**必须看 QQ 域名那份**。

### 依据二：实测对照（4 种格式 × 真实 prehandle 会话）

| 格式 | `ans` 的 data | errorCode |
|---|---|---|
| **`uc_region_ids`（正确）** | `"3"`（区域编号） | **`51` verifyHybrid** |
| `uc_join_semicolon` | `"336,144"`（坐标分号） | `9` |
| `uc_multi_record` | 一条一点、elem_id 递增 | `9` |
| `uc_join_comma` | `"336,144"`（坐标逗号） | `9` |

**`uc_region_ids` 的返回码与另外三种不同**（51 vs 9），说明服务端真的解析了 `ans`
并接受了这个格式。**最终用真 ticket 验证通过**（见下一节）。

> 这条实测同时**推翻了旧结论**：以前试的 4 种格式全返 `ec=9`，
> 于是记录里写成「假 cap_cd 的会话在解析 ans 之前就被拒，无法区分格式」。
> 真相是**那 4 种全都不对**，正确的格式从没被试过。

## ★ 单选 / 多选两种点选（2026-10-08 打通全流程）

`json_payload.lang_headers` 里的措辞决定了这道题要选几格 —— 这是整条链路的关键：

| 语言头 | 题型 | 出没规律 | 正确做法 |
|---|---|---|---|
| `选择$最$符合描述的图片` | 单选 | **第一题恒为单选** | 取最高分 1 格 → 返 `ec=51` |
| `选择$%所有%$符合描述的图片` | **多选** | **51 之后的续做题恒为多选** | 全 6 格打分 → **Otsu 断层切簇** → 整簇提交 → 返 `ec=0` + ticket |

（`$...$` / `$%...%$` 是前端高亮标记，不是正则。）

### `ec=51` 不是「答案对了」

实测 9/9 第一题都给 51，**连 CLIP margin 只有 7.8 的那次也是** ——
一个 88% 准确率的模型不可能 9/9 全对。所以 51 的语义是
**「第一题答完，进入混合验证阶段」的流程信号**。

51 的正确处理是：**拿响应里的新 sess 重新 prehandle，在同一流程里续做**
（源码链路 `verifyHybrid → Notify.hybridVerify(resp.sess)` →
`tcaptcha-frame` `case 8: onHybridVerify(sess)` → `Topics.HybridVerify` →
`startPreHandle(sess)` → `getPreHandleNew({sess})`）。
不是换新会话，更不是放弃。

### 多选为什么必须按「断层」切

多选的**正解格数不固定**（实测 2、3、4 格都有），所以「固定选前 k 格」
**必然错** —— 连续 15 次失败全是**少选了一格**。真正的信号是分数断层：

```
湖边   42.4 32.9 42.6 36.5 43.4 35.5   → 高分簇 {5,3,1}
海角   36.9 40.4 33.1 41.2 37.3 35.9   → 高分簇 {4,2}
消防车  31.3 44.8 30.6 48.5 46.6 45.9   → 高分簇 {4,5,6,2}
```

切分用 **1-D Otsu**（类间方差最大），不是「相邻两格最大差值」——
后者会被末尾那个离群低分格抢走最大间隔：「扫雪车」
`48.2 47.3 46.7 45.0 36.2 25.7` 最大间隔切 k=5（错），Otsu 切 k=4（对）。

Otsu 返回的**类间方差同时是置信度**：能过的题最低 **5.56**，
而认不出的「包含文字：X」只有 **1.23** —— 代码里取 `min_separation=3.0`，
低于它就当作「没认出来」，宁可 ec=9 换题也不盲交。

### 实测结果

```
solve_click() 端到端 4 轮：3 轮拿到真 ticket
  第 1 轮 ok ec=0  题面='“栏杆”'  多选 提交格=[2,3]     ticket=t03tservereJf-vBe...
  第 2 轮    ec=51（续做预算用完）
  第 3 轮 ok ec=0  题面='“气球”'  多选 提交格=[5,1]     ticket=t03tserverXjvioGa7...
  第 4 轮 ok ec=0  题面='“海岸”'  多选 提交格=[4,5,3]   ticket=t03tserverEeLQYZBR...
```

成功响应体形如：

```json
{"errorCode":"0","randstr":"@fb8","ticket":"t03tserver3nU7txlz_TFf3bWyhqCZx8xB...","errMessage":"","sess":""}
```

### ⚠️ 提交一次就会推进题目

实测：**提交一次之后，题目就变了**（再 prehandle 拿到的是另一道题）。
所以「同一道题反复提交试不同答案组合」是**无效实验** ——
第二次提交用的是旧题的格子和新 sess，服务端看到一份自相矛盾的答卷，必然 ec=9。
**这个坑直接导致了之前「多选怎么试都错」的错误结论。**

### errorCode 语义（扒自 `dy-ele` 的 verify 分发表）

| 码 | 名字 | 含义 |
|---|---|---|
| `0` | `verifySuccess` | **成功，带 ticket** |
| `9` | `verifyFailRefresh` | **验证失败、换一道题重来**（不是「会话无效」）|
| `12` | `verifyError` | 风控 / 环境异常（同 IP 高频连续过码，会自己恢复）|
| `20` | `verifySessionTimeout` | 会话超时 |
| `30` | `verifyHybrid` | **混合验证：带响应里的新 sess 续做** |
| `50` | `verifyFail` | 答案真的不对 |
| `51` | `verifyHybrid` | **同 30**（点选第一题恒返这个）|
| `52` | `verifyError` | 错误 |
| `206` | `verifySessionTimeout` | 会话超时 |

⚠️ 两个曾经踩过的坑：

- **`9` 不是「会话无效」** —— 它是「换题重来」。代码遇到 ec=9 会换会话重试
  （`QQ_SLIDER_CLICK_REFRESH`，默认 3 次），每换一次都是新题、必须重新识别。
- **`51` / `30` 不是「答案不对」** —— 旧表把 51 写成「混合验证（答案不对那类）」，
  代码于是把它和 50 一起当「答案不对」直接放弃，还打出一句「点选答案不对」的
  误导文案（用户看到的就是这句）。真实语义是「带 sess 续做」。

### 调试开关

- **`QQ_SLIDER_CLICK_PROBE=1`** —— 依次试全部 4 种格式并把结果记进日志。
  只在「怀疑腾讯改版了」时用；常规路径**默认只用 `uc_region_ids`**。
- **`QQ_SLIDER_CLICK_REFRESH=<n>`** —— ec=9 时的换题重试次数，默认 3。
- **`QQ_SLIDER_CLICK_HYBRID=<n>`** —— ec=51/30 时的带 sess 续做次数，默认 5。
  **别调小**：点选第一题必返 51，续做那一轮才是多选、才可能拿到 ticket，
  小于 2 就等于永远过不去。
- **`QQ_SLIDER_CLICK_REROLL=<n>`** —— 「断崖太小、这张认不出」时的重摇次数，默认 8。
  这类题（典型「包含文字：X」）实测约占多选的 1/3，单独一份额度，
  不会挤掉能认出的题的机会。
- **`QQ_SLIDER_CLICK_TOPK=<n>`** —— 单选时 CV 预筛保留几格，默认 3
  （多选**不用**预筛，必须全 6 格打分）。


## 三个实现要点（改代码前先看）

### Node worker 必须叫 `.cjs`

`tdc_server.cjs` 是 **CommonJS**（用 `require`）。插件目录的 `package.json`
带 `"type": "module"`，那样 `.js` 会被 Node 当 ESM 解析，
一启动就 `ReferenceError: require is not defined in ES module scope`。
`.cjs` 后缀不受 `package.json` 影响 —— **别改回 `.js`**。

⚠️ 这个坑的症状很误导：Python 侧只看到 `TDC worker exited unexpectedly`
（stderr 被 `subprocess.DEVNULL` 吞了），完全看不出是 ESM 问题。

### 降热逻辑必须关

上游 TXCaptcha 有个「连续成功 3 次后故意全错」的降热机制（`errorCode=50`），
那是给同一 IP 压测用的。**当服务用必须关**，否则第 4 次起永远拿不到 ticket。
代码里是 `SlideSolverConfig.heat_control`，默认 `False`。

实测：开着时 2.59s ✅ → 2.39s ✅ → 2.39s ✅ → **2.39s ❌ ec=50**；
关掉后 6/6 全过，均值 2.28s。

### 常驻 solver + 复用 worker + 启动预热

- 每次新建 solver = Node 进程重启 + 会话预热 ≈ **5~7 秒**
- 复用常驻实例 ≈ **2.1~2.8 秒**
- **启动预热**（`warmup()`）把两个端点的 worker 和会话都提前拉热，
  这样**第一次**过码也是 2 秒级 —— 登录是偶发操作，很可能一次登录只过一次码，
  那次就是「第一次」

`tdc_server.cjs` 每个请求都 `vm.createContext` 建新上下文，**进程本身跨请求无状态**，
所以复用安全。按 `(端点, aid)` 各缓存一个 solver：只缓存「最后一个」的话，
QQ 与腾讯云交替请求会互相踢掉（实测 QQ 请求 4.5s vs 命中缓存 2.17s）。

## 端点差异

| | 腾讯云 | QQ |
|---|---|---|
| prehandle | `turing.captcha.qcloud.com` | **`t.captcha.qq.com`** |
| `aid` | `199999861` | 空（靠 `uin`/`cap_cd`） |

⚠️ `ssl.captcha.qq.com/cap_union_prehandle` **恒返 407**（stgw 网关直接拒），
真正吃请求的是 `t.captcha.qq.com`（实测 200，返回完整 `tdc_path`）。
`TCapIframeApi.js` 源码里也写着 `var f="https://t.captcha.qq.com"`，跟实测一致。

**QQ 那套的 `aid` 是故意为空的**，题型靠 `uin` + `cap_cd` 决定。
代码里用 `Endpoints.prehandle_style` 分 `cloud` / `qq` 两套参数。

## 关于题型

QQ 登录的题型由**服务端按 `uin` + `cap_cd` 下发**。静态探测（不带真实 `cap_cd`）
一律只得到「点选」—— 扫过 24 种参数组合 + 11 个 aid，全是 `DynAnswerType_UC`。

所以：**必须用 icqq 登录时实际弹出的那个 URL**（里面的 `cap_cd` 才是真的）。
拿假 `cap_cd` 打接口会得到 `kind=click`，这是正常的，不是服务坏了。

## 文件

| 文件 | 说明 |
|---|---|
| `server.py` | HTTP 服务（`/solve`、`/health`、启动预热） |
| `slide_solver.py` | 过码引擎（缺口识别 + 轨迹 + 协议） |
| `tdc_server.cjs` | Node worker，在 `vm` 沙箱里跑腾讯 `tdc.js` |
| `requirements.txt` | Python 依赖 |

## 环境要求

- Python ≥ 3.9（用了 `from __future__ import annotations`）
- Node.js（跑 `tdc_server.cjs`；命令名默认 `node`，可用 `SlideSolverConfig.node_command` 改）
- 依赖：`curl-cffi`、`requests`、`numpy`、`opencv-python`、`Pillow`

## 来源

过码引擎移植自 [adnogpu/TXCaptcha](https://github.com/adnogpu/TXCaptcha)，
做了三处改造：端点参数化、关闭降热、常驻复用 + 启动预热。
