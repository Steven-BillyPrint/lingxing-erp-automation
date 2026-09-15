# ERP 自动化

面向公司内部订单处理团队的 Windows 桌面应用，连接领星 ERP、Amazon 和阿里国际站，集中处理定制订单、物流标发和客户发货通知。

项目采用 **Windows 客户端 + Linux 协调服务**：客户端提供操作界面与本机浏览器交互，服务器统一管理业务队列、状态和审计记录，支持多人协作。

[![Tests](https://github.com/Steven-BillyPrint/lingxing-erp-automation/actions/workflows/test.yml/badge.svg)](https://github.com/Steven-BillyPrint/lingxing-erp-automation/actions/workflows/test.yml)

[下载客户端](https://github.com/Steven-BillyPrint/lingxing-erp-automation/releases/latest/download/ERP-Automation-Client.zip) · [版本发布](https://github.com/Steven-BillyPrint/lingxing-erp-automation/releases) · [问题反馈](https://github.com/Steven-BillyPrint/lingxing-erp-automation/issues)

## 主要功能

| 功能 | 说明 |
| --- | --- |
| 定制订单 | 扫描 Amazon 待审核订单，解析定制资料，生成订单文件夹，处理联系方式、SKU、拆包和备注；按阶段记录进度并支持恢复。 |
| 自动标发 | 扫描符合规则的 ERP 订单，查询阿里物流，校验承运商与国际单号，执行审核、出库和标发，并跟踪逾期及完成后的物流变化。 |
| 阿里物流下单辅助 | 根据订单准备收货地址和商品申报资料，填写并校验下单草稿；线路选择、查价和最终提交由操作者完成。 |
| 客户通知 | 汇总联系方式和包裹信息；自动模式下发送符合条件的通知，手动模式下由操作者审核后发送，保留发送结果与历史记录。 |
| 多人协作 | 共享任务队列、并发操作保护、可信操作账号、实时状态刷新和按账号隔离的配置。 |
| 运行管理 | ERP 写入急停、任务取消、带原因的状态调整、扫描审计及加密设置导入导出。 |

领星业务优先通过 OpenAPI 执行；联系方式写回、部分标发操作和阿里物流交互使用网页适配器。需要登录或人工验证时，任务会在对应客户端等待操作者完成。

## 安装与使用

### 运行条件

- Windows 电脑，并安装 Google Chrome，用于需要网页交互的业务步骤。
- 能够访问已部署的协调服务、企业登录服务及相关业务平台。
- 已获准使用的客户端授权材料和本人企业邮箱账号。

使用已打包客户端无需安装 Python。下载包仅包含程序，不包含企业授权、业务配置或订单数据。

### 首次安装

1. 下载并完整解压 [最新客户端 ZIP](https://github.com/Steven-BillyPrint/lingxing-erp-automation/releases/latest/download/ERP-Automation-Client.zip)。
2. 在解压目录打开 PowerShell，运行安装脚本：

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_shared_client.ps1
   ```

3. 从桌面快捷方式打开程序。首次启动先检查程序版本，再导入获准的 `.erp-client` 授权文件或填写授权材料。
4. 按界面提示使用本人企业邮箱完成登录，在“设置”中填写或导入领星、阿里国际站、Amazon 及通知服务配置。
5. 检查订单文件夹路径、仓库和物流 ID 映射，执行“测试领星 API”，再通过只读扫描核对数据。

日常使用从已安装客户端进入。新版通过程序内置更新器安装，失败时保留旧入口；源码仓库中的 `dist/` 和 `release-staging/` 是构建产物，不作为正式客户端安装目录，也不由更新器回写。

### 操作约定

- “设置 → 处理模式”提供当前账号的手动/自动切换，点击立即加密保存。默认自动模式；客户端在线时定制订单每 5 分钟扫描，标发订单、客户通知和阿里物流每 3 小时扫描；队列变化后立即调度，调度客户端每 60 秒做一次断线、交接和重启恢复检查，符合条件的订单无需勾选或逐条审核。
- 切换手动模式会撤下尚未开始的自动任务，已开始的订单继续完成；保留原有定时扫描和手动处理入口。多人在线时优先由支持自动处理且已开启自动模式的客户端协调发起，旧客户端在升级兼容期内立即交接；暂停、退出或掉线后由其他在线客户端接管。
- 客户通知允许先发送已出库且物流可用的部分包裹，尚未就绪的包裹显示 `Available soon`；后续新增已出库包裹时自动补发。重复扫描、文案变化和同一包裹的跟踪号修正不会自动重发；未知发送结果仍需人工核对。
- 自动模式下标发结果更新到订单状态与日志，不逐单弹出完成窗口，也不会在切回手动后补弹。手动提交的批次在手动模式下保留汇总提示。
- 定制处理按阶段保存结果；中断后先查看已完成阶段和错误原因，再选择继续或重试。
- ERP 写入受全局急停和操作确认控制。人工修改状态必须填写原因，且不能代替真实 ERP 操作。
- 自动处理沿用现有业务校验和急停。异常、人工取消、已执行过的自动任务和结果不确定的写入不循环重试；查看错误并使用现有继续/重开/重试操作处理。客户通知须有完整的来源快照、有效联系方式及至少一个可通知的新包裹；来源不完整或历史发送结果未确认时仍需人工核对。
- 手动模式下客户通知发送前需要审核。阿里物流下单辅助只填写草稿，最终下单由操作者提交。
- 登录、验证码和业务审核在发起任务的电脑上完成。退出程序时，正在执行的写入会等待安全边界后结束。

## 架构与数据

```text
Windows 客户端（PySide6）
├── 操作界面、企业登录、版本更新
├── 本机 Chrome：网页读取、填写与人工确认
└── 受控 SSH 隧道 + 服务端身份校验
    └── Linux 协调服务（Docker）
        ├── 共享任务、租约、业务编排与审计
        ├── SQLite 状态库与按账号加密的设置
        ├── 领星 OpenAPI、Amazon SP-API、通知服务
        └── 订单文件存储
```

服务器保存唯一的业务队列和持久化状态，客户端不各自维护一套业务数据库，也不通过共享文件夹并发读写 SQLite。网页任务绑定到提交任务的客户端；定时扫描由在线客户端协调发起，避免多人重复提交。

| 数据 | 存放位置与用途 |
| --- | --- |
| 正式客户端程序 | `%LOCALAPPDATA%\Programs\LingxingERP\<版本>`，按版本安装与更新。 |
| 客户端授权与本机状态 | `%LOCALAPPDATA%\LingxingERP`，与程序版本目录分离。 |
| 源码测试状态 | `%LOCALAPPDATA%\LingxingERP-LocalTest`，与正式客户端本机状态分离。 |
| 业务队列、配置和审计 | 服务器运行目录，由协调服务统一管理。 |
| 订单文件 | 按配置写入业务存储；当前部署通过受限挂载访问 NAS 目标目录。 |

`.erp-client` 是加密的设置与访问授权包，不包含业务数据库。授权文件和密码应分开保管；浏览器登录状态需要在新电脑按需建立。

## 开发

### 准备环境

Windows CI 使用 Python 3.14；服务端兼容性在 Python 3.12 环境验证。客户端依赖见 [requirements.txt](requirements.txt)，服务端依赖见 [requirements-server.txt](requirements-server.txt)。

在仓库根目录执行：

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

安装的 Chromium 用于 DOM 测试；日常网页操作使用本机 Chrome。源码联调和客户端打包需要下载仓库指定版本的 Cloudflared，脚本会校验下载内容：

```powershell
.\scripts\download_cloudflared.ps1
```

### 运行测试

```powershell
# 完整测试
.\.venv\Scripts\python.exe -m pytest -q

# 单独验证模块
.\.venv\Scripts\python.exe -m pytest -q tests/test_architecture_contracts.py
```

[Tests 工作流](.github/workflows/test.yml)包含 Windows 完整测试、Linux 服务端兼容性测试，以及 Windows 客户端构建、安装和更新冒烟检查。

### 本机联调

在已安装并授权正式客户端的电脑上，关闭正式客户端后运行：

```powershell
.\start_local_test.cmd
```

启动器使用当前分支源码，并在窗口中持续显示“本机测试”。旧的 `start_shared_desktop.cmd` 也转到这个入口。

**本机联调连接正式协调服务，订单处理、标发和通知发送会影响真实业务。** 独立测试目录只隔离本机状态，不隔离服务器数据。当前分支仅替换客户端源码；服务端改动先通过自动化测试，再按正式流程部署验证。

### 本地构建

```powershell
.\scripts\download_cloudflared.ps1
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean `
  --distpath "$PWD\release-staging\dist" `
  --workpath "$PWD\release-staging\build" `
  "ERP_Automation.spec"
```

构建结果位于 `release-staging\dist\ERP自动化`。生成客户端 ZIP：

```powershell
.\scripts\package_shared_client.ps1 `
  -BuiltApplicationDir "$PWD\release-staging\dist\ERP自动化" `
  -Version (Get-Content CLIENT_VERSION -Raw).Trim() `
  -ArchiveName "ERP-Automation-Client.zip"
```

`CLIENT_VERSION` 是源码版本权威，构建时写入 EXE；发布包中的 `VERSION.txt` 只是包元数据。正式安装目录由安装器和内置更新器维护，不通过复制构建产物或单独修改版本文件更新。

### 目录结构

```text
erp_automation/          应用编排、配置、公共契约、协调服务与桌面界面
lingxing_automation/     领星网页适配、定制资料解析与产品规则
shipment_automation/     物流查询、标发工作流与客户通知
rules/                  规则示例与仓库路由数据
data/china_workdays.json 工作日日历
scripts/                测试启动、构建、发布及维护工具
deploy/server/          Docker、systemd 与受控部署脚本
tests/                  业务规则、界面、集成与发布回归测试
docs/                   架构决策与专题审计记录
desktop_main.py         桌面入口
ERP_Automation.spec      PyInstaller 构建配置
CLIENT_VERSION          源码客户端版本
```

## 发布与部署

源码通过功能分支、测试和 PR 合并到 `main`。正式发布与部署需要负责人在当前任务中审核通过并明确授权，具体约束见 [AGENTS.md](AGENTS.md)。

维护者在干净且已同步 `origin/main` 的 Windows 工作区，确认该精确提交的完整 CI 成功后，依次执行：

```powershell
.\scripts\publish_client_release.ps1 -ConfirmProductionRelease
.\scripts\deploy_production.ps1 -ConfirmProductionDeployment
```

发布脚本复用该提交在 CI 中已验证的客户端资产并创建 GitHub Release。部署脚本在确认无活动任务后切换服务器，执行健康检查，再激活更新入口和客户端兼容窗口。服务器有活动任务时会拒绝切换，不能通过强制停止业务绕过检查。最后由用户打开现有客户端，通过内置更新器完成本机更新。

首次服务器安装及运行配置位于 [deploy/server](deploy/server)。协调接口只监听服务器回环地址，通过 SSH 隧道访问，并校验访问令牌和 Cloudflare Access 签名身份；业务配置、数据库和凭据位于源码仓库之外。

生产部署私钥固定保存于 `%LOCALAPPDATA%\Codex\credentials\erp-production-deploy-ed25519`，同目录的 `erp-production-known_hosts` 用于固定主机指纹。目录与私钥关闭权限继承，仅当前 Windows 用户、SYSTEM 和本机 Administrators 可访问。部署脚本显式使用 Windows OpenSSH，服务器上的密钥绑定受限部署命令。

共享备份目录只保存公钥、主机指纹及绑定当前 Windows 用户的 DPAPI 加密备份，**不保存可直接使用的明文私钥**。本机凭据丢失时，由获准维护者执行：

```powershell
.\scripts\restore_production_deploy_credentials.ps1 -ConfirmCredentialRestore
```

## 排障与维护

| 情况 | 处理方式 |
| --- | --- |
| 无法连接服务器或企业登录过期 | 检查网络和客户端授权，按程序提示完成企业登录；记录界面错误信息。 |
| 网页任务停在登录或验证页面 | 在发起任务的电脑上完成登录或人工验证，再按界面提示继续。 |
| 订单进入人工复核或写入被拒绝 | 查看订单身份、缺失字段、急停状态及任务日志，核对 ERP 实际结果后使用受控重试。 |
| 更新失败 | 保留原安装目录，记录更新错误并交由维护者排查；不要通过修改版本文件绕过校验。 |
| 需要定位扫描问题 | 按任务 ID 查看扫描审计；定制订单与自动标发页面分别提供日志目录入口。 |

反馈问题时提供客户端版本、操作步骤、预期与实际结果、任务 ID 或错误编号。日志可能包含订单与联系人信息，应通过公司认可的渠道传递，不在公开 Issue 中附上原始业务日志、授权文件或凭据。

构建输出、运行数据库、浏览器资料、日志和订单文件不属于源码。提交前检查差异，避免把本机生成文件加入仓库。架构边界见 [ADR-001](docs/architecture/ADR-001-contract-layer-boundaries.md)，性能与故障分析记录见 [docs](docs)。
