(() => {
  "use strict";

  const THEME_MODE_KEY = "longwatch-theme-mode";
  const THEME_MODES = ["auto", "light", "dark"];

  function preferredThemeForTime(now = new Date()) {
    const hour = now.getHours();
    return hour >= 7 && hour < 19 ? "light" : "dark";
  }

  function readThemeMode() {
    try {
      const stored = window.localStorage.getItem(THEME_MODE_KEY);
      return THEME_MODES.includes(stored) ? stored : "auto";
    } catch (_error) {
      return "auto";
    }
  }

  let themeMode = readThemeMode();

  function applyTheme(mode, { persist = false, redraw = false } = {}) {
    const normalized = THEME_MODES.includes(mode) ? mode : "auto";
    const effective = normalized === "auto" ? preferredThemeForTime() : normalized;
    const changed = document.documentElement.dataset.theme !== effective;
    themeMode = normalized;
    document.documentElement.dataset.theme = effective;
    document.documentElement.dataset.themeMode = normalized;
    const label = normalized === "auto"
      ? `自动 · ${effective === "light" ? "☀" : "☾"}`
      : normalized === "light" ? "白天 · ☀" : "夜间 · ☾";
    const button = document.getElementById("themeButton");
    if (button) {
      button.textContent = label;
      button.setAttribute("aria-label", `主题：${label.replace(" · ", "，")}`);
      button.title = "切换主题：自动 / 白天 / 夜间";
    }
    const themeColor = document.getElementById("themeColor");
    if (themeColor) themeColor.content = effective === "light" ? "#f3f6f8" : "#0b0e12";
    if (persist) {
      try { window.localStorage.setItem(THEME_MODE_KEY, normalized); } catch (_error) { /* 使用内存状态 */ }
    }
    if (redraw && changed) {
      refreshChartColors();
      drawChart();
    }
  }

  applyTheme(themeMode);

  const state = {
    positions: [],
    symbol: "",
    name: "",
    period: "day",
    count: 200,
    adjusted: true,
    allSessions: true,
    candles: [],
    ma5: [],
    ma20: [],
    hoverIndex: null,
    request: null,
    newsRequest: null,
    alertView: false,
    alertToken: "",
  };

  const initialParams = new URLSearchParams(window.location.search);
  const initialSymbol = initialParams.get("symbol")?.trim().toUpperCase() || "";
  if (/^[A-Z0-9][A-Z0-9.-]{0,31}$/.test(initialSymbol)) state.symbol = initialSymbol;
  // 兼容已经发送过的旧 Bark 链接：带 symbol 的外部直达链接默认使用单股视图。
  // 面板内选股会显式写入 view=portfolio，刷新后仍保留完整持仓界面。
  state.alertView = Boolean(state.symbol) && initialParams.get("view") !== "portfolio";
  state.alertToken = state.alertView ? initialParams.get("token") || "" : "";
  document.body.classList.toggle("alert-view", state.alertView);

  const el = Object.fromEntries([
    "connectionStatus", "positionCount", "positionList", "symbolForm", "symbolInput",
    "instrumentName", "instrumentSymbol", "lastPrice", "priceChange", "periods",
    "candleCount", "adjustToggle", "sessionsToggle", "refreshButton", "cursorLegend", "chartWrap",
    "klineChart", "chartMessage", "updatedAt", "notice", "noticeTitle", "noticeBody",
    "tradeComparison", "lastTradeRow", "lastTradeReference", "lastTradeDifference",
    "lastBuyRow", "lastBuyReference", "lastBuyDifference",
    "portfolioPulse", "portfolioAverage", "portfolioBreadth", "addWatchCount", "reduceWatchCount",
    "decisionPanel", "decisionBadge", "decisionReasons", "relativeMove", "distanceMa20",
    "distanceMa60", "volumeRatio", "volatility20", "currencyWeightLabel", "currencyWeight",
    "newsPanel", "newsStatus", "newsList",
    "settingsButton", "settingsDialog", "settingsForm", "settingsClose", "barkDeviceKey",
    "barkConfigured", "barkServerUrl", "barkGroup", "barkLevel", "barkSound",
    "settingsStatus", "testBarkButton", "saveBarkButton", "brandLabel", "themeButton",
  ].map((id) => [id, document.getElementById(id)]));

  if (state.alertView) {
    el.brandLabel.textContent = `${state.symbol} · 告警详情`;
    document.title = `${state.symbol} · 告警详情`;
  }

  const context = el.klineChart.getContext("2d");
  const colors = {};

  function refreshChartColors() {
    const styles = getComputedStyle(document.documentElement);
    const value = (name) => styles.getPropertyValue(name).trim();
    Object.assign(colors, {
      grid: value("--chart-grid"), axis: value("--chart-axis"), cross: value("--chart-cross"),
      neutral: value("--chart-label-bg"), labelText: value("--chart-label-text"),
      up: value("--up"), down: value("--down"), ma5: value("--ma5"), ma20: value("--ma20"),
    });
  }

  refreshChartColors();

  function setConnection(mode, text) {
    el.connectionStatus.className = `connection ${mode}`;
    el.connectionStatus.lastElementChild.textContent = text;
  }

  function showNotice(title, body) {
    el.noticeTitle.textContent = title;
    el.noticeBody.textContent = body;
    el.notice.classList.remove("hidden");
  }

  function hideNotice() { el.notice.classList.add("hidden"); }

  function withAlertAuth(url) {
    if (!state.alertView) return url;
    const secured = new URL(url, window.location.origin);
    secured.searchParams.set("symbol", state.symbol);
    secured.searchParams.set("view", "alert");
    secured.searchParams.set("token", state.alertToken);
    return `${secured.pathname}${secured.search}`;
  }

  async function requestJSON(url) {
    const response = await fetch(withAlertAuth(url), { headers: { Accept: "application/json" } });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(payload.detail || payload.error || `HTTP ${response.status}`);
      error.hint = payload.hint;
      throw error;
    }
    return payload;
  }

  async function settingsRequest(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-LongWatch-Request": "settings",
        ...(options.headers || {}),
      },
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || payload.error || `HTTP ${response.status}`);
    return payload;
  }

  function settingsMessage(text, mode = "") {
    el.settingsStatus.textContent = text;
    el.settingsStatus.className = `settings-status${mode ? ` ${mode}` : ""}`;
  }

  async function openSettings() {
    el.settingsDialog.showModal();
    settingsMessage("正在读取设置…");
    try {
      const settings = await settingsRequest("/api/settings/bark");
      el.barkConfigured.textContent = settings.configured ? "已配置；留空表示不修改" : "尚未配置";
      el.barkDeviceKey.placeholder = settings.configured ? "已保存，留空不修改" : "请输入 Device Key";
      el.barkServerUrl.value = settings.server_url || "https://api.day.app";
      el.barkGroup.value = settings.group || "LongWatch";
      el.barkLevel.value = settings.level || "timeSensitive";
      el.barkSound.value = settings.sound || "";
      settingsMessage(settings.configured ? "Bark 已配置" : "请填写 Device Key 后保存");
    } catch (error) {
      settingsMessage(error.message, "error");
    }
  }

  function barkFormPayload() {
    return {
      device_key: el.barkDeviceKey.value,
      server_url: el.barkServerUrl.value,
      group: el.barkGroup.value,
      level: el.barkLevel.value,
      sound: el.barkSound.value,
    };
  }

  async function saveBarkSettings(event) {
    event.preventDefault();
    el.saveBarkButton.disabled = true;
    el.testBarkButton.disabled = true;
    settingsMessage("正在保存…");
    try {
      const settings = await settingsRequest("/api/settings/bark", {
        method: "POST",
        body: JSON.stringify(barkFormPayload()),
      });
      el.barkDeviceKey.value = "";
      el.barkDeviceKey.placeholder = "已保存，留空不修改";
      el.barkConfigured.textContent = "已配置；留空表示不修改";
      settingsMessage(settings.configured ? "已保存，可以发送测试通知" : "保存失败", settings.configured ? "success" : "error");
    } catch (error) {
      settingsMessage(error.message, "error");
    } finally {
      el.saveBarkButton.disabled = false;
      el.testBarkButton.disabled = false;
    }
  }

  async function testBark() {
    el.saveBarkButton.disabled = true;
    el.testBarkButton.disabled = true;
    settingsMessage("正在发送测试通知…");
    try {
      await settingsRequest("/api/settings/bark/test", { method: "POST", body: "{}" });
      settingsMessage("INTC 测试告警已发送，请查看 Bark", "success");
    } catch (error) {
      settingsMessage(error.message, "error");
    } finally {
      el.saveBarkButton.disabled = false;
      el.testBarkButton.disabled = false;
    }
  }

  function formatNumber(value, digits = 2) {
    if (!Number.isFinite(Number(value))) return "—";
    return Number(value).toLocaleString("zh-CN", { maximumFractionDigits: digits });
  }

  function compactNumber(value) {
    const number = Number(value);
    if (number >= 1e9) return `${(number / 1e9).toFixed(2)}B`;
    if (number >= 1e6) return `${(number / 1e6).toFixed(2)}M`;
    if (number >= 1e4) return `${(number / 1e4).toFixed(1)}万`;
    return formatNumber(number, 0);
  }

  function renderPositions() {
    el.positionCount.textContent = state.positions.length;
    el.positionList.replaceChildren();
    if (!state.positions.length) {
      const empty = document.createElement("p");
      empty.className = "position-value";
      empty.textContent = "暂无证券持仓";
      el.positionList.appendChild(empty);
      renderTradeComparison();
      renderDecisionPanel();
      renderPortfolioPulse();
      return;
    }
    for (const position of state.positions) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `position-card${position.symbol === state.symbol ? " active" : ""}`;
      button.dataset.symbol = position.symbol;
      button.innerHTML = `
        <span class="position-name"></span><span class="position-value"></span>
        <span class="position-symbol"></span><span class="position-value position-change"></span>
        <span class="position-signal"></span><span class="position-relative"></span>`;
      button.querySelector(".position-name").textContent = position.name || position.symbol;
      button.querySelector(".position-symbol").textContent = position.symbol;
      const session = position.session && position.session !== "常规" ? ` · ${position.session}` : "";
      button.querySelector(".position-value").textContent = `现价 ${formatNumber(position.current_price, 4)} ${position.currency}${session}`;
      const changeNode = button.querySelector(".position-change");
      const change = Number(position.day_change_pct);
      changeNode.textContent = Number.isFinite(change) ? `${change >= 0 ? "+" : ""}${change.toFixed(2)}%` : "—";
      if (Number.isFinite(change)) changeNode.classList.add(change >= 0 ? "up" : "down");
      const signalNode = button.querySelector(".position-signal");
      signalNode.textContent = position.decision_label || "保持观察";
      signalNode.classList.add(position.decision_signal || "observe");
      const relative = valueNumber(position.relative_change_pct);
      const relativeNode = button.querySelector(".position-relative");
      relativeNode.textContent = relative === null
        ? "相对同市场 —"
        : `相对同市场 ${relative >= 0 ? "+" : ""}${relative.toFixed(2)}%`;
      if (relative !== null) relativeNode.classList.add(relative >= 0 ? "up" : "down");
      button.addEventListener("click", () => selectSymbol(position.symbol, position.name));
      el.positionList.appendChild(button);
    }
    renderTradeComparison();
    renderDecisionPanel();
    renderPortfolioPulse();
  }

  function valueNumber(value) {
    if (value === null || value === undefined || value === "") return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function signedPercent(value) {
    const number = valueNumber(value);
    return number === null ? "—" : `${number >= 0 ? "+" : ""}${number.toFixed(2)}%`;
  }

  function renderPortfolioPulse() {
    if (!state.positions.length) {
      el.portfolioPulse.classList.add("hidden");
      return;
    }
    const changes = state.positions.map((item) => valueNumber(item.day_change_pct)).filter((item) => item !== null);
    const average = changes.length ? changes.reduce((sum, item) => sum + item, 0) / changes.length : null;
    const upCount = changes.filter((item) => item > 0).length;
    const downCount = changes.filter((item) => item < 0).length;
    el.portfolioAverage.textContent = signedPercent(average);
    el.portfolioAverage.className = average === null ? "" : average >= 0 ? "up" : "down";
    el.portfolioBreadth.textContent = `${upCount} / ${downCount}`;
    el.addWatchCount.textContent = state.positions.filter((item) => item.decision_signal === "add_watch").length;
    el.reduceWatchCount.textContent = state.positions.filter((item) => item.decision_signal === "reduce_watch").length;
    el.portfolioPulse.classList.remove("hidden");
  }

  function setDecisionMetric(node, value, mode = "signed") {
    node.className = "";
    const number = valueNumber(value);
    if (number === null) {
      node.textContent = "—";
      return;
    }
    if (mode === "ratio") node.textContent = `${number.toFixed(2)}×`;
    else if (mode === "plain") node.textContent = `${number.toFixed(1)}%`;
    else {
      node.textContent = signedPercent(number);
      node.classList.add(number >= 0 ? "up" : "down");
    }
  }

  function renderDecisionPanel() {
    const position = state.positions.find((item) => item.symbol === state.symbol);
    if (!position) {
      el.decisionPanel.classList.add("hidden");
      return;
    }
    el.decisionBadge.className = `decision-badge ${position.decision_signal || "observe"}`;
    el.decisionBadge.textContent = position.decision_label || "保持观察";
    el.decisionReasons.textContent = (position.decision_reasons || []).join(" · ") || "暂时没有足够的方向信号";
    setDecisionMetric(el.relativeMove, position.relative_change_pct);
    setDecisionMetric(el.distanceMa20, position.distance_ma20_pct);
    setDecisionMetric(el.distanceMa60, position.distance_ma60_pct);
    setDecisionMetric(el.volumeRatio, position.volume_ratio_20d, "ratio");
    setDecisionMetric(el.volatility20, position.annualized_volatility_20d, "plain");
    setDecisionMetric(el.currencyWeight, position.currency_weight_pct, "plain");
    el.currencyWeightLabel.textContent = `${position.currency || "同币种"}证券仓位`;
    el.decisionPanel.classList.remove("hidden");
  }

  function newsTime(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "时间未知";
    const age = Date.now() - date.getTime();
    if (age >= 0 && age < 24 * 60 * 60 * 1000) {
      return `24h 内 · ${date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false })}`;
    }
    return date.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
  }

  function renderNews(articles) {
    el.newsList.replaceChildren();
    if (!articles.length) {
      const empty = document.createElement("p");
      empty.className = "news-empty";
      empty.textContent = "这只股票暂无近期新闻";
      el.newsList.appendChild(empty);
      return;
    }
    for (const article of articles) {
      const item = document.createElement(article.url ? "a" : "div");
      item.className = "news-item";
      if (article.url) {
        item.href = article.url;
        item.target = "_blank";
        item.rel = "noopener noreferrer";
      }
      const meta = document.createElement("div");
      meta.className = "news-meta";
      const category = document.createElement("span");
      category.className = `news-category ${article.category || "other"}`;
      category.textContent = article.category_label || "其他";
      const timestamp = document.createElement("time");
      timestamp.dateTime = article.published_at || "";
      timestamp.textContent = newsTime(article.published_at);
      const title = document.createElement("strong");
      title.textContent = article.title;
      meta.append(category, timestamp);
      item.append(meta, title);
      el.newsList.appendChild(item);
    }
  }

  async function loadNews() {
    if (!state.symbol) {
      el.newsPanel.classList.add("hidden");
      return;
    }
    if (state.newsRequest) state.newsRequest.abort();
    state.newsRequest = new AbortController();
    const requestedSymbol = state.symbol;
    el.newsPanel.classList.remove("hidden");
    el.newsStatus.textContent = "正在读取…";
    try {
      const payload = await fetch(withAlertAuth(`/api/news?symbol=${encodeURIComponent(requestedSymbol)}&count=5`), {
        signal: state.newsRequest.signal,
        headers: { Accept: "application/json" },
      }).then(async (response) => {
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.detail || body.error || `HTTP ${response.status}`);
        return body;
      });
      if (requestedSymbol !== state.symbol) return;
      renderNews(payload.articles || []);
      el.newsStatus.textContent = "10 分钟缓存";
    } catch (error) {
      if (error.name === "AbortError") return;
      if (requestedSymbol !== state.symbol) return;
      renderNews([]);
      el.newsStatus.textContent = "新闻暂不可用";
    }
  }

  function renderTradeComparison() {
    const position = state.positions.find((item) => item.symbol === state.symbol);
    if (!position) {
      el.tradeComparison.classList.add("hidden");
      return;
    }
    el.tradeComparison.classList.remove("hidden");
    renderComparisonRow({
      row: el.lastTradeRow,
      reference: el.lastTradeReference,
      difference: el.lastTradeDifference,
      label: `相比最近成交${position.last_trade_side ? `（${position.last_trade_side}）` : ""}`,
      emptyLabel: "近一年暂无成交记录",
      price: position.last_trade_price,
      amount: position.price_vs_last_trade,
      percentage: position.price_vs_last_trade_pct,
      timestamp: position.last_trade_timestamp,
      timeLabel: "成交时间",
    });
    renderComparisonRow({
      row: el.lastBuyRow,
      reference: el.lastBuyReference,
      difference: el.lastBuyDifference,
      label: "相比最近买入",
      emptyLabel: "近一年暂无买入记录",
      price: position.last_buy_price,
      amount: position.price_vs_last_buy,
      percentage: position.price_vs_last_buy_pct,
      timestamp: position.last_buy_timestamp,
      timeLabel: "买入时间",
    });
  }

  function renderComparisonRow({ row, reference, difference, label, emptyLabel, price, amount, percentage, timestamp, timeLabel }) {
    const hasPrice = price !== null && price !== undefined && price !== "";
    const hasAmount = amount !== null && amount !== undefined && amount !== "";
    const hasPercentage = percentage !== null && percentage !== undefined && percentage !== "";
    const numericPrice = Number(price);
    const numericAmount = Number(amount);
    const numericPercentage = Number(percentage);
    difference.className = "";
    row.removeAttribute("title");
    if (!hasPrice || !Number.isFinite(numericPrice)) {
      reference.textContent = emptyLabel;
      difference.textContent = "—";
      return;
    }
    reference.textContent = `${label} · ${formatNumber(numericPrice, 4)}`;
    difference.textContent = hasAmount && hasPercentage && Number.isFinite(numericPercentage)
      ? `${numericAmount >= 0 ? "+" : ""}${formatNumber(numericAmount, 4)} · ${numericPercentage >= 0 ? "+" : ""}${numericPercentage.toFixed(2)}%`
      : "—";
    if (hasAmount && Number.isFinite(numericAmount)) difference.classList.add(numericAmount >= 0 ? "up" : "down");
    if (timestamp) {
      row.title = `${timeLabel} ${new Date(timestamp * 1000).toLocaleString("zh-CN", { hour12: false })}`;
    }
  }

  async function loadPositions(loadChart = true) {
    try {
      const sessions = state.allSessions ? "all" : "intraday";
      const payload = await requestJSON(`/api/positions?sessions=${sessions}`);
      state.positions = payload.positions || [];
      setConnection("online", "行情服务在线");
      hideNotice();
      if (!state.symbol && state.positions.length) {
        state.symbol = state.positions[0].symbol;
        state.name = state.positions[0].name;
      }
      const selectedPosition = state.positions.find((item) => item.symbol === state.symbol);
      if (selectedPosition) {
        state.name = selectedPosition.name;
        if (state.alertView) {
          el.brandLabel.textContent = `${selectedPosition.name || state.symbol} · 告警详情`;
          document.title = `${selectedPosition.name || state.symbol} · 告警详情`;
        }
      }
      renderPositions();
      if (state.symbol && loadChart) {
        loadNews();
        await loadCandles();
      }
    } catch (error) {
      state.positions = [];
      renderPositions();
      setConnection("offline", "等待 API 配置");
      showNotice("Longbridge 行情暂不可用", error.hint || error.message);
      showChartMessage("配置 API 凭证后即可显示真实 K 线");
    }
  }

  function selectSymbol(symbol, name = "") {
    state.symbol = symbol.toUpperCase();
    const position = state.positions.find((item) => item.symbol === state.symbol);
    state.name = name || position?.name || state.symbol.split(".")[0];
    const url = new URL(window.location.href);
    url.searchParams.set("symbol", state.symbol);
    if (!state.alertView) url.searchParams.set("view", "portfolio");
    window.history.replaceState({}, "", url);
    renderPositions();
    loadCandles();
    loadNews();
  }

  function showChartMessage(text, loading = false) {
    el.chartMessage.classList.remove("hidden");
    el.chartMessage.querySelector("p").textContent = text;
    el.chartMessage.querySelector(".pulse-icon").style.opacity = loading ? "1" : ".65";
  }

  function hideChartMessage() { el.chartMessage.classList.add("hidden"); }

  function movingAverage(data, windowSize) {
    let sum = 0;
    return data.map((item, index) => {
      sum += item.close;
      if (index >= windowSize) sum -= data[index - windowSize].close;
      return index >= windowSize - 1 ? sum / windowSize : null;
    });
  }

  async function loadCandles() {
    if (!state.symbol) return;
    if (state.request) state.request.abort();
    state.request = new AbortController();
    showChartMessage("正在读取 K 线…", true);
    el.refreshButton.disabled = true;
    el.instrumentName.textContent = state.name || state.symbol;
    el.instrumentSymbol.textContent = state.symbol;
    try {
      const params = new URLSearchParams({
        symbol: state.symbol,
        period: state.period,
        count: String(state.count),
        adjust: state.adjusted ? "forward" : "none",
        sessions: state.allSessions ? "all" : "intraday",
      });
      const response = await fetch(withAlertAuth(`/api/candlesticks?${params}`), { signal: state.request.signal });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        const error = new Error(payload.detail || payload.error || `HTTP ${response.status}`);
        error.hint = payload.hint;
        throw error;
      }
      state.candles = payload.candles || [];
      state.ma5 = movingAverage(state.candles, 5);
      state.ma20 = movingAverage(state.candles, 20);
      state.hoverIndex = null;
      if (!state.candles.length) {
        showChartMessage("这个周期暂无 K 线数据");
      } else {
        hideChartMessage();
        updateSummary(state.candles.length - 1);
      }
      setConnection("online", "行情服务在线");
      hideNotice();
      el.updatedAt.textContent = `更新于 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
      drawChart();
    } catch (error) {
      if (error.name === "AbortError") return;
      state.candles = [];
      drawChart();
      setConnection("offline", "行情获取失败");
      showNotice("无法读取 K 线", error.hint || error.message);
      showChartMessage("暂时无法读取行情，请检查配置");
    } finally {
      el.refreshButton.disabled = false;
    }
  }

  function updateSummary(index) {
    const candle = state.candles[index];
    if (!candle) return;
    const previous = state.candles[Math.max(0, index - 1)];
    const change = previous.close ? (candle.close / previous.close - 1) * 100 : 0;
    el.lastPrice.textContent = formatNumber(candle.close, 4);
    const session = candle.trade_session && candle.trade_session !== "常规" ? ` · ${candle.trade_session}` : "";
    el.priceChange.textContent = `${change >= 0 ? "+" : ""}${change.toFixed(2)}%${session}`;
    el.priceChange.className = change >= 0 ? "up" : "down";
    const values = [candle.open, candle.high, candle.low, candle.close];
    el.cursorLegend.querySelectorAll("span:not(.ma) b").forEach((node, i) => {
      node.textContent = formatNumber(values[i], 4);
    });
    el.cursorLegend.querySelector(".ma5 b").textContent = formatNumber(state.ma5[index], 4);
    el.cursorLegend.querySelector(".ma20 b").textContent = formatNumber(state.ma20[index], 4);
  }

  function chartMetrics() {
    const rect = el.klineChart.getBoundingClientRect();
    const ratio = window.devicePixelRatio || 1;
    const width = Math.max(320, rect.width);
    const height = Math.max(320, rect.height);
    if (el.klineChart.width !== Math.round(width * ratio) || el.klineChart.height !== Math.round(height * ratio)) {
      el.klineChart.width = Math.round(width * ratio);
      el.klineChart.height = Math.round(height * ratio);
    }
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    return { width, height, left: 8, right: 70, top: 22, bottom: 26 };
  }

  function dateLabel(timestamp) {
    const date = new Date(timestamp * 1000);
    if (["1m", "5m", "15m", "30m", "60m"].includes(state.period)) {
      return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).replace(" ", "\n");
    }
    return date.toLocaleDateString("zh-CN", { year: "2-digit", month: "2-digit", day: "2-digit" });
  }

  function drawChart() {
    const m = chartMetrics();
    context.clearRect(0, 0, m.width, m.height);
    if (!state.candles.length) return;

    const data = state.candles;
    const plotWidth = m.width - m.left - m.right;
    const volumeHeight = Math.max(65, m.height * .18);
    const gap = 23;
    const priceBottom = m.height - m.bottom - volumeHeight - gap;
    const volumeTop = priceBottom + gap;
    const lows = data.map((item) => item.low);
    const highs = data.map((item) => item.high);
    let minPrice = Math.min(...lows);
    let maxPrice = Math.max(...highs);
    const pricePadding = Math.max((maxPrice - minPrice) * .08, maxPrice * .002);
    minPrice -= pricePadding;
    maxPrice += pricePadding;
    const priceRange = maxPrice - minPrice || 1;
    const maxVolume = Math.max(...data.map((item) => item.volume), 1);
    const step = plotWidth / data.length;
    const bodyWidth = Math.max(1, Math.min(8, step * .64));
    const x = (index) => m.left + step * (index + .5);
    const y = (value) => m.top + (maxPrice - value) / priceRange * (priceBottom - m.top);

    context.lineWidth = 1;
    context.font = "9px Inter, sans-serif";
    context.textBaseline = "middle";
    for (let i = 0; i <= 5; i += 1) {
      const gridY = m.top + (priceBottom - m.top) * i / 5;
      const price = maxPrice - priceRange * i / 5;
      context.strokeStyle = colors.grid;
      context.beginPath(); context.moveTo(m.left, gridY); context.lineTo(m.width - m.right, gridY); context.stroke();
      context.fillStyle = colors.axis;
      context.fillText(formatNumber(price, 3), m.width - m.right + 9, gridY);
    }

    const labelCount = Math.min(6, data.length);
    context.textAlign = "center";
    for (let i = 0; i < labelCount; i += 1) {
      const index = Math.round(i * (data.length - 1) / Math.max(1, labelCount - 1));
      const gridX = x(index);
      context.strokeStyle = colors.grid;
      context.beginPath(); context.moveTo(gridX, m.top); context.lineTo(gridX, m.height - m.bottom); context.stroke();
      context.fillStyle = colors.axis;
      context.fillText(dateLabel(data[index].timestamp), gridX, m.height - 10);
    }
    context.textAlign = "left";

    data.forEach((item, index) => {
      const candleX = x(index);
      const color = item.close >= item.open ? colors.up : colors.down;
      context.strokeStyle = color;
      context.fillStyle = color;
      context.beginPath(); context.moveTo(candleX, y(item.high)); context.lineTo(candleX, y(item.low)); context.stroke();
      const top = y(Math.max(item.open, item.close));
      const height = Math.max(1, Math.abs(y(item.open) - y(item.close)));
      context.fillRect(candleX - bodyWidth / 2, top, bodyWidth, height);
      const volumeBar = item.volume / maxVolume * volumeHeight;
      context.globalAlpha = .45;
      context.fillRect(candleX - bodyWidth / 2, volumeTop + volumeHeight - volumeBar, bodyWidth, volumeBar);
      context.globalAlpha = 1;
    });

    drawAverage(state.ma5, colors.ma5, x, y);
    drawAverage(state.ma20, colors.ma20, x, y);

    context.fillStyle = colors.axis;
    context.fillText(compactNumber(maxVolume), m.width - m.right + 9, volumeTop + 6);
    context.fillText("0", m.width - m.right + 9, volumeTop + volumeHeight);

    if (state.hoverIndex !== null) {
      const index = Math.max(0, Math.min(data.length - 1, state.hoverIndex));
      const hoverX = x(index);
      const hoverY = y(data[index].close);
      context.setLineDash([3, 4]);
      context.strokeStyle = colors.cross;
      context.beginPath(); context.moveTo(hoverX, m.top); context.lineTo(hoverX, m.height - m.bottom); context.stroke();
      context.beginPath(); context.moveTo(m.left, hoverY); context.lineTo(m.width - m.right, hoverY); context.stroke();
      context.setLineDash([]);
      context.fillStyle = colors.neutral;
      context.fillRect(m.width - m.right, hoverY - 9, m.right, 18);
      context.fillStyle = colors.labelText;
      context.fillText(formatNumber(data[index].close, 3), m.width - m.right + 7, hoverY);
    }
  }

  function drawAverage(values, color, x, y) {
    context.strokeStyle = color;
    context.lineWidth = 1.2;
    context.beginPath();
    let started = false;
    values.forEach((value, index) => {
      if (value === null) return;
      if (!started) { context.moveTo(x(index), y(value)); started = true; }
      else context.lineTo(x(index), y(value));
    });
    if (started) context.stroke();
    context.lineWidth = 1;
  }

  el.periods.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-period]");
    if (!button || button.dataset.period === state.period) return;
    state.period = button.dataset.period;
    el.periods.querySelectorAll("button").forEach((item) => item.classList.toggle("active", item === button));
    loadCandles();
  });

  el.settingsButton.addEventListener("click", openSettings);
  el.themeButton.addEventListener("click", () => {
    const nextMode = THEME_MODES[(THEME_MODES.indexOf(themeMode) + 1) % THEME_MODES.length];
    applyTheme(nextMode, { persist: true, redraw: true });
  });
  el.settingsClose.addEventListener("click", () => el.settingsDialog.close());
  el.settingsForm.addEventListener("submit", saveBarkSettings);
  el.testBarkButton.addEventListener("click", testBark);

  el.candleCount.addEventListener("change", () => { state.count = Number(el.candleCount.value); loadCandles(); });
  el.adjustToggle.addEventListener("change", () => { state.adjusted = el.adjustToggle.checked; loadCandles(); });
  el.sessionsToggle.addEventListener("change", () => {
    state.allSessions = el.sessionsToggle.checked;
    loadPositions(false);
    loadCandles();
  });
  el.refreshButton.addEventListener("click", loadCandles);
  el.symbolForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const symbol = el.symbolInput.value.trim().toUpperCase();
    if (!/^[A-Z0-9][A-Z0-9.-]{0,31}$/.test(symbol)) {
      showNotice("证券代码格式不正确", "请输入完整代码，例如 AAPL.US、700.HK。 ");
      return;
    }
    el.symbolInput.value = "";
    selectSymbol(symbol);
  });

  el.klineChart.addEventListener("mousemove", (event) => {
    if (!state.candles.length) return;
    const rect = el.klineChart.getBoundingClientRect();
    const plotWidth = rect.width - 78;
    const relative = Math.max(0, Math.min(plotWidth, event.clientX - rect.left - 8));
    state.hoverIndex = Math.min(state.candles.length - 1, Math.floor(relative / plotWidth * state.candles.length));
    updateSummary(state.hoverIndex);
    drawChart();
  });
  el.klineChart.addEventListener("mouseleave", () => {
    state.hoverIndex = null;
    if (state.candles.length) updateSummary(state.candles.length - 1);
    drawChart();
  });

  new ResizeObserver(drawChart).observe(el.chartWrap);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && themeMode === "auto") applyTheme("auto", { redraw: true });
  });
  window.addEventListener("storage", (event) => {
    if (event.key === THEME_MODE_KEY) applyTheme(event.newValue || "auto", { redraw: true });
  });
  setInterval(() => { if (themeMode === "auto") applyTheme("auto", { redraw: true }); }, 60_000);
  setInterval(() => { if (!document.hidden) loadPositions(false); }, 15_000);
  setInterval(() => { if (state.symbol && !document.hidden) loadCandles(); }, 60_000);
  setInterval(() => { if (state.symbol && !document.hidden) loadNews(); }, 600_000);
  loadPositions();
})();
