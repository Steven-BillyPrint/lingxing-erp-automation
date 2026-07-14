# ERP 自动化桌面程序

这是面向 Windows 的领星 ERP 自动化桌面程序。日常入口是 PySide6 图形界面或打包后的 `ERP自动化.exe`，不再要求用户运行 BAT、Python 命令或旧脚本。程序把领星官方 OpenAPI 能力、定制订单规则、自动标发队列、SQLite 状态管理、加密配置和跨电脑迁移集中到一个界面中。

当前版本采用“官方 API 能做的全部走 API，官方没有 API 的步骤才使用网页”的固定策略。旧脚本不会与新程序并行运行，仅保存在 Git 回退基线中。

## 日常使用

### 直接运行 EXE

将整个 `dist\ERP自动化` 文件夹放在当前 Windows 用户有写权限的位置，然后双击：

```text
dist\ERP自动化\ERP自动化.exe
```

不要只复制其中的 EXE，也不建议放到需要管理员权限才能写入的目录。程序会在 EXE 所在目录维护 `data`、`rules`、`logs` 和浏览器 Profile。网页专用步骤默认调用系统 Chrome；首次遇到登录、验证码或二次验证时，需要用户在打开的浏览器中完成验证，程序不会绕过网站安全校验。

### 第一次启动

1. 打开“设置”，填写领星 AppID、AppSecret，以及实际需要的领星网页、阿里国际站和 Amazon 凭据。
2. 确认订单文件夹和浏览器 Profile；两个 SQLite 数据库及日志目录固定在程序目录下，不能改到任意路径。
3. 填写“ERP 仓库/物流 ID 映射”，选择分阶段出库或快速出库。
4. 点击“保存加密配置”，再点击“测试领星 API”。测试只执行无副作用的订单读取，用于验证 Token、签名和连接。
5. 初始状态会“紧急停止所有 ERP 写入”。先完成只读扫描并检查候选数据，确认配置正确后，再到“状态管理”解除写入急停。

主界面包含：

- “仪表盘”：查看等待、运行、成功、需人工处理和已取消任务。
- “定制订单”：API 扫描候选，处理选中订单，查看或修改阶段状态，并可从指定阶段重新打开工作流。
- “自动标发”：API 扫描候选，查询阿里国际物流，执行选中标发，以及按物流、ERP 或邮件预览阶段重试和取消。
- “状态管理”：查看后台任务、能力实际模式和 ERP 写入急停开关。
- “设置”：统一编辑加密配置、测试 API、迁移旧状态和跨电脑迁移。
- “日志”：无需额外账号或权限即可查看程序日志。

后台任务使用单 Worker 串行执行，避免同一批订单被两个写入任务同时修改。急停会取消尚未开始的写任务；已经进入原子步骤的任务不会被强制杀死，但会在下一次写入边界重新检查急停并停止。后台仍有任务时不能解除急停。

存在等待或运行中的任务时，主窗口会拒绝关闭，避免任务在不可见状态下继续写入；请先等待结束，或在“状态管理”中安全取消尚未开始的任务。

## API 与网页能力边界

领星接口以[领星官方 API 文档](https://apidoc.lingxing.com/#/docs/TestToken/Token)为准。当前程序已接入下列官方能力：

| 业务能力 | 执行方式 | 主要官方接口 |
| --- | --- | --- |
| 获取及刷新 Token | API，自动维护 | `/api/auth-server/oauth/access-token`、`/api/auth-server/oauth/refresh` |
| 扫描订单列表 | API | `/pb/mp/order/v2/list` |
| 读取订单详情 | API | `/erp/sc/routing/order/Order/getOrderDetail` |
| 下载定制 ZIP | API | `/erp/sc/routing/customized/file/download` |
| 更新电话、调整订单商品 | API | `/pb/mp/order/v2/updateOrder` |
| 更新客户备注 | API | `/pb/mp/order/setRemark` |
| 拆分订单或包裹 | API | `/pb/mp/order/v2/splitOrder` |
| 查询仓库和物流方式 | API | `/erp/sc/data/local_inventory/warehouse`、`/erp/sc/routing/wms/WmsLogistics/listUsedLogisticsType` |
| 设置仓库和物流渠道 | API | `/pb/mp/order/editOrder` |
| 审核订单 | API | `/basicOpen/openapi/multiplatform/order/review` |
| 查询销售出库单 | API | `/erp/sc/routing/wms/order/wmsOrderList` |
| 写入跟踪号 | API | `/basicOpen/logisticsOrdering/setTrackingNo` |
| 发货出库 | API | `/basicOpen/selfShipmentOrder/deliveryGoods` |
| 快速出库及结果查询 | API | `/pb/mp/order/v2/fastOutbound`、`/pb/mp/order/v2/getFastOutboundResult` |

以下步骤没有可用的领星官方 API，因此保留网页实现：

| 能力 | 保留网页的原因 |
| --- | --- |
| 写入买家邮箱 | 官方订单更新接口可写电话，但没有买家邮箱写入字段 |
| 读取未遮罩的完整收货地址 | 当前官方订单接口未提供项目所需的完整未遮罩信息 |
| 查询阿里国际站物流 | 数据属于阿里国际站，不是领星 OpenAPI 能力 |

Amazon 订单商品数量继续通过 Amazon SP-API 获取。邮件阶段只在本地生成预览，不连接邮箱，也不会发送真实邮件。

对于已被官方 API 覆盖的功能，桌面程序只允许“API”或“禁用”，不会静默切回网页。读取失败会明确报错；写入如果超时、断线或返回结果无法证明成功或失败，会标记为 `UNKNOWN`/“需人工处理”，禁止自动重试同一写入，也禁止改用网页重复执行。待人工读回 ERP 状态确认后，再从界面指定阶段重开。

## Token、签名与加密配置

所有可编辑配置保存在工作区的 `data/config.enc`。它使用 Windows DPAPI 绑定当前 Windows 用户，不能被另一用户或另一台电脑直接解密。每次覆盖保存会保留 `data/config.enc.bak`，设置页中的密码字段使用掩码显示，敏感值不会写入 README、Git 或普通日志。

领星 Access Token 和一次性 Refresh Token 由程序自动申请、提前刷新并原子轮换。令牌单独保存在当前用户的：

```text
%LOCALAPPDATA%\ERPAutomation\lingxing-token.enc
```

令牌文件同样使用 DPAPI，并通过进程锁避免多个请求同时消费同一个 Refresh Token。令牌密文和载荷均绑定当前 AppID/AppSecret 的域分离单向指纹：任一凭据变化或遇到旧版未绑定令牌时，都会安全忽略缓存并重新签发；文件中不保存这两项明文。用户不需要复制、粘贴或手工更新 Token；修改 AppID/AppSecret 后保存配置，下一次任务会用新的凭据建立客户端。

如果旧版本仍有 `.env`，可在“设置”点击“导入旧 .env”。确认 `config.enc` 和 API 测试正常后，应人工删除明文 `.env`；新程序日常运行不依赖它。不要把 `config.enc` 当作跨电脑备份直接复制，跨电脑请使用下文的迁移包。

## ERP 物流 routes 配置

“ERP 仓库/物流 ID 映射”按阿里物流返回的规范化承运商名称匹配领星的仓库和物流方式。程序绝不会根据名称猜测 ID；缺少映射时，该任务进入人工处理。

下面的数字和字符串均为虚构占位值，不能直接用于生产，请替换为当前领星账号通过官方仓库、物流方式接口查到的真实值：

```json
{
  "示例承运商": {
    "warehouse_id": 123456,
    "logistics_type_id": 234567,
    "fast_logistics_type_id": "示例-快速出库线值",
    "freight_currency_code": "USD"
  }
}
```

- `warehouse_id`：正整数，领星仓库 ID。
- `logistics_type_id`：正整数，分阶段审核/出库使用的物流方式 ID。
- `fast_logistics_type_id`：快速出库接口要求的原始字符串；只使用分阶段出库时可省略。
- `freight_currency_code`：可选运费币种，例如 `USD` 或 `CNY`。

推荐先使用“分阶段审核并出库”。只有确认账号的快速出库字段和值后，才切换“快速出库”。所有 ERP 写入仍受全局急停和操作确认保护。

## 业务规则

### 付款时间窗口

候选扫描只处理付款时间位于最近 **96 小时**内的订单。API 查询会略微放宽边界以避免开放区间漏单，业务层随后再次执行精确的 96 小时判断；不会恢复为旧版本的 24 小时窗口。

### 3x6m 帐篷与整行替换

- 3x6m 帐篷规则中的拖轮包需求数量为 `x2`；现有 3x6m 沙袋规则也输出数量 `x2`。
- 主商品行用于替换配件时，必须整行替换，不能只替换其中一部分。
- 替换后的配件数量等于该主商品行最开始的数量。例如原商品行数量为 3，替换行数量也必须为 3，而不是最多替换 1 个。
- 当配件总需求不足以覆盖完整商品行时，该行不做部分替换；剩余配件需求使用新增商品行承接。

该语义同时保留来源订单行 ID、原始 SKU 和原始数量，用于 API 写入前匹配与写入后核对，防止把配件写到错误订单行。

## SQLite 状态与旧 JSON 迁移

新程序使用两个可持久化状态库：

```text
data/automation.sqlite3       # 定制订单、阶段状态、事件历史和查重
data/shipment_queue.sqlite3   # 自动标发队列、物流/ERP/邮件预览阶段和检查点
```

用户不需要直接打开数据库。在“定制订单”和“自动标发”页面可以查看当前状态；修改定制阶段、从阶段重开、重试自动标发阶段或取消任务时，都必须填写原因并保留审计历史。

旧 `data/processed_platform_orders.json` 可在“设置”中先执行“状态迁移预检”，确认数量后再点“JSON 迁入 SQLite”。执行时会：

1. 先生成带时间戳的 `processed_platform_orders.json.pre_sqlite_*.bak`；
2. 在 SQLite 事务中导入，失败则整笔回滚；
3. 保留原 JSON 和备份，不做隐式双向同步。

自动标发数据库的 schema 升级也会在原文件旁生成对应的升级前备份。不要在桌面程序运行期间使用第三方 SQLite 工具直接改库。

## 跨电脑迁移

在旧电脑打开“设置”并点击“导出到新电脑”。输入至少 12 个字符的迁移密码，并选择：

- 只迁移加密配置；或
- 同时迁移配置、定制订单 SQLite、自动标发 SQLite、工作日日历和规则文件。

生成的 `.erp-migrate` 文件使用 Argon2id 从密码派生密钥，再用 AES-GCM 加密并校验完整性。迁移包内不会直接放入机器绑定的 `config.enc`；配置会在新电脑导入时重新用该电脑当前 Windows 用户的 DPAPI 加密。导入替换文件前会生成 `.bak`。

无论旧电脑是否已经解除写入急停，导入到新电脑后都会强制恢复为“停止 ERP 写入”，必须先完成只读验证再由用户重新解除。

完整迁移只接受固定白名单中的业务文件：两个 SQLite、工作日日历、旧查重 JSON，以及 `rules` 下的非隐藏 JSON。即使迁移包本身通过密码校验，也不能覆盖程序源码、EXE、日志或白名单以外的路径；有后台任务运行时，导入、导出和状态维护都会被拒绝。

迁移包明确排除 `.env`、本机 Token、`browser_profile`、日志、调试截图、虚拟环境和生成输出。新电脑因此需要重新完成网页登录；首次 API 请求会根据迁移后的 AppID/AppSecret 自动申请 Token。迁移密码无法找回，请通过与迁移包不同的安全渠道传递密码。

## 邮件和日志策略

- 邮件永远是 `preview_only`：只生成本地批次和预览，不连接邮箱、不真实发送。
- 日志页面不增加应用内访问权限，当前电脑用户可直接查看；文件仍受 Windows 自身的文件权限保护。
- `logs` 和 `debug/logs` 中超过 90 天的普通日志文件会在程序启动时清理，保留期限固定为三个月；程序拒绝把其他名称的宽泛目录当作自动清理根目录，避免误删普通文件。
- 日志强制脱敏且不能关闭，不记录 AppSecret、Token、账号密码、完整邮箱或电话。

## 严重错误时回退

新程序不在运行时维护旧、新两套入口。脚本版本被冻结在 Git 分支：

```text
codex/script-baseline-20260714
```

可变业务数据的回退快照位于 `rollback_backups/rollback_时间戳`，每份快照包含 `manifest.json`、文件是否原本存在、大小和 SHA-256。恢复前会先验证所有哈希；目标已有文件还会保留一份 `.pre_restore.bak`。快照只允许工作区内普通文件，不接受符号链接或越界路径。

如果出现严重错误，请先退出桌面程序，然后明确要求维护者/Codex“回退到脚本基线”。标准顺序是：

1. 对当前 `config.enc`、SQLite、旧 JSON、日历和规则创建新的 `rollback_backups` 快照；
2. 在仍使用重构代码时，通过 `CustomWorkflowStore.export_legacy_json` 把当前 `automation.sqlite3` 的最新完成/忽略状态导出成脚本兼容的 `processed_platform_orders.json`，并校验导出数量；不能直接拿 14:11 的旧快照覆盖最新查重状态；
3. 保留当前重构分支和提交，再切换到 `codex/script-baseline-20260714`；
4. 恢复刚导出的兼容 JSON，以及脚本版本需要的配置/日历/规则，验证订单查重状态后再运行。

不要在程序仍运行或数据库仍有写入任务时直接切分支，也不要把“切换代码分支”误当作“自动恢复业务数据”。旧 BAT/脚本只用于这条受控回退路径，不是新版本的日常启动方式。

## 开发环境

项目要求 Windows；源代码至少需要 Python 3.11，当前开发环境建议使用 Python 3.14。安装依赖：

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

如电脑没有可供 Playwright 调用的 Chrome，可为源码调试安装 Chromium：

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

从源码启动桌面程序：

```powershell
.\.venv\Scripts\python.exe desktop_main.py
```

也可临时指定一个独立、可写的运行目录，避免开发测试接触正式状态：

```powershell
$env:ERP_AUTOMATION_HOME = "$PWD\smoke-workspace"
.\.venv\Scripts\python.exe desktop_main.py
Remove-Item Env:ERP_AUTOMATION_HOME
```

主要代码目录：

- `erp_automation/ui/`：PySide6 页面、桌面状态模型和持久化控制器。
- `erp_automation/application/`：桌面任务、API 扫描、能力路由、定制订单 API 和 ERP 标发编排。
- `erp_automation/integrations/lingxing/`：鉴权、Token 轮换、签名、端点策略和异步 HTTP 客户端。
- `erp_automation/configuration/`：DPAPI 配置、`.env` 导入和便携迁移加密。
- `erp_automation/persistence/`：定制订单 SQLite 工作流与事件历史。
- `lingxing_automation/`：产品规则、文件夹生成、定制解析，以及仅无 API 步骤所需的网页适配器。
- `shipment_automation/`：自动标发队列、阿里物流查询、ERP 检查点和邮件预览。

这些模块由桌面 Worker 在同一进程中调用，不会通过 BAT 或子进程启动旧脚本。

## 测试与打包

运行完整测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

构建 Windows 单目录 EXE：

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean "ERP自动化.spec"
```

产物位于：

```text
dist\ERP自动化\ERP自动化.exe
```

`ERP自动化.spec` 会收集三个项目包、PySide6/Playwright 运行依赖、工作日日历和规则示例，不应打包 `.env`、`data/config.enc`、业务数据库、浏览器 Profile、日志或真实订单输出。发布前建议使用独立的 `ERP_AUTOMATION_HOME` 完成以下冒烟检查：

1. 首次启动可以创建 DPAPI 配置和 SQLite；
2. 保存配置后 API 连接测试成功；
3. 写入急停开启时只读扫描可用、写入被阻止；
4. 状态修改、迁移预检和便携包导入/导出有明确结果；
5. 日志、错误提示和打包目录内均不存在真实凭据。
