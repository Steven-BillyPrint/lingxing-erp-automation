# 领星订单最长边拆单阈值修改方案

## 1. 结论与目标

领星 OpenAPI 已通过真实订单验证，可以稳定读取订单详情页红框中的估算包裹尺寸。

本次修改目标是在现有“高金额非帐篷订单按预估实重拆单”规则上增加最长边条件。金额、重量、最长边是三个必须同时成立的条件：

```text
订单金额 > 200 USD/CAD
且满足既有适用范围（美国、非加急、非帐篷等）
且预估实重 > 用户设置的 kg 阈值
且估算包裹最长边 > 用户设置的 cm 阈值
```

三个条件之间使用 AND（且），不是 OR（或）。金额、重量和尺寸均使用严格“大于”；任一项未超过或恰好等于阈值时都不拆单。

尺寸阈值默认 `55 cm`，在设置页与现有重量阈值并排显示，允许用户输入并保存 `1～500 cm` 的整数值。本需求的标准验收设置为重量 `4 kg`、最长边 `55 cm`；已有重量阈值仍保留用户修改能力。

## 2. 真实接口验证证据

验证订单：

- 平台单号：`111-5339152-4637828`
- 领星系统单号：`103739787843301645`
- 订单列表请求链路 ID：`0cffee25640047b890a5bb687b7677b2.1788333829365`
- 订单详情请求链路 ID：`B9DEFD5A-3976-17B3-1BF1-4B89942BCF24`

`POST /pb/mp/order/v2/list` 返回：

```json
{
  "logistics_info": {
    "pre_weight": 3010.05,
    "pre_fee_weight": 1323.0,
    "pre_pkg_length": 45.0,
    "pre_pkg_width": 21.0,
    "pre_pkg_height": 7.0
  }
}
```

`POST /erp/sc/routing/order/Order/getOrderDetail` 返回：

```json
{
  "logistics_pre_weight": "3010.05",
  "logistics_pre_weight_unit": "g",
  "package_length": "45.0",
  "package_width": "21.0",
  "package_height": "7.0",
  "package_unit": "cm"
}
```

两套只读接口的估算尺寸均与领星页面红框 `45×21×7 cm` 一致，最长边为 `45 cm`。

该订单总金额为 `176.82 USD`，未超过 `200 USD/CAD` 门槛，因此即使重量和尺寸都超过各自阈值，也不应进入高金额拆单流程。

### 字段选择约束

- 多平台订单列表只读取 `logistics_info.pre_pkg_length/pre_pkg_width/pre_pkg_height`。
- Amazon 自发货详情兜底只读取顶层 `package_length/package_width/package_height/package_unit`。
- 不使用 `pre_fee_weight`，它对应页面“计费重”，不是现有规则使用的“实重 [估]”。
- 不使用 `pkg_length/pkg_width/pkg_height` 作为估算尺寸兜底。真实订单中这些字段在实际重量为 `0` 时仍返回 `45×21×7`，与页面实际尺寸为零的状态不一致，语义不足以安全参与预打包拆单判断。

## 3. 数据流修改

```text
领星订单列表 pre_pkg_*
        │
        ▼
尺寸规范化与完整性状态
        │
        ▼
同一系统订单多商品行聚合（尺寸不相加，只接受一致的订单级值）
        │
        ▼
BatchOrderItem：长、宽、高、最长边、状态、用户阈值
        │
        ▼
高金额规则：金额超限 AND 重量超限 AND 最长边超限
        │
        ▼
SKU 换货、说明书备注和拆单计划沿用现有流程
```

## 4. 文件级修改方案

### 4.1 配置模型与持久化

涉及文件：

- `erp_automation/configuration/settings.py`
- `erp_automation/contracts/models.py`
- `erp_automation/ui/persistent_controller.py`

修改内容：

1. 新增配置键 `automation.high_value_split_longest_side_cm`，默认值为 `55`。
2. `with_configuration_defaults()` 对该值进行整数和范围校验；无效、缺失或越界值回退到 `55`。
3. `DesktopSettings` 新增 `high_value_split_longest_side_cm: int = 55`。
4. `DesktopSettings.validate()` 只接受 `1～500 cm`。
5. `_settings_from_values()` 和 `_settings_values()` 双向映射新配置键。
6. 旧加密配置无需单独迁移；旧文件缺少新键时由默认值自动补为 `55`，用户下一次保存设置时写入加密配置。

### 4.2 设置页交互

涉及文件：

- `erp_automation/ui/qt.py`

修改内容：

1. 保留现有重量 `QComboBox`（3/4/5 kg）。
2. 新增 `QSpinBox`：
   - 范围：`1～500`
   - 默认：`55`
   - 后缀：` cm`
   - 步长：`1`
3. 将两个控件放在同一行，视觉文案建议为：

   ```text
   非帐篷高金额订单拆单阈值  重量超过 [4 kg ▼]  且最长边超过 [55 cm]
   ```

4. 工具提示明确说明尺寸读取自 `logistics_info.pre_pkg_*`，使用估算包裹尺寸，并强调金额、重量、最长边三项必须同时超限。
5. 把尺寸控件的 `valueChanged` 接入现有脏状态跟踪。
6. 在 `_save()` 中写入 `DesktopSettings.high_value_split_longest_side_cm`，在 `update_snapshot()` 中回显保存值。

### 4.3 API 字段规范化

涉及文件：

- `erp_automation/application/api_scanners.py`

新增 `_normalize_estimated_package_dimensions()`，职责如下：

1. 优先读取列表响应的 `logistics_info.pre_pkg_length/pre_pkg_width/pre_pkg_height`，兼容已有驼峰字段风格。
2. 订单详情兜底读取 `package_length/package_width/package_height`，并校验 `package_unit == cm`。
3. 数字和数字字符串统一使用 `Decimal` 解析，拒绝布尔值、NaN、无穷值、零和负数。
4. 每条边分别保留规范化结果，计算已知有效边的最大值。
5. 输出状态至少区分：
   - `valid`：三条边均有效；
   - `partial`：只有部分边有效；
   - `missing`：三条边均缺失；
   - `invalid`：存在无法解析或非正数值。
6. 将长、宽、高、最长边和状态写入扫描行与调试记录，便于追溯真实 API 输入。

### 4.4 同一订单聚合与领域模型

涉及文件：

- `lingxing_automation/pages/order_list.py`
- `lingxing_automation/models.py`

`BatchOrderItem` 新增：

```text
estimated_package_length_cm
estimated_package_width_cm
estimated_package_height_cm
estimated_package_longest_side_cm
estimated_package_dimensions_status
high_value_split_longest_side_threshold_cm
```

聚合规则：

1. `pre_pkg_*` 是订单级字段，虽然会重复到多个商品行，但不得按商品数量相加。
2. 同一系统订单的重复值完全一致时记为 `complete`。
3. 三边全部缺失时记为 `missing`。
4. 多行之间冲突、任一值无效或只返回部分边时记为 `invalid/partial`；如果某条已知边已经超过阈值，则可确定“最长边超限”，否则不能把不完整数据当作未超限。
5. 不从商品包装规格自行重算订单尺寸，确保判定与领星页面红框一致。

### 4.5 API 上下文与配置注入

涉及文件：

- `erp_automation/application/custom_order_api.py`
- `erp_automation/application/desktop_services.py`

修改内容：

1. `LingxingCustomOrderApiOperations.__init__()` 增加 `high_value_split_longest_side_cm=55`，做范围校验并保存。
2. `get_order_context()` 将尺寸阈值写入 `BatchOrderItem`。
3. `_merge_api_candidates()` 在列表尺寸不完整、详情尺寸完整时使用详情的 `package_*` 兜底；不得使用 `pkg_*`。
4. `DesktopApiServices.custom_order_operations()` 从加密配置读取并注入尺寸阈值。

### 4.6 拆单判定

涉及文件：

- `lingxing_automation/services/high_value_custom_order.py`

保留目的国、加急、品类、桌布数量等现有前置规则，并将金额、重量、最长边作为三个必须同时通过的门槛：

```text
amount_exceeded = 有效订单金额 > 200 USD/CAD
weight_exceeded = 有效预估实重 > 重量阈值
dimension_exceeded = 任一有效估算边长 > 最长边阈值
```

判定顺序：

1. 金额、重量、最长边三项均明确超过阈值：进入现有 `ready` 流程。
2. 任一项有有效值且明确未超过阈值：无需拆单；因为 AND 条件已经不成立。
3. 没有任何一项明确不超限，但至少一项缺失/无效：转人工，不能把未知值当作满足条件。
4. 绝不因只有重量超限、只有最长边超限，或二者超限但金额未超限而自动拆单。

`HighValueSplitEvaluation` 建议增加：

```text
estimated_package_longest_side_cm
longest_side_threshold_cm
amount_exceeded
weight_exceeded
dimension_exceeded
```

原因文案需同时显示三个门槛和实际读数，例如：

```text
订单总金额 220 USD 超过 200 USD；预估实重 4100g 超过 4000g；
估算最长边 56cm 超过 55cm。三项条件均满足，因此进入拆单流程。
```

后续换货为说明书、备注日期、拆包计划及写入步骤不改变。

### 4.7 审计与调试信息

涉及文件：

- `lingxing_automation/flows/contact_sync.py`

在现有候选调试和处理结果中追加：

- 三边估算尺寸；
- 最长边；
- 尺寸完整性状态；
- 用户设置的最长边阈值；
- 金额、重量、最长边三个条件各自的判定结果，以及最终 AND 判定结果。

这些字段仅用于业务审计，不记录鉴权信息。

## 5. 测试方案

### 5.1 API 解析测试

在 `tests/test_api_order_scanners.py` 增加：

1. `pre_pkg_*` 为数字时正确得到三边和最长边。
2. `pre_pkg_*` 为数字字符串时正确解析。
3. 零、负数、布尔值、NaN、无穷值和非数字字符串进入无效状态。
4. 缺一条边时记为部分数据，不误判为完整。
5. 详情 `package_* + package_unit=cm` 可作为列表缺失时的兜底。
6. `pkg_*` 和商品包装规格不会被当作红框估算尺寸。

### 5.2 规则矩阵测试

在 `tests/test_high_value_custom_order.py` 增加：

| 金额与门槛 | 重量与阈值 | 最长边与阈值 | 期望 |
|---|---|---|---|
| 等于 200 | 超过 4 kg | 超过 55 cm | 不拆单 |
| 超过 200 | 等于 4 kg | 超过 55 cm | 不拆单 |
| 超过 200 | 超过 4 kg | 等于 55 cm | 不拆单 |
| 超过 200 | 超过 4 kg | 超过 55 cm | 拆单 |
| 超过 200 | 超过 4 kg | 未超过 55 cm | 不拆单 |
| 超过 200 | 未超过 4 kg | 超过 55 cm | 不拆单 |
| 未超过 200 | 超过 4 kg | 超过 55 cm | 不拆单 |
| 超过 200 | 缺失 | 超过 55 cm | 转人工，不自动拆单 |
| 超过 200 | 超过 4 kg | 缺失 | 转人工，不自动拆单 |
| 超过 200 | 未超过 4 kg | 缺失 | 不拆单 |
| 超过 200 | 缺失 | 未超过 55 cm | 不拆单 |

继续覆盖加拿大、加急订单、帐篷订单及桌布数量例外，确保新增尺寸条件不扩大既有适用范围。

### 5.3 设置与注入测试

涉及测试：

- `tests/test_runtime_policies.py`
- `tests/test_custom_orders_qt.py`
- `tests/test_desktop_api_services.py`
- `tests/test_persistent_desktop_controller.py`

覆盖：

1. 旧配置缺少尺寸阈值时默认为 `55`。
2. 用户修改为 `60` 后保存、重启和回显仍为 `60`。
3. `0`、负数、非数字和大于 `500` 的旧配置回退为 `55`。
4. 设置页尺寸控件与重量控件处在同一行。
5. 桌面服务把保存值准确注入 `LingxingCustomOrderApiOperations`。

### 5.4 真实订单回归测试

使用平台单号 `111-5339152-4637828` 做只读验收：

- 解析结果：`3010.05 g`、`45×21×7 cm`、最长边 `45 cm`；
- 订单总金额：`176.82 USD`；
- 因金额低于 `200`，结果必须是不进入高金额拆单；
- 不执行换货、拆单、备注或任何领星写接口。

## 6. 验收标准

1. 设置页同一行显示重量阈值和最长边阈值，用户可修改并持久化尺寸阈值。
2. 标准设置为 `200 USD/CAD`、`4 kg`、`55 cm`；只有金额、预估实重、估算最长边三项都严格大于对应阈值时才拆单。
3. 规则使用 `pre_weight` 和 `pre_pkg_*`，不使用计费重或语义不可靠的 `pkg_*`。
4. 金额门槛为严格大于 `200 USD/CAD`；恰好 `200` 不拆单，现有国家、配送级别、品类规则不变。
5. 任何无法排除超限的缺失/异常数据不会被静默判为无需拆单。
6. 全量测试通过，并对真实订单完成一次只读回归。
7. 本方案不包含发布或部署；代码实现完成并经用户审核后，再按仓库正式发布流程单独授权执行。
