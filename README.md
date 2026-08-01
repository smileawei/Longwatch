# LongWatch

从 Longbridge OpenAPI 自动读取证券持仓，轮询最新行情，并在涨跌幅跨越阈值时通过 Bark 推送到 iPhone。

默认监控：

- 当日涨幅 / 跌幅（相对昨收）
- 持仓盈利 / 亏损（相对持仓成本，按持仓市值口径）
- 持仓每 5 分钟自动刷新，买入或清仓后不需要手工维护证券列表
- 告警状态持久化；同一阈值只告警一次，回到恢复区间后才重新启用
- Bark 发送失败不会误记为成功，下一轮会继续重试

## 快速开始

要求 Python 3.9+，以及已经开通的 Longbridge OpenAPI 权限。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

推荐使用 Python SDK OAuth。首次先注册 OAuth Client，并把返回的 `client_id` 写入：

```dotenv
LONGBRIDGE_OAUTH_CLIENT_ID=你的ClientID
LONGBRIDGE_OAUTH_CALLBACK_PORT=60355
```

然后执行一次本机授权：

```bash
longwatch --oauth-login
```

浏览器确认后，SDK 会把可自动刷新的令牌保存在 `~/.longbridge/openapi/tokens/<client_id>`。不要把令牌复制到 `.env` 或提交到 Git。

Bark 告警还需填写 `BARK_DEVICE_KEY`；它是 Bark App 首页测试 URL 中 `https://api.day.app/` 后面的部分。`.env` 已加入 `.gitignore`。

也可以在网页右上角打开“告警设置”，填写 Device Key、服务器、分组、通知级别和声音，并直接发送测试通知。已保存的 Device Key 不会回显到页面。

每条告警会携带对应股票的详情链接。LongWatch 每次告警都会生成一个独立随机 token，手机点击即可直接访问，不需要登录；token 在 24 小时后自动失效。用于签名的 `ALERT_LINK_SECRET` 会在首次启动时自动生成并保存在 `.env`，不会出现在页面或通知中。

```dotenv
ALERT_DETAIL_BASE_URL=http://127.0.0.1:8765
```

`127.0.0.1` 只适合在运行 LongWatch 的电脑上打开。若要从 iPhone 点击 Bark 通知访问，应改为手机能够访问的局域网或私有网络地址，并让 HTTP 服务监听相应网卡。告警 URL 中的 token 等同访问凭证，请勿分享；完整持仓首页仍建议仅在局域网或私有网络中使用。

同一 Wi-Fi 下的手机访问示例：

```dotenv
HTTP_HOST=0.0.0.0
HTTP_PORT=8765
ALERT_DETAIL_BASE_URL=http://192.168.1.100:8765
```

其中 `192.168.1.100` 应替换为运行 LongWatch 的电脑当前局域网地址。更换 Wi-Fi 后地址可能发生变化，需要同步更新该配置。

先验证两端配置：

```bash
longwatch --test-bark
longwatch --list
longwatch --once
```

确认无误后持续运行：

```bash
longwatch
```

## K 线网页

启动本地 HTTP 服务：

```bash
longwatch --web
```

浏览器打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。网页会自动列出当前持仓，并支持：

- 1 分、5 分、15 分、30 分、1 小时、日、周、月 K 线
- 100～1000 根数据切换
- 前复权切换、MA5 / MA20、成交量与悬停十字线
- “全时段”开关可包含盘前、盘后和夜盘，并标注价格来源
- 输入 `AAPL.US`、`700.HK` 等完整代码查看非持仓证券
- 持仓实时价每 15 秒刷新，K 线每 60 秒刷新
- 每只持仓显示现价相对最近一次成交价、最近一次买入价的金额和百分比差值；成交记录缓存 5 分钟
- 组合波动决策台按同市场持仓识别相对强弱，并结合 MA20/MA60、20 日量比与波动率标记加仓观察/减仓观察候选
- 点击股票后通过 Longbridge CLI 显示最近 5 条公开新闻，24 小时内新闻突出时间，并缓存 10 分钟

监听地址和端口可在 `.env` 中设置，也可以临时覆盖：

```bash
longwatch --web --host 127.0.0.1 --port 9000
```

服务默认只监听本机，不应直接暴露到公网。K 线接口单次最多获取 1000 根；行情范围与实时性取决于 Longbridge OpenAPI 行情权限。

## 告警阈值

在 `.env` 中修改，数值都填正数：

```dotenv
DAY_RISE_PCT=3
DAY_FALL_PCT=3
COST_GAIN_PCT=10
COST_LOSS_PCT=5
ALERT_HYSTERESIS_PCT=0.3
```

例如 `DAY_RISE_PCT=3` 会在当日涨幅第一次达到 `+3%` 时推送。只有涨幅回落到 `+2.7%` 或以下，再次升到 `+3%` 才会产生新告警。跌幅与成本盈亏规则同理。

默认只看常规交易时段价格。若需要按时间戳选取最新的美股盘前、盘后或夜盘价格：

```dotenv
INCLUDE_EXTENDED_HOURS=true
LONGBRIDGE_ENABLE_OVERNIGHT=true
```

扩展时段涨跌幅使用 SDK 为该时段返回的 `prev_close`。行情权限不足时，SDK 可能无法返回实时价格；Longbridge 的 OpenAPI 行情权限与 App/Web 行情权限是分开的。

## Docker 常驻运行

配置好 `.env` 后：

```bash
docker compose up -d --build
docker compose logs -f
```

Compose 会同时启动 Bark 监控进程和 `8765` 端口的 K 线网页。状态保存在 `./data/state.json`，容器重启不会造成同一告警重复推送。

## 自建 Bark Server

```dotenv
BARK_SERVER_URL=https://bark.example.com
BARK_DEVICE_KEY=你的设备Key
```

程序使用 Bark V2 的 `POST /push` JSON 接口，兼容官方与自建服务。

## 测试

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

> 本项目只读取持仓和行情，不包含下单逻辑。Access Token 仍具有敏感账户权限，应当像密码一样保管。
