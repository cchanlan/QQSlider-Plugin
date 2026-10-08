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
  "pick": 2, "candidates": [2, 6, 4], "margin": 10.6, "solver": "local-clip" }
```

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
并接受了这个格式 —— 51 是 `verifyHybrid`，属于「格式对、答案不对」那一类。

> 这条实测同时**推翻了旧结论**：以前试的 4 种格式全返 `ec=9`，
> 于是记录里写成「假 cap_cd 的会话在解析 ans 之前就被拒，无法区分格式」。
> 真相是**那 4 种全都不对**，正确的格式从没被试过。

### errorCode 语义（扒自 `dy-ele` 的 verify 分发表）

| 码 | 名字 | 含义 |
|---|---|---|
| `0` | `verifySuccess` | 成功 |
| `9` | `verifyFailRefresh` | **验证失败、换一张题重来**（不是「会话无效」）|
| `12` | `verifyError` | 风控 / 环境异常（同 IP 高频连续过码，会自己恢复）|
| `20` | `verifySessionTimeout` | 会话超时 |
| `50` | `verifyFail` | 答案不对 |
| `51` | `verifyHybrid` | 混合验证（答案不对那类）|
| `52` | `verifyError` | 错误 |
| `206` | `verifySessionTimeout` | 会话超时 |

⚠️ `9` 的语义尤其容易误判 —— 它是「换题重来」，所以代码里遇到 ec=9 会
**换会话重试**（`QQ_SLIDER_CLICK_REFRESH`，默认 3 次），每换一次都是新题、必须重新识别。

### 调试开关

- **`QQ_SLIDER_CLICK_PROBE=1`** —— 依次试全部 4 种格式并把结果记进日志。
  只在「怀疑腾讯改版了」时用；常规路径**默认只用 `uc_region_ids`**。
- **`QQ_SLIDER_CLICK_REFRESH=<n>`** —— ec=9 时的换题重试次数，默认 3。


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
