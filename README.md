<div align="center">

# AP 信道规划助手

**用 Windows 原生 API 读取真实的 Wi-Fi 环境，一步步算出办公室的 AP 该用哪些信道。**

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%20%2F%2011-0078D6.svg)](#环境要求)
[![Python](https://img.shields.io/badge/python-3.8%2B-3776AB.svg)](#环境要求)
[![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](#技术选型)

</div>
这个工具把「规划」变成一件有依据的事：

- **真实扫描** —— 走 Windows 原生 `wlanapi.dll`，不是示例数据，也不需要抓包网卡
- **认出同一台 AP** —— 一台 AP 的 2.4G / 5G 是两条不同的 BSSID，按尾部字节合并成一台
- **确认相邻关系** —— 由你逐台确认哪些 AP 挨得近（相邻的必须错开信道）
- **给出建议** —— 结合外部同频占用，输出信道与频宽建议，**每条结论都附带依据**

<div align="center">
  <img src="tests/screenshots/step1-scan.png" alt="第一步：扫描结果" width="880">
  <br><sub>第一步：把真实扫到的 BSS 按「本公司 / 外部」分开，并认出哪些属于同一台 AP</sub>
</div>

---


## 三步工作流

页面分成三步，每一步的结论都会流到下一步。

### 第一步 · 扫描结果

把本机扫到的所有 BSS 列成表。默认按「当前连着的 SSID」把本公司的 BSS 圈出来，
并猜测哪些 BSS 属于同一台 AP 的两个频段（这个猜测可以逐条改）。

### 第二步 · 确认相邻

一台一台问：这两个 AP 挨得近吗？**相邻的 AP 必须错开信道**，隔了几间房、离得远的
就可以复用同一条信道 —— 这正是 2.4 GHz 只有三条非重叠信道却能覆盖多于三台 AP 的原理。

不确定时的默认答案是「靠近」：报少了会让规划错误地复用同信道；报多了只会让规划更保守。

### 第三步 · 规划建议

结合「外部同频占用」和「你确认的相邻关系」，给出两个频段各自的信道与频宽建议，
并逐条写出依据。附一张频谱占用图，可以直接看出哪些信道已经被别人占住。

---

## 截图

| 第二步 · 确认相邻 | 第三步 · 规划结论 |
| --- | --- |
| ![第二步](tests/screenshots/step2-adjacency.png) | ![第三步](tests/screenshots/step3-plan-top.png) |

| 频谱占用图 | 本次扫描的发现 |
| --- | --- |
| ![频谱图](tests/screenshots/step3-spectrum.png) | ![发现](tests/screenshots/step3-findings.png) |

> 截图里的 SSID 形如 `示例-办公网`、`EXAMPLE-*`，是仓库内置的**示例快照**，
> 不是任何真实环境 —— 原因见[数据从哪来](#数据从哪来)。

---

## 快速开始

### 方式一 · 便携版（目标电脑不用装 Python）

```bat
python tools\build_portable.py
```

产出 `dist/ap-planner-portable.zip`，发给别人解压后双击 `start.bat` 即可 ——
会自动扫一次 Wi-Fi、起本地服务、打开浏览器。

### 方式二 · 源码运行（开发）

需要 Python 3.8+，不需要 pip 安装任何东西。

```bat
git clone <your-repo-url>
cd ap-planner

run_server.bat
```

`run_server.bat` 等价于下面这条，会先扫描一次再起服务并打开浏览器：

```bat
python backend\server.py --preload --open
```

只想导出一份离线快照、不起服务：

```bat
python backend\cli.py --table
```

之后直接双击 `frontend\index.html` 也能看到这批数据（页面走 `data/scan.js` 快照，
不依赖服务）。

--- 

## 项目结构

```
ap-planner/
├─ backend/                     Python 后端（零第三方依赖）
│  ├─ wlanapi.py                wlanapi.dll 的 ctypes 绑定
│  ├─ ie_parser.py              802.11 信息元素解析
│  ├─ wlan_scanner.py           扫描编排 + 归一化成页面列
│  ├─ server.py                 本地 HTTP 服务（静态文件 + /api/scan）
│  └─ cli.py                    命令行扫描 / 导出快照 / --diagnose 诊断
│
├─ frontend/
│  ├─ index.html                页面结构
│  ├─ favicon.svg
│  ├─ css/style.css             全部样式
│  ├─ js/
│  │  ├─ config.js              规划算法常量（CCA 门限 / 信道表 / 分组规则）
│  │  ├─ store.js               全局状态与通用工具
│  │  ├─ scan-data.js           数据来源：快照 + 实时接口
│  │  ├─ step1-scan.js          第一步：扫描结果表与圈选分组
│  │  ├─ step2-adjacency.js     第二步：逐台确认相邻关系
│  │  ├─ step3-plan.js          第三步：规划计算与结果渲染
│  │  └─ app.js                 步骤切换 / 重新扫描 / 导出 / 启动
│  └─ data/
│     ├─ scan.js                扫描快照（供页面直接读取）
│     └─ scan.json              同一份数据的 JSON 版本
│
├─ tests/                       验证工具（开发用，不影响运行）
│  ├─ page-smoke-test.mjs       用 jsdom 跑页面逻辑，58 项断言
│  ├─ browser-check.mjs         用系统 Chrome 跑一遍并截图，采集控制台报错
│  ├─ inspect-spectrum.mjs      打印频谱图每个信道的实际渲染状态
│  ├─ server_ports_check.py     端口策略：重复启动 / 端口被别的程序占用
│  ├─ scanner_fallback_check.py 扫描回退：多网卡 / 权限被拒 / 周围真没 AP
│  └─ screenshots/              browser-check 的输出
│
├─ tools/                       打包相关（开发用）
│  ├─ build_portable.py         生成 dist/ 下的便携版文件夹与 zip
│  └─ check_portable.py         便携版冒烟测试：真跑一遍包里的运行时与服务
│
├─ run_server.bat               启动本地服务
├─ scan_once.bat                只扫描并导出快照
├─ package.json                 仅为前端测试声明 jsdom（非 Python 依赖）
└─ LICENSE                      MIT
```

## 开发指南

### 环境要求

- **Windows 10 / 11**，Wi-Fi 已开启（后端硬依赖 `wlanapi.dll`，其它系统跑不了）
- **Python 3.8+**，只用到标准库，无需 `pip install`
- **Node 18+**（可选，只有前端测试需要）
- 若提示 `WlanSvc 服务未启动`，请在「服务」里启动 WLAN AutoConfig
- 若提示 `访问被拒绝（错误码 5）`，见[常见问题](#常见问题)

### 常用命令

```bat
:: 起服务（默认 127.0.0.1:8765，端口被占会自动往后找）
python backend\server.py
python backend\server.py --port 9000 --preload --open

:: 扫描并写入 frontend/data/ 下的快照
python backend\cli.py
python backend\cli.py --table           :: 顺便打印一张表
python backend\cli.py --json-only       :: 只输出 JSON，不写文件
python backend\cli.py --diagnose        :: 诊断：网卡清单 + 每一步的调用结果（不写文件）
```

### 测试

后端零依赖这一点让测试很容易跑 —— 大部分检查甚至不需要无线网卡。

```bat
npm install jsdom                          :: 只有前端测试需要

node tests\page-smoke-test.mjs             :: 页面逻辑 + 规划算法，58 项断言
node tests\browser-check.mjs               :: 真实浏览器渲染 + 控制台检查
node tests\inspect-spectrum.mjs            :: 频谱图逐信道状态
python tests\server_ports_check.py         :: 端口策略（7 项）
python tests\scanner_fallback_check.py     :: 扫描回退（19 项，用假的 wlanapi 替身）
```


### 打包便携版

```bat
python tools\build_portable.py
python tools\check_portable.py --from-zip   :: 解压真 zip 再验一遍
```
---

## 许可证

[MIT](LICENSE) © 2026 ap-planner contributors
。
