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
| ⑦ verify | 提交 `ans`（`DynAnswerType_UC`），拿 ticket |

**为什么必须用模型**：协议里只有题面，**没有任何答案字段**（实测 60 张真题）：

    instruction = "百香果"
    select_region_list = [{"id":1,"range":[0,34,220,254]}, ...]   # 纯坐标
    prompt_id = 166767      # 与题面一一对应，但每次图和正解位置都变
    img_url                 # 随机哈希

`prompt_id` 虽然稳定对应题面，但**同一 id 两次采样，正解分别在 #6 和 #3**
—— 所以「按题面缓存答案」行不通，只能每次现场认图。

**没有逆向 jsvmp，也没有浏览器** —— `collect`/`eks` 是腾讯自己的 `tdc.js`
在 Node 的 `vm` 里算出来的，腾讯改算法也不用跟着改。

## ⚠️ 点选提交格式尚未在真实登录中验证（改代码前必读）

**识别链路已完整验证**（60 张真题 + 12/12 复测），但**提交格式还没被真实验证过**，原因：

静态探测（不带真实 `cap_cd`）拿到的会话本身就是无效的。
用它试了 **6 种截然不同的 `ans` 格式**，**全部返回 `errorCode=9`** ——
说明服务端在「解析 ans」之前就拒了。所以那个 ec=9 反映的是**会话无效**，
不是格式对错，**无法用它区分格式**。

因此 `click_solver.py` 的做法是：

- **默认只试一种**格式（`uc_join_semicolon`，与滑块的 `DynAnswerType_POS` 结构最像）：
  换格式要重建会话，实测每次 +2.5 秒，常规路径不该为猜测付这个代价
- 设 **`QQ_SLIDER_CLICK_PROBE=1`** 会依次试全部 4 种格式，并把实际命中的
  记进日志 —— **等有真实登录 URL（真 `cap_cd`）时，用它一次就能定下来**

判据：如果某个格式**格式对但答案错**，服务端会返 `ec=50` 或 `51`
（而不是 9）。所以看到 50/51 就说明格式被接受了 —— 代码里也是这么判的，
遇到 50/51 会立即停止换格式。

**已知 errorCode 语义**（实测 + 上游代码）：

| 码 | 含义 |
|---|---|
| `0` | 成功 |
| `9` | 会话无效（`cap_cd`/`sess` 不成立）|
| `12` | 热度控制（同 IP 高频连续过码，会自己恢复）|
| `50` | 答案错（降热机制触发时"故意全错"）|
| `51` | 答案错/被拒（点选实测见到）|

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
