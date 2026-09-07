# 承运商格式与操作拒绝提示核查（2026-09-07）

## 核查范围与结论

逐项检查了源码内 15 个承运商名称、对应的单号格式、未知承运商推断和人工选择入口；通过现有认证接口只读分页检查了服务器当时全部 604 条物流记录（4 页、604 个不同物流任务），并读取相关格式拒绝日志。没有修改线上订单、发起物流写入或发送客户通知。

确认补充以下格式：

| 承运商 | 本次补充 | 核实依据 | 未知承运商时的处理 |
| --- | --- | --- | --- |
| Yanwen | `YWE` + 14 位数字 | 用户已核实的实际单号 `YWE00001506996989`；服务器人工保存拒绝日志 | 使用已有唯一匹配机制识别为 Yanwen，保留原始 `null` 审计 |
| DHL | 14 位数字 | [DHL eCommerce UK 开发指南](https://www.dhl.com/content/dam/dhl/local/gb/dhl-ecommerce/documents/pdf/gb-domestic-self-label-developers-guide-v5-0.pdf) 第 32 页的 Shipment No `55500001700462` | 与 FedEx 等数字单号重叠，不自动推断为 DHL |
| DHL | 20 位数字 | [DHL 德国 Express 官方帮助](https://www.dhl.de/de/geschaeftskunden/express/kontakt-express/online/sendungsstatus.html) 明确说明德国国内 Express 为 20 位 | 与其他承运商重叠，不自动推断为 DHL |
| 泛远 | `FAREX` + 10 位数字 + `YQ` | [泛远官方轨迹查询](https://www.far800.com/logistics/result?key=FAREX2606037475YQ) 返回真实已签收轨迹 | 该轨迹存在转交 FedEx 的记录，因此仅在承运商明确为泛远时验证，不从该转运号自动推断尾程承运商 |

## 其余承运商逐项检查

| 承运商 | 当前记录数（按承运商名称） | 复核内容与处理 |
| --- | ---: | --- |
| FedEx | 212 | 现有记录中的 12 位数字均通过；[官方 Ship Manager Server 指南](https://www.fedex.com/us/developer/webhelp/fsms/2024/Docs/FedEx_Ship_Manager_Server_20.08_Developer_Guide_MEISA.pdf) 的 14/15 位格式已在原规则中 |
| UPS | 238 | 有单号的记录均通过；[官方说明](https://www.ups.com/us/en/support/tracking-support) 中 1Z、T + 10 位数字、9/12 位数字、Mail Innovations 数字及 MI 格式已收录，不新增没有依据的 T + 9 位格式 |
| DHL | 25 | 现有 10 位数字均通过；补充上表两类官方明确格式，不将官网描述的 10–39 位范围泛化成任意字符串放行 |
| USPS | 23 | 22 位及含 420 路由前缀的 30/34 位样本均通过；[官方 Tracking 页面](https://tools.usps.com/go/TrackAction) 所示 82 开头的 9 位及国际邮政格式已有规则 |
| GOFO | 0 | [官方查询页](https://www.gofoexpress.com/tracking.html?searchID=GF6350311188916) 的 GF 样例符合原规则；未发现有一手依据的其他遗漏 |
| Yanwen | 15，另有 1 条原值为 null | YW + 地区字母 + 数字样本均通过；补充 YWE 新格式。另两条已完成记录的单号符合 USPS 420 格式，属于历史承运商归属问题，不扩充为燕文格式 |
| SpeedX | 0 | [官方标签 API](https://docs.speedx.io/reference/create-label) 与官方 SPX 轨迹样例核对，现有 SPX 长度范围覆盖已见样例 |
| UniUni | 3 | JY/UUS 样本均通过；[官方 Webhook 文档](https://help.ship.uniuni.com/en/webhooks/webhooks) 的 `UR12345678901234567` 已覆盖 |
| 1ST | 2 | 现有 1ST 样本通过；[官方常见问题](https://1st56.com/group/commonproblems/en/?pages=1) 区分最终 USPS 单号，不把合作邮政单号一概归到 1ST |
| SwiftX | 0 | [官网查询输入框](https://www.swiftx-express.com/) 的 `SWX123456789012345` 样例符合原规则；直接读取公开 HTML 复核 |
| 泛远 | 0 | 原名单中存在、原格式规则缺失；通过官网实际轨迹核实后补充限定格式，并排除自动推断 |
| Wanb Express | 30 | WNBAA 样本全部通过；[官方 API](https://apidoc.wanbexpress.com/parcels/Get-Parcel/) 区分检索号和最终派送号，不把所有下游承运商格式复制给万邦 |
| Canada Post | 9 | 16 位样本均通过；[官方说明](https://www.canadapost-postescanada.ca/cpc/en/support/receiving/tracking/find-your-tracking-number.page) 的 11/13 位 CA 结尾及 12 位 Priority Worldwide 均已收录 |
| Aramex | 1 | MP 字母数字混合样本通过；[官方查询入口](https://www.aramex.com/us/en/track/shipments) 与现有宽格式规则保持一致，仍不用于未知承运商推断 |
| OnTrac | 7 | 1LS 样本均通过；[官方支持页](https://www.ontrac.com/support/) 的 C/D/1LS/LS/LX/BN 前缀已收录 |

此外有 36 条承运商空白记录、2 条 SUNYOU 记录。SUNYOU 不在当前系统支持名单，其 [官方线路介绍](https://www.sypost.com/?id=0&num=1) 说明存在当地末公里服务商；这两条需要核实真实尾程或单独接入承运商，不属于现有承运商漏配单号格式，未擅自放行。全量核查覆盖现有数据和可核实的一手格式，未把第三方格式猜测作为自动标发依据。

人工修改物流窗口改为直接使用统一承运商名单，补齐原界面遗漏的 Wanb Express、OnTrac，防止后续后台支持新承运商而界面无法选择。

## 拒绝提示覆盖

- 单条修改、取消、恢复、重开、人工完成、保存物流、保存设置等操作返回 `accepted=False` 时，统一显示“操作被拒绝”和简短原因；旧 `non_modal` 标记不能隐藏明确拒绝。
- 批量结果即使有部分成功，只要存在 `skipped_reasons`、`rejected_orders` 或失败提交回执，也显示拒绝条目与原因摘要。
- 超时只表示结果待确认，不误报为已拒绝、不重发请求；随后服务器确认拒绝或部分拒绝时，客户端将结果排入一次性通知队列，在 GUI 线程弹窗。相同快照对象不会漏掉该通知。
- 已显示专用失败/冲突弹窗的路径添加明确标记，避免重复弹窗；自动重试的读取故障保持原重试行为。
- `_ControlResultThread` 增加结果投递完成信号，所有相关清理/删除操作移到结果回调之后，防止快速返回或嵌套弹窗时丢失结果。
- 同步/异步调用发生异常时显示保留业务原因的失败提示，并用现有脱敏方法清理凭据内容。
- 静态检查界面调用的 27 类变更方法，没有发现直接丢弃返回值的调用表达式。

## 验证

对应回归位于 `test_alibaba_logistics.py`、`test_shipment_queue_store.py`、`test_shipment_logistics_worker.py` 及新增的三个 `test_operation_*` 文件。包含格式边界、数字格式歧义、人工保存持久化、真实 GUI 修改动作返回拒绝、部分拒绝、延迟拒绝只提示一次、快速线程回调清理和异常脱敏。

本地完整测试：`pytest -q --disable-warnings --maxfail=3`，2845 项通过、5 项跳过（185 秒）。完整测试运行期间补充了服务器失败日志重复查询时按请求去重的保护；补充后单独运行格式、反馈、GUI 与协调拒绝的四个测试文件，168 项通过。两个弹窗样例已使用实际中文字体渲染核对，`git diff --check` 通过。整合分支仍需按发布流程运行完整 CI。

本次不修改版本号，不发布客户端、不部署服务器；独立提交交由统一发布任务集成及运行最终 CI。
