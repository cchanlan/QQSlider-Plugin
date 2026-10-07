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
`slide` 滑块 / `click` 点选 / `icon` 图标点选 / `other`。本引擎只解 `slide`。

> icqq 的 `submitSlider(ticket)` 内部会 `String(ticket).trim()`，
> **只要纯 ticket、不要拼 randstr**。

### `GET /health`

```json
{ "status": "ok", "ok": 12, "fail": 1, "successRate": "92%", "avgSeconds": 2.4 }
```

## 过码链路

| 步骤 | 做什么 |
|---|---|
| ① prehandle | 拿 `sess`、图片 URL、pow 配置、`tdc_path` |
| ② tdc.js | 下载腾讯官方脚本，交给 Node worker 在 `vm` 沙箱里跑 |
| ③ 图片 | 还原乱序背景 → OpenCV 多尺度模板匹配出缺口坐标 |
| ④ 轨迹 | 拟人拖拽事件（贝塞尔 + 抖动 + 过冲回拉） |
| ⑤ 参数 | 把轨迹喂给 `tdc.js`，拿到 `collect` + `eks` |
| ⑥ pow | 解工作量证明 |
| ⑦ verify | 提交，拿 ticket |

**没有逆向 jsvmp，也没有浏览器** —— `collect`/`eks` 是腾讯自己的 `tdc.js`
在 Node 的 `vm` 里算出来的，腾讯改算法也不用跟着改。

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
