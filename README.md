# 领星网页订单批量巡检

这是一套基于领星 ERP 订单管理页的批量巡检自动化方案，不使用 `归档` 目录里的旧代码，也不调用领星 OpenAPI。

## 它做什么

1. 打开 `https://erp.lingxing.com/erp/mmulti/mpOrderManagement`。
2. 如果没有登录，会优先读取 `.env` 自动登录。
3. 切换到订单视图，按表头读取平台单号、系统单号、付款时间、ASIN、SKU、标签和物流信息。
4. 找出符合当前规则的定制订单，进入详情页下载并解析定制 zip。
5. 从定制 JSON 解析电话、邮箱和文件夹命名信息。
6. 写回 ERP 联系方式，并按规则生成或预览订单文件夹。

普通批量巡检会跳过已有标签、已完成查重和付款时间过旧的订单。安全重测单个订单会复用同一套批量处理链路，但只处理你输入的平台单号，并默认忽略标签、已完成状态和付款时间窗口，方便修改代码后反复验证。

## 代码结构

`lingxing_web_sync.py` 现在只作为兼容入口。日常使用请双击 `启动领星批量巡检.bat` 或 `安全重测单个订单.bat`。核心代码已经拆到 `lingxing_automation` 包里：

- `config.py`、`models.py`、`constants.py`：配置、数据结构和常量。
- `parsers/`：订单号、电话、邮箱和定制化文本解析。
- `browser/`：浏览器启动、登录和订单管理页等待。
- `pages/`：订单列表页、订单详情页的页面操作。
- `flows/contact_sync.py`：批量巡检、安全重测和遗留兼容流程。
- `storage/dedupe.py`：已处理平台单号查重。
- `products/tents.py`：帐篷父 ASIN、子 ASIN 和定制化提示语格式。
- `services/`：阶段 2 文件夹生成、阶段 3 SKU 决策、阶段 4 拆单决策的基础模块。

阶段 2-4 的规则示例放在 `rules/sku_rules.example.json` 和 `rules/split_rules.example.json`，后续正式规则不要直接写死到页面自动化代码里。

## 使用方法

首次使用先把 `.env.example` 复制为 `.env`，然后填写：

```text
LINGXING_ACCOUNT=你的手机号/用户名/邮箱
LINGXING_PASSWORD=你的密码
LINGXING_REMEMBER_LOGIN=true
AMAZON_REFRESH_TOKEN=
AMAZON_LWA_CLIENT_ID=
AMAZON_LWA_CLIENT_SECRET=
AMAZON_SP_API_SANDBOX=false
```

前三项用于领星自动登录。后四项用于 Amazon Selling Partner API（SP-API）订单商品数量读取：

- `AMAZON_REFRESH_TOKEN`：Amazon 卖家账号授权你的 SP-API 应用后生成的长期授权令牌。进入 Seller Central 的 `Apps and Services` -> `Develop Apps`，找到你的 SP-API 应用，对需要访问的卖家账号点击 `Authorize app`，授权完成后复制生成的 refresh token。
- `AMAZON_LWA_CLIENT_ID`：你的 SP-API 应用的 Login with Amazon（LWA）客户端 ID。进入 Seller Central 的 `Apps and Services` -> `Develop Apps`，找到应用后查看 `LWA credentials`，复制 `Client identifier`。
- `AMAZON_LWA_CLIENT_SECRET`：同一个 `LWA credentials` 页面里的客户端密钥，复制 `Client secret`。它和 refresh token 都是敏感凭据，只能放在本机 `.env`，不要提交到 GitHub。
- `AMAZON_SP_API_SANDBOX`：是否使用 SP-API 沙箱环境的本地开关。日常正式读取订单数量填 `false`；只有做沙箱测试时才填 `true`。

如果要临时覆盖 SP-API 地址，可以额外在 `.env` 里添加 `AMAZON_SP_API_ENDPOINT=...`。
官方参考：Amazon SP-API 的 [Authorize Private Applications](https://developer-docs.amazon.com/sp-api/docs/self-authorization)、[View your Application Information and Credentials](https://developer-docs.amazon.com/sp-api/docs/viewing-your-application-information-and-credentials) 和 [Selling Partner API Sandbox](https://developer-docs.amazon.com/sp-api/docs/sp-api-sandbox)。

日常自动巡检请双击 `启动领星批量巡检.bat`。修改代码后要反复测试同一个平台单号，请双击 `安全重测单个订单.bat`。

## 批量巡检

双击 `启动领星批量巡检.bat` 会进入批量模式。脚本会：

1. 登录后打开订单管理页。
2. 如果当前是“商品”视图，自动点击切换到“订单”视图。
3. 从当前订单列表里按表头列名读取 `ASIN/商品ID` 和 `付款时间`，不再靠整行文本猜测。
4. 只保留平台单号唯一、非拆分订单、主 SKU 数量为 1、未查重、帐篷 ASIN 命中、最近 24 小时付款的平台单号。
5. 进入详情页后再次带着列表里的 ASIN 和付款时间记录处理结果；非帐篷或超出付款时间窗口不会进入写回。
6. 根据帐篷父 ASIN 对应的定制化提示语格式，读取“更多商品信息”里的电话和邮箱。
7. 点击详情页“基本信息”这一栏右侧的“编辑”，写入“收货信息”里的电话和买家邮箱，然后保存。
8. 写回成功后把平台单号追加到 `data/processed_platform_orders.json`，下次巡检会跳过，避免重复修改。
9. 默认每5分钟重复一轮。

也可以手动运行：

```powershell
python lingxing_web_sync.py --batch --loop --batch-interval-minutes 5
```

如果需要临时放宽或缩短“最近一天”的判断，可以改用：

```powershell
python lingxing_web_sync.py --batch --batch-payment-hours 48
```

## 安全重测单个订单

修改代码后如果要拿同一个平台单号真实重测，不需要手动删除
`data/processed_platform_orders.json`、ERP 标签或 Z 盘订单文件夹。双击
`安全重测单个订单.bat`，输入平台单号即可。

等价命令：

```powershell
python lingxing_web_sync.py --retry-order "112-xxxxxxx-xxxxxxx" --apply --no-dedupe-write --no-create-folder --keep-browser-open
```

这个入口会真实打开 ERP，进入订单管理页并按平台单号搜索列表，然后走批量巡检的详情页处理流程；
`--retry-order` 会自动启用安全开关，不写正式查重文件，只预览文件夹路径，
不创建 Z 盘目录，也不会把定制 zip 复制进已有正式文件夹。命令里保留
`--no-dedupe-write` 和 `--no-create-folder` 是为了让双击入口的行为一眼可见。

如果要在安全重测里真实测试帐篷 SKU 页面调整，可以额外加：

```powershell
--allow-sku-adjustment
```

这个开关只允许 ERP 页面里的帐篷 SKU 调整动作；仍不创建 Z 盘订单文件夹、不复制定制 zip，也不写入正式查重状态。

批量模式会直接写回页面，不需要再加 `--apply`。建议把每页数量设为 `1000条/页`，这样一轮能覆盖当前筛选条件下的所有订单。

## 中国工作日日历维护

帐篷 SKU 阶段如果主商品换成 `Instruction`，脚本会按发货时限提前 3 个中国工作日生成客服备注，例如 `7.3发说明书`。中国大陆节假日和调休上班日维护在 `data/china_workdays.json`，后续添加 2027、2028 时只改这个 JSON，不需要改 Python 代码。

填写规则：

- `holidays` 填官方放假日期；连续日期可以写成 `["2027-02-06", "2027-02-12"]`，单日也可以写 `"2027-01-01"`。
- `adjusted_workdays` 填官方调休上班的周末日期，只写单日字符串。
- 普通周一到周五默认是工作日，普通周六周日默认休息，不需要写进 JSON。
- 如果发货时限年份没有 JSON 数据，脚本会停止自动备注并提示人工添加，不会猜日期。

新增年份示例：

```json
{
  "calendars": {
    "2027": {
      "source": "国务院办公厅关于2027年部分节假日安排的通知",
      "holidays": [
        "2027-01-01",
        ["2027-02-06", "2027-02-12"]
      ],
      "adjusted_workdays": [
        "2027-02-14"
      ]
    }
  }
}
```

## 登录说明

账号密码放在本机 `.env` 里，不写进代码；`.env` 已被 `.gitignore` 忽略，不要上传或发给别人。脚本仍会保存浏览器登录状态到 `browser_profile`，下次能继续使用。

如果领星触发验证码、短信验证、滑块验证或账号异常提示，脚本不会绕过验证，需要你在打开的浏览器里手动处理一次；处理完成后脚本会自动继续。

如果临时不想使用 `.env` 自动登录，可以运行：

```powershell
python lingxing_web_sync.py --retry-order "111-6622902-4192214" --no-auto-login
```

## 重要提醒

领星页面如果没有提供可编辑输入框，脚本会把电话/邮箱解析出来并停在需要人工保存的状态，同时保存截图到 `logs`。这种情况下通常需要从页面的“操作/编辑订单”入口进入可编辑表单后再运行或调整。

## 测试

```powershell
python -m pytest
```
