# 近 15 天已完成订单物流更新与重新标发方案

日期：2026-09-01  
状态：已按真实接口验证修订，待评审后开发

## 1. 最终结论

领星公开 OpenAPI 没有以下两个能力：

1. 将“订单管理 > 已发货”的自建仓销售出库单撤销回待发货；
2. 将“订单标发 > 可更新”中的新标发单号再次提交到店铺后台。

因此，这个功能不能做成纯 OpenAPI 流程。建议采用“领星 OpenAPI + 本机专用 Chrome 网页适配器”的混合流程：

```text
读取近 15 天本系统自动完成任务
  -> 重新查询同一 ALS 的阿里物流详情
  -> 发现有效的新国际物流单号
  -> 创建独立重新标发周期
  -> 【领星网页】按系统单号撤销发货
  -> 【领星 OpenAPI】读回销售出库单已回到待发货
  -> 【领星 OpenAPI】重填运单号、ALS 号、运费、币种和计费重量
  -> 【领星 OpenAPI】逐字段读回
  -> 【领星 OpenAPI】重新出库并读回
  -> 等待领星订单标发进入“可更新”
  -> 【领星网页】按平台单号提交“标发”
  -> 【领星网页】读回线上物流包含新单号且状态回到“已完成”
  -> 完成周期并保留旧值、新值和全过程证据
```

全流程不使用 Amazon SP-API。店铺标发由领星官方“订单标发”网页完成，继续使用领星已有的店铺授权。

如果不允许使用领星网页自动化，则系统最多只能实现“15 天复查、识别单号变化、生成异常任务并提醒人工处理”，无法自动完成撤销发货和再次标发。

## 2. 已验证的能力边界

### 2.1 可以使用的领星 OpenAPI

| 业务动作 | OpenAPI | 用途 |
| --- | --- | --- |
| 查询销售出库单 | `/erp/sc/routing/wms/order/wmsOrderList` | 用系统单号或 `WO` 单号读回状态、单号、仓型、运费和重量 |
| 写入物流信息 | `/basicOpen/logisticsOrdering/setTrackingNo` | 仅在撤销回待发货后写入完整新物流数据 |
| 重新出库 | `/basicOpen/selfShipmentOrder/deliveryGoods` | 在新物流数据完全读回后重新出库 |

`setTrackingNo` 已在真实的 `status=3 / 已出库` 单据上返回错误，不能直接覆盖已发货订单；只能在网页撤销成功且 OpenAPI 已读回待发货状态后调用。

### 2.2 不能替代目标动作的接口

| 接口 | 原因 |
| --- | --- |
| `/basicOpen/wmsOrder/cancel` | 这是销售出库单“截单”，回退到待审核，不是“已发货撤销回待发货” |
| `/basicOpen/outboundOrder/outbound/setOrderRevoke` | 只接受 `OB...` 普通出库单，不接受系统单号或 `WO...` 销售出库单 |
| `/pb/mp/order/submitFulfillment` | 真实测试返回平台履约错误，不能可靠表达目标包裹的“更新已标发单号”语义 |
| `/pb/mp/order/editOrder` | 已发货订单不允许修改，且文档没有运单号、跟踪号字段 |

### 2.3 必须通过领星网页完成的动作

1. `REVERSE_SHIPPED_ORDER_UI`：订单管理 > 已发货 > 精确搜索系统单号 > 勾选唯一订单 > 撤销发货；
2. `UPDATE_MARKED_SHIPMENT_UI`：订单标发 > 可更新 > 精确搜索平台单号 > 核对系统标发单号 > 标发。

领星官方说明中，“撤销发货/撤销出库”与“截单”是两个不同动作；“可更新”页面则专门用于将新的系统标发单号更新到店铺后台。

## 3. 复查范围与变化判定

每次正常阿里物流查询完成后，再选择以下任务进行复查：

```text
identity_state = ACTIVE
erp_state = DONE
completion_source = AUTOMATION
outbounded_at >= 当前 UTC 时间 - 15 天
logistics_no 为可查询的 ALS 单号
当前没有活动租约或未结束的重新标发周期
```

15 天以本系统记录的 `outbounded_at` 为准，不以客户通知发送时间为准。人工发现已完成的 `MANUAL_DETECTED` 任务只生成提示，不自动撤销。

主要触发条件：

```text
normalize(阿里最新 international_tracking_no)
    != normalize(本地已经应用的 international_tracking_no)
```

同时保存并比较：承运商、国际运单号、ALS 物流订单号、运费、币种、计费重量、阿里状态、服务线路、查询时间和证据摘要。

以下结果只记录观察，不执行外部写入：

- 新单号为空、占位号、中间号或格式无效；
- 承运商与单号格式不匹配；
- 阿里状态未就绪、已取消、查询失败或详情不完整；
- 缺少运费、币种依据或计费重量；
- 只有空格、大小写或格式化差异；
- 只有运费或重量变化，单号和承运商没有变化；
- 相同新快照已经存在一个重新标发周期。

## 4. 数据模型

不要把原 `shipment_erp.state = DONE` 重置成普通待标发。原完成记录是已经发生的业务事实，需要永久保留。

建议把队列数据库从 v22 升级到 v23，并沿用现有迁移前备份策略。

在 `shipment_logistics` 增加：

```sql
completed_refresh_checked_at TEXT,
completed_refresh_next_at TEXT,
completed_refresh_last_error TEXT,
completed_refresh_snapshot_hash TEXT
```

新增独立周期表 `shipment_re_mark_cycles`，至少保存：

```sql
id, job_id, revision_no, source_snapshot_hash,
state, checkpoint, system_order_no, platform_order_no, wo_number,
old_carrier, old_waybill_no, old_tracking_no,
old_freight, old_currency, old_fee_weight_g,
new_carrier, new_waybill_no, new_tracking_no,
new_freight, new_currency, new_fee_weight_g,
attempt_count, next_attempt_at, lease_owner, lease_until, last_error,
detected_at, withdrawn_at, tracking_saved_at,
reoutbounded_at, platform_marked_at, created_at, updated_at
```

建立 `UNIQUE(job_id, source_snapshot_hash)`，保证同一变化只生成一个周期。旧值在检测时不能被新值覆盖；只有整个周期完成后，才把新快照提升为当前已应用物流。

一个订单同时只能有一个进入外部写入阶段的周期。新的变化只能排队等待，不能修改已经记录撤销意图的目标 payload。

## 5. 状态机与幂等

```text
DETECTED
  -> WMS_PRECHECKED
  -> WITHDRAW_UI_INTENT
  -> WITHDRAW_UI_CONFIRMED
  -> TRACKING_API_INTENT
  -> TRACKING_API_CONFIRMED
  -> OUTBOUND_API_INTENT
  -> OUTBOUND_API_CONFIRMED
  -> MARK_UI_WAITING_UPDATEABLE
  -> MARK_UI_INTENT
  -> MARK_UI_CONFIRMED
  -> COMPLETED
```

异常状态：

- `MANUAL_REVIEW`：外部状态不明确、页面结构变化、仓型不支持或结果冲突；
- `CANCELLED`：操作员明确取消本次周期。

每个外部写动作之前，必须先在 SQLite 事务中记录 intent、目标单号、payload hash 和当前权威状态；动作后必须通过独立读回推进检查点。不能仅根据“点击成功”、HTTP 200 或页面 toast 判定业务成功。

## 6. 撤销发货网页适配器

网页 DOM 动作建议放在现有页面适配层
`lingxing_automation/pages/shipment_reversal.py`；周期编排放在
`shipment_automation/lingxing_re_mark_browser.py`。这样可以复用现有
`erp_mark_ship.py` 的页面会话、确认、急停和故障恢复模式，而不在应用层新增
Playwright 依赖。

执行前：

1. OpenAPI 按系统单号读取销售出库单，并要求唯一匹配一条活动 `WO`；
2. 状态必须为 `3 / 已出库`，仓型必须是官方允许撤销的自建仓；
3. 系统单号、平台单号、`WO`、旧单号、运费和重量必须与周期旧快照一致；
4. 获取订单级跨客户端租约并检查 ERP 写入急停开关。

网页动作：

1. 复用现有本机专用 Chrome 和已登录的领星会话；
2. 打开订单管理“已发货”页；
3. 搜索类型选择“系统单号”，精确输入目标系统单号；
4. 要求表格只出现目标系统单号，并再次读取平台单号、仓库、运单号和跟踪号；
5. 只勾选目标行；
6. 记录 `WITHDRAW_UI_INTENT` 后点击“撤销发货”；
7. 在确认窗口再次核对目标单号后确认；
8. 保存操作前后页面结构摘要和截图路径，但不得保存登录凭据。

读回规则：

- OpenAPI 读回待发货：推进 `WITHDRAW_UI_CONFIRMED`；
- 仍为已出库：退避轮询，超时转人工，不重复点击；
- 变成已截单、待审核、消失或出现多个 `WO`：立即转人工；
- 浏览器断开或结果不明：重启后先 OpenAPI 读回，禁止重新点击。

网页定位器不能依赖按钮序号、表格行号或易变 CSS 类名。应使用可见文本、列名、系统单号和确认弹窗内容组成业务定位器；必要字段缺失时失败关闭。

## 7. OpenAPI 中间步骤

撤销读回成功后，调用现有 `setTrackingNo`，一次提交全部字段：

```text
waybill_no = 新国际物流单号
wo_number = 原销售出库单号
tracking_no = ALS 物流订单号
logistics_freight = 新运费
logistics_freight_currency_code = 币种
pkg_fee_weight = 新计费重量（g）
pkg_fee_weight_unit = g
```

之后用 `wmsOrderList` 逐字段读回。金额和重量使用 `Decimal` 比较；任一字段不一致都不能继续出库。

调用 `deliveryGoods` 前再次读回同一 `WO`：状态必须是待发货且所有新物流字段一致。调用后只有读回状态 `3 / 已出库` 且新字段仍一致，才确认成功。

如果请求响应丢失但读回已经满足最终条件，按成功恢复；如果仍为待发货且结果不明，转人工，不能自动重复提交出库。

## 8. 订单标发更新网页适配器

网页 DOM 动作建议放在
`lingxing_automation/pages/marked_shipment_update.py`，由同一个
`shipment_automation/lingxing_re_mark_browser.py` 周期执行器调用。

执行前必须满足：同一 `WO` 再次出库成功，WMS 为已出库，并且国际运单号、ALS 号、运费、币种和计费重量全部等于周期新值。

网页动作：

1. 打开“订单标发 > 可更新”；
2. 按平台单号精确搜索并要求唯一匹配；
3. 系统标发单号必须等于周期新运单号；
4. 线上物流尚不包含新单号；
5. 记录 `MARK_UI_INTENT` 后勾选目标记录并点击“标发”；
6. 如果页面要求选择包裹或商品，必须按系统单号和原始商品数量唯一匹配，存在歧义直接转人工；
7. 保存页面结果证据。

读回规则：

- 轮询“可更新/标发中/已完成”页面；
- 只有线上物流包含新单号且状态为已完成，才推进 `MARK_UI_CONFIRMED`；
- 恢复时如果页面已包含新单号，按幂等成功处理，不重复点击；
- 平台拒绝、数量不足、授权失效、多条包裹歧义或长时间停留标发中，转人工并保留领星原错误。

## 9. 调度、界面与开关

- 正常新订单优先，完成后复查作为低优先级批次；
- 建议每 3 小时复查一次；
- 复用现有阿里/领星本机专用浏览器、登录检查、主实例和跨客户端租约；
- 无在线客户端、登录失效或安全验证出现时，只延后复查，不改变原 `DONE`；
- 同一轮先只读发现变化，再由单线程执行器逐单写入，禁止并发操作领星订单页面。

新增能力：

```text
REVERSE_SHIPPED_ORDER = browser_only
UPDATE_MARKED_SHIPMENT = browser_only
```

新增独立开关，默认关闭：

```text
automation.completed_tracking_refresh_enabled
automation.automatic_reverse_shipment_enabled
automation.automatic_update_marked_shipment_enabled
```

推荐分三阶段启用：

1. 只读复查，只报告变化；
2. 自动生成周期，但两次网页写入均要求人工确认；
3. 真实订单验收通过后，再评审是否允许无人值守网页写入。

急停必须在每个网页确认点击、每个 OpenAPI 写请求前立即重查。已经发出的动作只能读回，不能因急停或重启盲目重放。

队列新增状态：

- `完成后复查中`
- `物流单号已更新，待重新标发`
- `正在撤销原发货`
- `正在重填单号/运费/重量`
- `正在重新出库`
- `等待订单标发可更新`
- `正在更新订单标发`
- `重新标发完成`
- `重新标发需人工复核`

详情展示系统单号、平台单号、`WO`、旧/新承运商、旧/新单号、新运费、币种、计费重量、当前检查点和最后读回结果。

## 10. 客户通知

本功能不自动发送第二封普通发货通知。旧通知已经成功发送，物流更正不能伪装成新包裹或新的首次发货。

推荐行为：

- 重新标发成功后更新包裹权威物流快照；
- 保留当前“已发送订单因同一包裹单号修正不自动重入发送队列”的保护；
- 队列详情提示“客户此前收到旧单号”；
- 若业务需要通知客户，另行生成明确标题为“物流单号更正”的人工审核草稿，同时展示旧、新单号，默认不自动发送。

真正新增包裹的补充通知逻辑保持不变。

## 11. 预计代码改动

| 文件/模块 | 改动 |
| --- | --- |
| `shipment_automation/models.py` | 新增复查/重新标发状态、周期 DTO 和统计字段 |
| `shipment_automation/queue_store.py` | v23 迁移、15 天选择器、周期去重、租约、检查点和事件 |
| `shipment_automation/logistics_worker.py` | 正常查询后增加已完成复查，发现变化时创建周期 |
| `erp_automation/application/capabilities.py` | 新增两个 `browser_only` 写能力 |
| `lingxing_automation/pages/shipment_reversal.py` | 撤销发货页面定位、核对和点击适配器 |
| `lingxing_automation/pages/marked_shipment_update.py` | “可更新”订单标发页面定位、核对和点击适配器 |
| `shipment_automation/lingxing_re_mark_browser.py` | 复用本机专用 Chrome，编排两个页面动作和恢复读回 |
| `erp_automation/application/api_erp_mark.py` | 独立重新标发执行器，复用 WMS 查询、`setTrackingNo`、`deliveryGoods` 和读回 |
| `erp_automation/application/desktop_tasks.py` | 接入单线程任务、网页确认、急停、恢复和进度 |
| `erp_automation/application/desktop_services.py` | 调度 15 天复查并汇总指标 |
| `erp_automation/application/queue_queries.py`、UI | 展示新状态、旧值到新值和人工处理入口 |
| `shipment_automation/notification_store.py` | 原则上无需业务改动，只补已发送通知不重发的回归测试 |

不要复用 `reopen_shipments_from_stage`。它只修改本地检查点，无法表达外部销售出库单已经撤销，也会破坏原始完成证据。

## 12. 必测场景

1. 15 天边界内被选中、边界外不选；`MANUAL_DETECTED` 不自动执行。
2. 相同单号或只有格式差异不创建周期；相同新快照重复扫描只有一个周期。
3. 新单号无效、运费缺失或重量缺失时，原 `DONE` 和已应用快照不变。
4. 多张活动 `WO`、缺少 `WO`、第三方仓、未知仓型或未知状态全部转人工。
5. 撤销点击后客户端崩溃：重启先读回，不重复点击。
6. 撤销后状态不是待发货：禁止写物流和重新出库。
7. `setTrackingNo` 请求后断线：六个字段完全读回一致后才能继续。
8. 重新出库响应丢失：已出库且新字段一致时恢复成功，否则不重复提交。
9. kg 到 g、运费、币种和小数比较与阿里详情一致。
10. “可更新”已包含新系统标发单号时准确标发；线上物流已经包含新单号时幂等完成。
11. 标发点击后断线：恢复时先查线上物流，不重复点击。
12. 领星页面列名、按钮或确认弹窗变化时失败关闭，不会点相邻订单。
13. 同一包裹单号更正后不自动发送第二封普通通知；真正新增包裹逻辑不受影响。
14. 两台客户端同时运行时，只有一个实例持有订单级租约。
15. 急停在每个写边界生效；已发出的动作只读回、不重放。
16. v22 升级 v23 前生成备份，原历史、完成状态和通知记录不变。

## 13. 验收标准

- 每轮阿里物流查询覆盖最近 15 天的自动完成订单，并报告目标数、读取数和变化数；
- 没有有效单号变化时，不发生任何领星网页点击或 OpenAPI 写入；
- 同一有效变化最多创建一个重新标发周期；
- 最终销售出库单为已出库，运单号、ALS 号、运费、币种和计费重量全部等于阿里最新值；
- 最终订单标发线上物流包含新单号，并回到已完成；
- 任一步断线、客户端退出或服务重启后，不重复撤销、不重复写物流、不重复出库、不重复标发；
- 无法确认最终状态时停在可定位的人工复核原因，不能误报完成；
- 已发送客户通知不会因同一包裹单号修正而自动再次发送；
- 完整 CI 通过后才能提交评审；正式发布和部署仍须等待用户明确审核授权。

## 14. 推荐实施顺序

1. 实现 v23 数据模型和纯状态机测试；
2. 接入 15 天只读复查，只生成变化周期，不执行外部写入；
3. 实现撤销发货网页适配器，并用专用测试订单验证读回和崩溃恢复；
4. 接入 `setTrackingNo` 完整写入、逐字段读回和 `deliveryGoods` 重新出库；
5. 实现“可更新”订单标发网页适配器和线上物流读回；
6. 补 UI、审计、通知不重发和跨客户端租约测试；
7. 先以人工确认模式试运行，验收后再评审是否开启无人值守模式。

## 15. 官方依据

- [领星销售出库单说明](https://www.lingxing.com/help/article/SalesDeliveryOrder)
- [领星订单标发说明](https://www.lingxing.com/help/article/MarkOrderAsShipped)
- [查询销售出库单列表](https://apidoc.lingxing.com/#/docs/Warehouse/WmsOrderList)
- [物流下单 - 编辑运单号/跟踪号](https://apidoc.lingxing.com/#/docs/Warehouse/setTrackingNo)
- [销售出库单截单](https://apidoc.lingxing.com/#/docs/Warehouse/cancelWmsOrder)
- [撤销普通出库单](https://apidoc.lingxing.com/#/docs/Warehouse/SetOutboundOrderRevoke)
