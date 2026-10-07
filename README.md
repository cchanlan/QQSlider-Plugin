# QQSlider-Plugin

**QQ 滑块自动过码** —— 云崽登录遇到滑块验证时自动通过，不用再手点。

纯 HTTP 协议实现，**零浏览器、零人工**，单次约 2~3 秒。

## 它解决什么

用 icqq 登录 QQ 时，腾讯会弹一个滑块验证（`system.login.slider`）。
原版 ICQQ-Plugin 会让你在 QQ 里手选验证方式、再手动拖滑块或者去第三方站点过码。

装上本插件后：**遇到滑块自动过，你什么都不用做。**

## 一键部署

```bash
# 1. 进云崽的插件目录
cd Yunzai/plugins

# 2. 拉插件
git clone <本仓库地址> QQSlider-Plugin

# 3. 重启云崽（或发 #重启）
```

**完事。** 剩下的插件自己会做：

- 首次加载自动创建 Python 虚拟环境、装依赖（后台进行，约 1~3 分钟，**不阻塞云崽启动**）
- 依赖装好后自动拉起过码服务
- 之后登录遇到滑块自动过码

> 需要机器上有 **Python 3.9+** 和 **Node.js**（云崽本来就要 Node）。
> 没装 Python 的话，发 `#滑块过码安装` 会提示你。

## 指令

| 指令 | 作用 |
|---|---|
| `#滑块过码状态` | 看接管状态、服务是否在跑、成功率统计 |
| `#滑块过码安装` | 手动装依赖并启动服务 |
| `#滑块过码测试` | 真解一次验证码，确认环境可用 |
| `#滑块过码开启` / `#滑块过码关闭` | 开关自动过码 |

## 配置

配置在 `config/QQSlider.yaml`，首次加载自动生成：

```yaml
enable: true          # 总开关
mode: auto            # auto=自动过码；manual=只发验证链接，自己手动过
host: 127.0.0.1       # 过码服务监听地址
port: 8767            # 过码服务端口
rounds: 3             # 每次最多试几轮
timeout: 180000       # 单次超时（毫秒）
autoInstall: true     # 首次加载自动装 Python 依赖
```

`mode: manual` 适合你想自己过码、只是想省掉「选验证方式」那一步的场景。

## 它是怎么接进去的

**不修改任何第三方插件。**

只监听 `system.login.slider` 事件（跟 ICQQ-Plugin 监听同一个），
拿到验证 URL 后交给本地过码服务换 ticket，
再通过云崽核心自带的 `verify.<QQ>` 通道把 ticket 交回给 ICQQ-Plugin 提交。

`verify.<QQ>` 是云崽核心（`plugins/system/botOperate.js` 的 `#Bot验证` 指令）
与适配器之间的既有约定，不是某个插件私有的钩子 —— 所以 ICQQ-Plugin 更新也不会失效。

## 它是怎么过码的

| 步骤 | 做什么 |
|---|---|
| ① prehandle | 拿 `sess`、图片 URL、pow 配置、`tdc_path` |
| ② tdc.js | 下载腾讯官方脚本，交给 Node 在 `vm` 沙箱里跑 |
| ③ 图片 | 还原乱序背景 → OpenCV 多尺度模板匹配出缺口 |
| ④ 轨迹 | 拟人拖拽（贝塞尔 + 抖动 + 过冲回拉） |
| ⑤ 参数 | 把轨迹喂给 `tdc.js`，拿到 `collect` + `eks` |
| ⑥ pow | 解工作量证明 |
| ⑦ verify | 提交，拿 ticket |

**没有逆向 jsvmp，也没有浏览器** —— `collect`/`eks` 是腾讯自己的 `tdc.js`
在 Node 的 `vm` 里算出来的，腾讯改算法也不用跟着改。

## 目录结构

```
QQSlider-Plugin/
├── index.js              # 插件入口、指令
├── utils/
│   ├── hook.js           # 滑块事件接管（与 ICQQ-Plugin 的唯一接缝）
│   ├── service.js        # 服务生命周期（找 Python/建 venv/装依赖/起停）
│   └── solver.js         # 过码服务客户端
└── service/              # Python 过码服务
    ├── server.py         # HTTP 服务
    ├── slide_solver.py   # 过码引擎
    ├── tdc_server.cjs    # Node worker（跑腾讯 tdc.js）
    └── requirements.txt
```

## 环境要求

- **Python 3.9+**（Windows 上命令是 `python`，Linux 上是 `python3`，插件会自动探测）
- **Node.js**（云崽本来就要）
- Python 依赖：`curl-cffi`、`requests`、`numpy`、`opencv-python`、`Pillow`（插件自动装）

依赖默认走清华源（国内直连 PyPI 经常超时）。要换源：

```bash
# Linux / macOS
export QQSLIDER_PIP_INDEX=https://pypi.org/simple
# Windows
set QQSLIDER_PIP_INDEX=https://pypi.org/simple
```

## 常见问题

**Q：提示「服务端下发的是 click 题型」？**
QQ 登录的题型由服务端按 `uin` + `cap_cd` 决定。这个提示说明当前这次验证不是滑块
（可能是点选）。本插件只解滑块 —— QQ 登录验证正常就是滑块，遇到点选请手动过一次。

**Q：服务起不来？**
发 `#滑块过码状态` 看 Python 有没有找到、依赖装没装上。
手动装：`#滑块过码安装`。

**Q：会不会拖慢云崽启动？**
不会。装依赖和服务启动都在后台进行，不阻塞启动。过码只在登录遇到滑块时才用到。

**Q：能解极验（米游社那种）吗？**
不能。腾讯 TCaptcha 和极验是两套完全不同的协议。

## 来源

过码引擎移植自 [adnogpu/TXCaptcha](https://github.com/adnogpu/TXCaptcha)，
做了三处改造：端点参数化（腾讯云 / QQ 两套可切）、关闭上游的「降热」机制
（它会在连续成功 3 次后故意全错，服务化时是功能损坏）、常驻复用
（单次从 7 秒压到 2~3 秒）。

## License

MIT
