/* QuantAI 智能量化分析平台 - 前端逻辑 */
const state = {
  code: "000001", name: "平安银行",
  tab: "daily",
  charts: {},
  dailyRange: 120,
  dailyData: null, histData: null, shortData: null, heatData: null,
  chat: [],
};

/* ---------------- 工具 ---------------- */
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
const fmt = (v, n = 2) => (v === null || v === undefined || isNaN(v) ? "—" : Number(v).toFixed(n));
const fmtVol = (v) => {
  if (v === null || v === undefined) return "—";
  if (v >= 1e8) return (v / 1e8).toFixed(2) + "亿";
  if (v >= 1e4) return (v / 1e4).toFixed(1) + "万";
  return Math.round(v).toString();
};
const pctCls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
const pctText = (v) => (v > 0 ? "+" : "") + fmt(v) + "%";

async function api(path, opts) {
  const idempotent = !opts || !opts.method || opts.method === "GET";
  const maxRetry = idempotent ? 4 : 0;
  for (let attempt = 0; ; attempt++) {
    // 用 location.origin 拼绝对地址：地址栏若带账号密码（serveo 直达链接），
    // 直接 fetch 相对路径会抛 "URL includes credentials"
    const url = /^https?:/.test(path) ? path : new URL(path, window.location.origin).toString();
    const r = await fetch(url, opts);
    if (r.ok) {
      // 公网隧道带宽低时响应可能被截断：HTTP 状态仍是 200，但 JSON 解析会失败。
      // 对幂等 GET 自动重试，避免页面误报“未找到数据”。
      try {
        const text = await r.text();
        return JSON.parse(text);
      } catch (e) {
        if (attempt < maxRetry) {
          await new Promise((res) => setTimeout(res, 1500 * (attempt + 1)));
          continue;
        }
        throw new Error("数据传输不完整，请检查网络后重试");
      }
    }
    let msg = "HTTP " + r.status;
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    // 后端正在重启时（502/503/504），GET 请求自动重试，最多 3 次
    if (attempt < maxRetry && [502, 503, 504].includes(r.status)) {
      await new Promise((res) => setTimeout(res, 3000));
      continue;
    }
    if ([502, 503, 504].includes(r.status)) msg = "服务正在重启，请稍后刷新页面";
    throw new Error(msg);
  }
}

function mdToHtml(md) {
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const lines = esc(md).split("\n");
  let html = "", inUl = false, inTable = false, tRows = [];
  const closeUl = () => { if (inUl) { html += "</ul>"; inUl = false; } };
  const flushTable = () => {
    if (!inTable) return;
    html += "<table class='tbl'>";
    tRows.forEach((cells, i) => {
      const tag = i === 0 ? "th" : "td";
      html += "<tr>" + cells.map((c) => `<${tag} style="text-align:left">${c}</${tag}>`).join("") + "</tr>";
    });
    html += "</table>";
    inTable = false; tRows = [];
  };
  const inline = (s) =>
    s.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\*(.+?)\*/g, "<em>$1</em>").replace(/`(.+?)`/g, "<code>$1</code>");
  for (const raw of lines) {
    const line = raw.trim();
    if (line.startsWith("|")) {
      const cells = line.split("|").slice(1, -1).map((c) => inline(c.trim()));
      if (cells.every((c) => /^-+$/.test(c.replace(/:/g, "")))) continue;
      if (!inTable) { closeUl(); inTable = true; }
      tRows.push(cells);
      continue;
    }
    flushTable();
    if (!line) { closeUl(); continue; }
    if (line.startsWith("### ")) { closeUl(); html += `<h3>${inline(line.slice(4))}</h3>`; }
    else if (line.startsWith("## ")) { closeUl(); html += `<h3>${inline(line.slice(3))}</h3>`; }
    else if (line.startsWith("# ")) { closeUl(); html += `<h3>${inline(line.slice(2))}</h3>`; }
    else if (line.startsWith("- ")) {
      if (!inUl) { html += "<ul>"; inUl = true; }
      html += `<li>${inline(line.slice(2))}</li>`;
    } else if (/^\d+\.\s/.test(line)) {
      if (!inUl) { html += "<ul>"; inUl = true; }
      html += `<li>${inline(line.replace(/^\d+\.\s/, ""))}</li>`;
    } else if (line.startsWith("---")) { closeUl(); html += "<hr style='border-color:#233049;margin:10px 0'>"; }
    else { closeUl(); html += `<p>${inline(line)}</p>`; }
  }
  closeUl(); flushTable();
  return html;
}

/* ---------------- 图表基础 ---------------- */
// 图表库由 index.html 自愈式加载（CDN 多源 + 本地校验重试）。
// 这里等待加载结果事件，而不是干等到超时；失败后提供一键重载按钮。
function ensureEcharts(timeoutMs = 90000) {
  return new Promise((resolve) => {
    const ready = () => typeof window.echarts !== "undefined" && !!window.echarts.version;
    if (ready()) return resolve(true);
    let settled = false;
    const done = (v) => { if (settled) return; settled = true; cleanup(); resolve(v); };
    const onReady = () => done(true);
    const onFail = () => { if (!ready()) done(false); };
    const t = setTimeout(() => done(ready()), timeoutMs);
    const cleanup = () => {
      clearTimeout(t);
      window.removeEventListener("echarts-ready", onReady);
      window.removeEventListener("echarts-failed", onFail);
    };
    window.addEventListener("echarts-ready", onReady);
    window.addEventListener("echarts-failed", onFail);
    // 事件可能在监听前就已触发，轮询兜底
    (function poll() {
      if (settled) return;
      if (ready()) return done(true);
      setTimeout(poll, 300);
    })();
  });
}

function echartsFailHtml() {
  return '<div class="empty-tip"><div class="icon">📉</div>图表库加载失败' +
    '<div style="margin-top:12px"><button class="btn primary" onclick="window.__retryEcharts&&window.__retryEcharts();setTimeout(function(){location.reload()},1500)">重新加载图表库</button></div></div>';
}

function chartEl(id) {
  if (typeof window.echarts === "undefined") {
    const el = document.getElementById(id);
    if (el) el.innerHTML = echartsFailHtml();
    // 后台再试一次，成功后自动恢复，无需用户操作
    if (window.__retryEcharts) {
      window.__retryEcharts();
      window.addEventListener("echarts-ready", () => { if (window.__rerenderCurrent) window.__rerenderCurrent(); }, { once: true });
    }
    throw new Error("图表库 echarts 加载失败，请刷新页面后重试");
  }
  if (state.charts[id]) { state.charts[id].dispose(); }
  const el = document.getElementById(id);
  const c = echarts.init(el);
  state.charts[id] = c;
  return c;
}
window.addEventListener("resize", () => Object.values(state.charts).forEach((c) => c.resize()));

const UP_C = "#f04a5c", DOWN_C = "#22c58b", ACCENT = "#4f8bff";
const MA_COLORS = { ma5: "#f5d423", ma10: "#4f8bff", ma20: "#c56dff", ma60: "#22c58b", ma120: "#f5a623", ma250: "#ff7ac5" };

function baseOpt() {
  return {
    backgroundColor: "transparent",
    animation: false,
    textStyle: { color: "#8291b0" },
    tooltip: {
      trigger: "axis", axisPointer: { type: "cross", label: { backgroundColor: "#1b2445" } },
      backgroundColor: "rgba(18,26,46,.95)", borderColor: "#233049",
      textStyle: { color: "#dbe4f5", fontSize: 12 },
    },
  };
}

function klineSeries(kline, withMA) {
  const dates = kline.map((k) => k.day);
  const ohlc = kline.map((k) => [k.open, k.close, k.low, k.high]);
  const vols = kline.map((k, i) => ({
    value: k.volume,
    itemStyle: { color: k.close >= k.open ? UP_C : DOWN_C, opacity: 0.85 },
  }));
  const s = [
    {
      name: "K线", type: "candlestick", data: ohlc, xAxisIndex: 0, yAxisIndex: 0,
      itemStyle: { color: UP_C, color0: DOWN_C, borderColor: UP_C, borderColor0: DOWN_C },
    },
    { name: "成交量", type: "bar", data: vols, xAxisIndex: 1, yAxisIndex: 1 },
  ];
  if (withMA) {
    ["ma5", "ma10", "ma20", "ma60"].forEach((m) => {
      const arr = kline.map((k) => (k[m] === null || k[m] === undefined ? "-" : k[m]));
      if (arr.some((v) => v !== "-"))
        s.push({ name: m.toUpperCase(), type: "line", data: arr, xAxisIndex: 0, yAxisIndex: 0,
                 symbol: "none", lineStyle: { width: 1, color: MA_COLORS[m] } });
    });
  }
  // MACD
  const hist = kline.map((k) => ({
    value: k.macd ?? 0,
    itemStyle: { color: (k.macd ?? 0) >= 0 ? UP_C : DOWN_C },
  }));
  s.push({ name: "MACD", type: "bar", data: hist, xAxisIndex: 2, yAxisIndex: 2, barWidth: "60%" });
  s.push({ name: "DIF", type: "line", data: kline.map((k) => k.dif ?? "-"), xAxisIndex: 2, yAxisIndex: 2, symbol: "none", lineStyle: { width: 1, color: "#f5d423" } });
  s.push({ name: "DEA", type: "line", data: kline.map((k) => k.dea ?? "-"), xAxisIndex: 2, yAxisIndex: 2, symbol: "none", lineStyle: { width: 1, color: "#4f8bff" } });
  return { dates, ohlc, series: s };
}

function renderKlineChart(id, kline, opt = {}) {
  const c = chartEl(id);
  const { dates, series } = klineSeries(kline, opt.ma !== false);
  const showSlider = opt.slider !== false;
  const zoomPct = opt.zoomPct || [0, 100];
  c.setOption({
    ...baseOpt(),
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    legend: opt.ma === false ? undefined : {
      data: ["MA5", "MA10", "MA20", "MA60"], top: 0, textStyle: { color: "#8291b0", fontSize: 11 },
    },
    grid: [
      { left: 58, right: 20, top: 28, height: "48%" },
      { left: 58, right: 20, top: "63%", height: "12%" },
      { left: 58, right: 20, top: "80%", height: opt.slider === false ? "14%" : "13%" },
    ],
    xAxis: [0, 1, 2].map((i) => ({
      type: "category", data: dates, gridIndex: i,
      boundaryGap: true,
      axisLine: { lineStyle: { color: "#233049" } },
      axisLabel: { show: i === 2 || opt.slider === false, color: "#8291b0", fontSize: 10 },
      axisTick: { show: false },
    })),
    yAxis: [
      { scale: true, gridIndex: 0, splitLine: { lineStyle: { color: "#1a2440" } }, axisLabel: { fontSize: 10 } },
      { gridIndex: 1, splitLine: { show: false }, axisLabel: { show: false } },
      { gridIndex: 2, splitLine: { show: false }, axisLabel: { fontSize: 9 } },
    ],
    dataZoom: [
      { type: "inside", xAxisIndex: [0, 1, 2], start: zoomPct[0], end: zoomPct[1] },
      ...(showSlider ? [{ type: "slider", xAxisIndex: [0, 1, 2], bottom: 2, height: 16, start: zoomPct[0], end: zoomPct[1],
        borderColor: "#233049", backgroundColor: "rgba(18,26,46,.6)", fillerColor: "rgba(79,139,255,.15)",
        handleStyle: { color: "#4f8bff" }, textStyle: { color: "#8291b0", fontSize: 9 } }] : []),
    ],
    series,
  }, true);
}

/* ---------------- 股票搜索 / 搜索即提问 ---------------- */
let searchTimer = null;

function askFromSearch(el, input, drop) {
  const question = decodeURIComponent(el.dataset.ask);
  const code = el.dataset.code, name = el.dataset.name;
  input.value = ""; drop.classList.remove("show");
  switchTab("ai");
  if (code) selectStock(code, name);
  sendChat(question);
}

function initSearch() {
  const input = $("#searchInput"), drop = $("#searchDrop");
  input.addEventListener("input", () => {
    clearTimeout(searchTimer);
    const q = input.value.trim();
    searchTimer = setTimeout(async () => {
      if (!q && state.code) { drop.classList.remove("show"); return; }
      try {
        const data = await api("/api/stock/search", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ q }),
        });
        // 输入完整6位代码且精确唯一命中（含字符倒序自动纠正）：自动切换，无需点击
        const digits = q.replace(/\D/g, "");
        const rev = digits.split("").reverse().join("");
        if (!data.ask && data.results.length === 1 && digits.length === 6) {
          const s = data.results[0];
          if (s.code === digits || s.code === rev) {
            selectStock(s.code, s.name || s.code);
            input.value = ""; drop.classList.remove("show");
            return;
          }
        }
        let html = "";
        if (data.ask) {
          const a = data.ask;
          const who = a.code ? `已识别标的：${a.name} ${a.code}` : "未识别到具体股票，将结合全市场热度回答";
          html += `<div class="search-item ask" data-ask="${encodeURIComponent(a.question)}" data-code="${a.code || ""}" data-name="${a.name || ""}">
            <span class="nm">🤖 向 AI 提问：${a.question}<div class="cd ask-hint">${who}</div></span>
            <span class="ask-go">提问 ↵</span>
          </div>`;
        }
        if (data.results.length) {
          html += data.results.map((s) =>
            `<div class="search-item" data-code="${s.code}" data-name="${s.name || s.code}">
               <span class="nm">${s.name || "(名称加载中)"} <span class="cd">${s.market || ""}${s.code}</span>${s.corrected ? ' <span class="cd muted">（已自动纠正字符顺序）</span>' : ""}</span>
               <span class="${pctCls(s.pct)}">${fmt(s.price)} ${pctText(s.pct || 0)}</span>
             </div>`).join("");
        } else if (!data.ask) {
          const tip = data.loaded === false ? "未匹配（全市场列表可能仍在后台加载中…）" : `未匹配"${q}"，请确认代码或名称是否正确`;
          html = `<div class="search-item"><span class="nm muted">${tip}</span></div>`;
        }
        drop.innerHTML = html;
        drop.classList.add("show");
      } catch (e) {}
    }, 250);
  });
  // 事件委托：无论下拉内容怎么重建，点击始终生效
  drop.addEventListener("click", (e) => {
    const item = e.target.closest(".search-item");
    if (!item) return;
    if (item.classList.contains("ask")) { askFromSearch(item, input, drop); return; }
    if (item.dataset.code) {
      selectStock(item.dataset.code, item.dataset.name);
      input.value = ""; drop.classList.remove("show");
    }
  });
  // 兼容自动化工具直接赋值不触发 input 事件的情况：keyup/change 转发 + 全局轮询
  input.addEventListener("keyup", () => input.dispatchEvent(new Event("input")));
  input.addEventListener("change", () => input.dispatchEvent(new Event("input")));
  let lastVal = input.value;
  setInterval(() => {
    if (input.value !== lastVal) { lastVal = input.value; input.dispatchEvent(new Event("input")); }
  }, 400);
  input.addEventListener("keydown", async (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    clearTimeout(searchTimer); // 跳过防抖，立即查询，避免输入快时结果未返回导致"按了没反应"
    let data = null;
    try {
      data = await api("/api/stock/search", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ q }),
      });
    } catch (err) {}
    const digits = q.replace(/\D/g, "");
    const askEl = data && data.ask;
    const results = (data && data.results) || [];
    // 中文问句（无数字）优先走 AI 提问
    if (askEl && digits.length < 4) {
      askFromSearch({ dataset: { ask: encodeURIComponent(askEl.question), code: askEl.code || "", name: askEl.name || "" } }, input, drop);
      return;
    }
    // 有匹配结果：切到第一条（代码精确命中时 results 只有这一条）
    if (results.length) {
      const s = results[0];
      selectStock(s.code, s.name || s.code);
      input.value = ""; drop.classList.remove("show");
      return;
    }
    // 无匹配但输入含数字：取数字前6位补零强制尝试，找不到行情会在行情条给出提示（绝不静默）
    if (digits.length) {
      const code = digits.slice(0, 6).padStart(6, "0");
      selectStock(code, code);
      input.value = ""; drop.classList.remove("show");
      return;
    }
    // 纯中文无匹配：保持下拉的"未匹配"提示
    if (!drop.classList.contains("show")) drop.classList.add("show");
  });
  document.addEventListener("click", (e) => { if (!e.target.closest(".search-wrap")) drop.classList.remove("show"); });
}

/* ---------------- Tab 切换 ---------------- */
const TAB_LOADERS = { daily: loadDaily, history: loadHistory, analysis: loadAnalysis, short: loadShort, heat: loadHeat, global: loadGlobal, world: loadWorld, ai: loadModelsPanel, fundamental: loadFundamental, factors: loadFactors, backtest: loadBacktest, portfoliobt: loadPortfolioBacktest, watch: loadWatch, porisk: loadPortfolioRisk, capitalflow: loadCapitalFlow, validation: loadValidation, inst: loadInst };
function runLoader(tab) {
  const l = TAB_LOADERS[tab];
  if (!l) return;
  l().catch((e) => {
    // 加载失败（如代码不存在）时在行情条上给出可见提示，而不是静默失败
    const bar = $("#stockbar");
    if (!bar) return;
    bar.style.display = "flex";
    $("#sb-name").textContent = `⚠ ${state.name || state.code}`;
    $("#sb-price").textContent = "—";
    const isEcharts = String((e && e.message) || "").indexOf("echarts") >= 0;
    const pctEl = $("#sb-pct");
    // 图表库问题不要伪装成"没数据"：数据请求可能是成功的
    pctEl.textContent = isEcharts ? "图表库加载中" : "未找到行情数据";
    pctEl.className = "muted";
    $("#sb-grid").innerHTML = `<div class="qitem"><span>提示</span><b>${(e && e.message) || "加载失败，请检查代码是否正确"}</b></div>`;
  });
}

// 图表库迟到或重试成功后，自动重绘当前页，用户不需要手动刷新
window.__rerenderCurrent = () => {
  if (TAB_LOADERS[state.tab]) runLoader(state.tab);
};
window.addEventListener("echarts-ready", () => { window.__rerenderCurrent(); });
function switchTab(tab) {
  state.tab = tab;
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === tab));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + tab));
  runLoader(tab);
  if (tab === "ai") renderQuickChips();
}

function selectStock(code, name) {
  code = (code || "").trim();
  if (!code) return;
  state.code = code; state.name = name || code;
  state.dailyData = state.histData = state.shortData = null;
  const cs = $("#chatStock"); if (cs) cs.textContent = code + " " + (name || "");
  // 显著的切换反馈：stockbar 高亮闪动 + AI 页内插入系统提示
  const bar = $("#stockbar");
  if (bar) { bar.classList.remove("flash"); void bar.offsetWidth; bar.classList.add("flash"); }
  if (state.tab === "ai") {
    pushMsg("bot", `📌 已切换关注标的：<b>${name || code}（${code}）</b>，下方快捷提问与后续对话将围绕该股票展开。`);
  }
  if (["daily", "history", "analysis", "short", "fundamental", "factors", "capitalflow"].includes(state.tab)) runLoader(state.tab);
  else {
    // AI/热度页下切换：同步刷新顶部行情条，保证与当前标的一致
    api(`/api/stock/${code}/quote`).then((d) => renderStockbar(d)).catch(() => {});
  }
  if (state.tab === "ai") renderQuickChips();
}

/* ---------------- 通用：AI 解读块 ---------------- */
async function loadAIBox(type, elId, code) {
  const el = document.getElementById(elId);
  if (!el) return;
  el.innerHTML = `<div class="ai-loading"><span class="spin"></span>AI 模型分析生成中…</div>`;
  try {
    const d = await api("/api/ai/analyze", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: code || state.code, type }),
    });
    el.innerHTML = `<div class="ai-head">AI 智能解读 <span class="tag">${d.engine}</span></div><div class="ai-md">${mdToHtml(d.text)}</div>`;
  } catch (e) {
    el.innerHTML = `<div class="ai-md muted">AI 解读加载失败：${e.message}</div>`;
  }
}

/* ================= 每日行情 ================= */
async function loadDaily() {
  const code = state.code;
  const d = await api(`/api/stock/${code}/daily?days=250`);
  if (code !== state.code) return;
  state.dailyData = d;
  $("#stockbar").style.display = "flex";
  renderStockbar(d);
  renderKlineChart("daily-kline", d.kline.slice(-state.dailyRange));
  const s = d.summary;
  $("#daily-summary").innerHTML = `
    <div class="card"><h3>区间最高</h3><div class="big up">${fmt(s.period_high)}</div><div class="sub">区间涨幅 <b class="${pctCls(s.period_pct)}">${pctText(s.period_pct)}</b></div></div>
    <div class="card"><h3>区间最低</h3><div class="big down">${fmt(s.period_low)}</div><div class="sub">振幅 <b>${fmt((s.period_high / s.period_low - 1) * 100)}%</b></div></div>
    <div class="card"><h3>区间上涨天数</h3><div class="big up">${s.up_days}</div><div class="sub">占比 ${fmt(s.up_days / (s.up_days + s.down_days) * 100, 1)}%</div></div>
    <div class="card"><h3>区间下跌天数</h3><div class="big down">${s.down_days}</div><div class="sub">日均成交量 <b>${fmtVol(s.avg_volume)}</b></div></div>`;
  loadAIBox("daily", "daily-ai", code);
}

function renderStockbar(d) {
  const q = d.quote;
  const cls = pctCls(q.pct);
  $("#sb-name").innerHTML = `${d.name || "—"}<span class="code">${q.time ? "" : ""}${d.code}</span>`;
  $("#sb-price").className = "price " + cls;
  $("#sb-price").textContent = fmt(q.price);
  $("#sb-pct").className = "pct " + cls;
  $("#sb-pct").textContent = `${pctText(q.pct)}  (${q.pct > 0 ? "+" : ""}${fmt(q.price - q.prev_close)})`;
  const items = [
    ["今开", fmt(q.open)], ["最高", fmt(q.high)], ["最低", fmt(q.low)], ["昨收", fmt(q.prev_close)],
    ["成交量", fmtVol(q.volume)], ["成交额", q.amount !== null && q.amount !== undefined ? fmt(q.amount) + "亿" : "—"],
    ["行情时间", q.time || "—"],
  ];
  $("#sb-grid").innerHTML = items.map(([k, v]) => `<div class="qitem">${k}<b>${v}</b></div>`).join("");
}

function bindRangeButtons() {
  document.querySelectorAll("#daily-ranges .rng-btn").forEach((b) =>
    b.addEventListener("click", () => {
      document.querySelectorAll("#daily-ranges .rng-btn").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      state.dailyRange = +b.dataset.n;
      if (state.dailyData) renderKlineChart("daily-kline", state.dailyData.kline.slice(-state.dailyRange));
    }));
}

/* ================= 全部历史 ================= */
async function loadHistory() {
  const code = state.code;
  const d = await api(`/api/stock/${code}/history`);
  if (code !== state.code) return;
  state.histData = d;
  const rm = d.return_metrics;
  $("#hist-info").innerHTML = `
    <div class="card"><h3>上市日期</h3><div class="big" style="font-size:18px">${d.ipo_date}</div><div class="sub">已交易 ${d.total_days} 个交易日 / ${d.years} 年</div></div>
    <div class="card"><h3>累计收益</h3><div class="big ${pctCls(rm.total_return)}">${pctText(rm.total_return)}</div><div class="sub">年化收益 <b class="${pctCls(rm.cagr)}">${pctText(rm.cagr)}</b></div></div>
    <div class="card"><h3>最大回撤</h3><div class="big down">${pctText(rm.max_drawdown)}</div><div class="sub">${(rm.max_dd_start || "").slice(0, 10)} → ${(rm.max_dd_end || "").slice(0, 10)}</div></div>
    <div class="card"><h3>年化波动率</h3><div class="big">${fmt(rm.annual_volatility)}%</div><div class="sub">夏普比率 <b>${fmt(rm.sharpe)}</b></div></div>
    <div class="card"><h3>日胜率</h3><div class="big">${fmt(rm.daily_win_rate, 1)}%</div><div class="sub">涨 ${rm.up_days} 天 / 跌 ${rm.down_days} 天</div></div>`;
  // 后端为省带宽把全量 K 线压缩成 {fields, rows}，这里还原成对象数组供图表使用
  const kl = (d.kline && d.kline.fields)
    ? d.kline.rows.map((row) => {
        const o = {};
        d.kline.fields.forEach((f, i) => { o[f] = row[i]; });
        return o;
      })
    : d.kline;
  renderKlineChart("hist-kline", kl, { zoomPct: [60, 100] });
  // 年度收益
  const y = d.yearly;
  const yc = chartEl("hist-yearly");
  yc.setOption({
    ...baseOpt(),
    tooltip: { ...baseOpt().tooltip, formatter: (p) => `${p.name}年：${pctText(p.value)}` },
    grid: { left: 50, right: 20, top: 20, bottom: 30 },
    xAxis: { type: "category", data: y.map((x) => x.year), axisLabel: { fontSize: 10, color: "#8291b0" } },
    yAxis: { axisLabel: { formatter: "{value}%", fontSize: 10 } },
    series: [{
      type: "bar", data: y.map((x) => ({ value: x.ret, itemStyle: { color: x.ret >= 0 ? UP_C : DOWN_C } })),
      barMaxWidth: 26,
    }],
  }, true);
  // 最佳最差
  const bw = d.best_worst;
  $("#hist-extreme").innerHTML = `
    <div class="card"><h3>单日最大涨幅</h3><div class="big up">${pctText(bw.best_day.pct)}</div><div class="sub">${bw.best_day.date}</div></div>
    <div class="card"><h3>单日最大跌幅</h3><div class="big down">${pctText(bw.worst_day.pct)}</div><div class="sub">${bw.worst_day.date}</div></div>`;
  $("#hist-csv").onclick = () => window.open(`/api/stock/${code}/history.csv`);
  loadAIBox("full", "hist-ai", code);
}

/* ================= 深度分析 ================= */
async function loadAnalysis() {
  const code = state.code;
  const d = await api(`/api/stock/${code}/analysis`);
  if (code !== state.code) return;
  const btCode = $("#bt-code");
  if (btCode) btCode.textContent = state.name + "（" + state.code + "）";
  const ind = d.indicators;
  const sigHtml = d.signals.map((s) =>
    `<tr><td>${s.name}</td><td><span class="chip ${s.verdict}">${s.verdict}</span></td><td style="text-align:left">${s.desc}</td></tr>`).join("");
  $("#ana-signals").innerHTML = `<div class="card"><h3>信号面板（${d.date}）</h3>
    <table class="tbl"><thead><tr><th>指标</th><th>方向</th><th style="text-align:left">解读</th></tr></thead><tbody>${sigHtml}</tbody></table></div>`;
  $("#ana-kpi").innerHTML = `
    <div class="card"><h3>MACD</h3><div class="big" style="font-size:18px" >DIF ${fmt(ind.macd.dif, 3)}</div>
      <div class="sub">DEA ${fmt(ind.macd.dea, 3)} · 柱 ${fmt(ind.macd.hist, 3)}</div></div>
    <div class="card"><h3>RSI（6/12/24）</h3><div class="big" style="font-size:18px">${fmt(ind.rsi.rsi6, 1)}</div>
      <div class="sub">${fmt(ind.rsi.rsi12, 1)} / ${fmt(ind.rsi.rsi24, 1)}</div></div>
    <div class="card"><h3>KDJ</h3><div class="big" style="font-size:18px">K ${fmt(ind.kdj.k, 1)}</div>
      <div class="sub">D ${fmt(ind.kdj.d, 1)} · J ${fmt(ind.kdj.j, 1)}</div></div>
    <div class="card"><h3>布林带位置</h3><div class="big" style="font-size:18px">${fmt(ind.close)}</div>
      <div class="sub">[ ${fmt(ind.boll.low)} , ${fmt(ind.boll.mid)} , ${fmt(ind.boll.up)} ]</div></div>
    <div class="card"><h3>量比</h3><div class="big" style="font-size:18px">${fmt(ind.volume.vol_ratio)}</div>
      <div class="sub">5日均量 ${fmtVol(ind.volume.vol_ma5)}</div></div>
    <div class="card"><h3>支撑 / 压力</h3><div class="big" style="font-size:18px" ><span class="down">${fmt(d.support_resistance.support)}</span> / <span class="up">${fmt(d.support_resistance.resistance)}</span></div>
      <div class="sub">20日低点·布林下轨 / 20日高点</div></div>`;
  // 区间统计
  const st = d.stats;
  $("#ana-range").innerHTML = `
    <div class="card"><h3>近5日</h3><div class="big ${pctCls(st.pct_5d)}">${pctText(st.pct_5d)}</div></div>
    <div class="card"><h3>近20日</h3><div class="big ${pctCls(st.pct_20d)}">${pctText(st.pct_20d)}</div></div>
    <div class="card"><h3>近60日</h3><div class="big ${pctCls(st.pct_60d)}">${pctText(st.pct_60d)}</div></div>
    <div class="card"><h3>20日高/低</h3><div class="big" style="font-size:18px"><span class="up">${fmt(st.high_20d)}</span> / <span class="down">${fmt(st.low_20d)}</span></div></div>`;
  // 月度平均收益
  const ma = d.monthly_avg || [];
  const mc = chartEl("ana-monthly");
  mc.setOption({
    ...baseOpt(),
    tooltip: { ...baseOpt().tooltip, formatter: (p) => `${p.name}月平均收益：${p.value === null ? "—" : pctText(p.value)}` },
    grid: { left: 50, right: 20, top: 20, bottom: 30 },
    xAxis: { type: "category", data: Array.from({ length: 12 }, (_, i) => (i + 1) + "月") },
    yAxis: { axisLabel: { formatter: "{value}%", fontSize: 10 } },
    series: [{
      type: "bar", barMaxWidth: 26,
      data: ma.map((v) => ({ value: v, itemStyle: { color: v === null ? "#45536e" : v >= 0 ? UP_C : DOWN_C } })),
    }],
  }, true);
  loadAIBox("daily", "ana-ai", code);
}

/* ================= 短期涨跌 ================= */
async function loadShort() {
  const code = state.code;
  const d = await api(`/api/stock/${code}/shortterm`);
  if (code !== state.code) return;
  state.shortData = d;
  const dirCls = d.score >= 30 ? "up" : d.score <= -30 ? "down" : "";
  $("#short-head").innerHTML = `
    <div class="card"><h3>方向判断</h3><div class="big ${dirCls}">${d.direction}</div><div class="sub">置信度：${d.confidence}</div></div>
    <div class="card"><h3>5日上涨概率</h3><div class="big">${d.up_prob_5d}%</div><div class="sub">模型：7因子加权 + 概率映射</div></div>
    <div class="card"><h3>ATR 波动区间</h3><div class="big" style="font-size:18px">${d.expected_range[0]} ~ ${d.expected_range[1]}</div><div class="sub">未来5日参考区间（基于ATR14）</div></div>
    <div class="card"><h3>综合评分</h3><div class="big ${dirCls}">${d.score > 0 ? "+" : ""}${d.score}</div><div class="sub">范围 -100（极度看空）~ +100（极度看多）</div></div>`;
  // 仪表盘
  const g = chartEl("short-gauge");
  const color = d.score >= 30 ? UP_C : d.score <= -30 ? DOWN_C : "#f5a623";
  g.setOption({
    ...baseOpt(),
    series: [{
      type: "gauge", min: -100, max: 100, startAngle: 200, endAngle: -20,
      progress: { show: true, width: 14, itemStyle: { color } },
      axisLine: { lineStyle: { width: 14, color: [[1, "#1c2740"]] } },
      axisTick: { show: false }, splitLine: { length: 8, lineStyle: { color: "#2a3a5c" } },
      axisLabel: { color: "#8291b0", fontSize: 10, distance: 18 },
      pointer: { itemStyle: { color }, width: 4 },
      anchor: { show: true, size: 12, itemStyle: { color } },
      detail: { valueAnimation: false, formatter: "{value}", color, fontSize: 30, offsetCenter: [0, "62%"], fontFamily: "Consolas" },
      title: { color: "#8291b0", fontSize: 12, offsetCenter: [0, "88%"] },
      data: [{ value: d.score, name: d.direction + "（" + d.confidence + "）" }],
    }],
  }, true);
  // 因子条形图
  const fc = chartEl("short-factors");
  const f = d.factors;
  fc.setOption({
    ...baseOpt(),
    tooltip: { ...baseOpt().tooltip, formatter: (p) => `${p.name}：加权 ${p.value} 分<br/>${f[p.dataIndex].detail}` },
    grid: { left: 90, right: 40, top: 10, bottom: 24 },
    xAxis: { type: "value", min: -20, max: 20, axisLabel: { fontSize: 10 } },
    yAxis: { type: "category", data: f.map((x) => x.name).reverse(), axisLabel: { fontSize: 11 } },
    series: [{
      type: "bar", barMaxWidth: 16,
      data: f.map((x) => x.weighted).reverse().map((v) => ({ value: v, itemStyle: { color: v >= 0 ? UP_C : DOWN_C } })),
      label: { show: true, position: "right", formatter: (p) => (p.value > 0 ? "+" : "") + p.value, fontSize: 10, color: "#8291b0" },
    }],
  }, true);
  // 因子明细
  $("#short-factors-list").innerHTML = f.map((x) =>
    `<tr><td>${x.name}</td><td><span class="chip ${x.score > 0 ? "多" : x.score < 0 ? "空" : "中性"}">${x.weighted > 0 ? "+" : ""}${x.weighted}分</span></td>
     <td style="text-align:left">${x.detail}</td><td class="muted">权重${x.weight}%</td></tr>`).join("");
  renderKlineChart("short-kline", d.recent_kline, { zoomPct: [0, 100] });
  loadAIBox("short", "short-ai", code);
}

/* ================= 市场热度 ================= */
async function loadHeat() {
  const d = await api("/api/market/heat");
  state.heatData = d;
  const levelColor = { "冰点": "#4f8bff", "偏冷": "#22c58b", "中性": "#f5d423", "偏热": "#f5a623", "过热": "#f04a5c" }[d.level];
  $("#heat-head").innerHTML = `
    <div class="card"><h3>热度等级</h3><div class="big" style="color:${levelColor}">${d.level}</div><div class="sub">${d.date} 全市场情绪量化</div></div>
    <div class="card"><h3>两市成交额</h3><div class="big">${Number(d.breadth.total_amount_yi).toLocaleString()} 亿</div><div class="sub">资金活跃度是热度的燃料</div></div>
    <div class="card"><h3>涨停 / 跌停</h3><div class="big"><span class="up">${d.breadth.limit_up}</span> / <span class="down">${d.breadth.limit_down}</span></div><div class="sub">涨停温度计</div></div>
    <div class="card"><h3>上涨 / 下跌</h3><div class="big"><span class="up">${d.breadth.up}</span> / <span class="down">${d.breadth.down}</span></div><div class="sub">平盘 ${d.breadth.flat} 家</div></div>`;
  // 仪表盘
  const g = chartEl("heat-gauge");
  g.setOption({
    ...baseOpt(),
    series: [{
      type: "gauge", min: 0, max: 100, startAngle: 200, endAngle: -20,
      progress: { show: true, width: 14, itemStyle: { color: levelColor } },
      axisLine: { lineStyle: { width: 14, color: [[1, "#1c2740"]] } },
      axisTick: { show: false }, splitLine: { length: 8, lineStyle: { color: "#2a3a5c" } },
      axisLabel: { color: "#8291b0", fontSize: 10, distance: 18 },
      pointer: { itemStyle: { color: levelColor }, width: 4 },
      anchor: { show: true, size: 12, itemStyle: { color: levelColor } },
      detail: { color: levelColor, fontSize: 30, offsetCenter: [0, "62%"], fontFamily: "Consolas" },
      title: { color: "#8291b0", fontSize: 12, offsetCenter: [0, "88%"] },
      data: [{ value: d.score, name: "市场热度：" + d.level }],
    }],
  }, true);
  // 指数卡
  $("#heat-indices").innerHTML = d.indices.map((i) => `
    <div class="card"><h3>${i.name}</h3>
      <div class="big ${pctCls(i.pct)}" style="font-size:20px">${fmt(i.value)}</div>
      <div class="sub ${pctCls(i.pct)}"><b>${pctText(i.pct)}</b>（${i.chg > 0 ? "+" : ""}${fmt(i.chg)}）</div></div>`).join("");
  // 广度条
  const b = d.breadth, tot = Math.max(b.up + b.down + b.flat, 1);
  $("#heat-breadth").innerHTML = `
    <div class="breadth-bar">
      <div class="bu" style="width:${b.up / tot * 100}%">${b.up}</div>
      <div class="bf" style="width:${b.flat / tot * 100}%">${b.flat}</div>
      <div class="bd" style="width:${b.down / tot * 100}%">${b.down}</div>
    </div>
    <div class="muted" style="font-size:12px;margin-top:8px">上涨占比 ${fmt(b.up / tot * 100, 1)}% —— ${b.up / tot > 0.6 ? "普涨格局，赚钱效应好" : b.up / tot < 0.4 ? "普跌格局，防御为主" : "涨跌互现，结构分化"}</div>`;
  // 分项
  $("#heat-components").innerHTML = d.components.map((c) => `
    <div style="margin-bottom:14px">
      <div style="display:flex;justify-content:space-between;font-size:13px">
        <span>${c.name}</span><b>${c.score}分</b>
      </div>
      <div class="progress"><div style="width:${c.score}%"></div></div>
      <div class="muted" style="font-size:12px;margin-top:4px">${c.desc}</div>
    </div>`).join("");
  // 热度趋势
  if (d.history && d.history.length > 1) {
    const hc = chartEl("heat-history");
    hc.setOption({
      ...baseOpt(),
      tooltip: { ...baseOpt().tooltip },
      grid: { left: 44, right: 20, top: 20, bottom: 30 },
      xAxis: { type: "category", data: d.history.map((h) => h.date.slice(5)) },
      yAxis: { min: 0, max: 100 },
      series: [{
        type: "line", data: d.history.map((h) => h.score), smooth: true,
        lineStyle: { color: ACCENT, width: 2 }, symbolSize: 5,
        areaStyle: { color: "rgba(79,139,255,.15)" },
        markLine: { silent: true, symbol: "none", lineStyle: { color: "#3a4a6e", type: "dashed" },
          data: [{ yAxis: 60, label: { formatter: "偏热线", fontSize: 10 } }, { yAxis: 40, label: { formatter: "偏冷线", fontSize: 10 } }] },
      }],
    }, true);
  }
  // 涨跌榜
  const rows = (arr) => arr.map((s) =>
    `<tr><td>${s.name}</td><td>${s.code}</td><td class="${pctCls(s.pct)}"><b>${pctText(s.pct)}</b></td><td>${fmt(s.price)}</td></tr>`).join("");
  $("#heat-tops").innerHTML = `
    <div class="card"><h3>涨幅榜 TOP10</h3><table class="tbl"><tbody>${rows(d.top_gainers)}</tbody></table></div>
    <div class="card"><h3>跌幅榜 TOP10</h3><table class="tbl"><tbody>${rows(d.top_losers)}</tbody></table></div>`;
  loadAIBox("heat", "heat-ai");
}

/* ================= AI 分析师 ================= */
const escHtml = (s) => String(s === null || s === undefined ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

function agentStepsHtml(steps) {
  if (!steps || !steps.length) return "";
  const rows = steps.map((s) => {
    const icon = s.type === "tool" ? (s.ok ? "🔧" : "⚠️")
      : s.type === "review" ? "🧪" : s.type === "final" ? "✅" : "•";
    let title = s.type === "tool" ? `调用 ${s.tool}`
      : s.type === "final" ? "形成结论"
      : s.type === "review" ? "自我复核"
      : s.type === "error" ? "异常" : "思考";
    if (s.type === "tool") title += `（${s.seconds ?? "?"}s）`;
    const text = s.text ? escHtml(s.text) : "";
    const res = s.type === "tool" && s.result ? `<pre class="agent-pre">${escHtml(s.result)}</pre>` : "";
    const issues = (s.issues && s.issues.length)
      ? `<div class="agent-issues">⚠️ 复核发现：${s.issues.map(escHtml).join("；")}</div>` : "";
    return `<li><span class="ico">${icon}</span><div><b>${escHtml(title)}</b>${text ? ` — ${text}` : ""}${issues}${res}</div></li>`;
  }).join("");
  return `<details class="agent-steps"><summary>🧠 思考过程（${steps.length} 步）</summary><ul>${rows}</ul></details>`;
}

function agentMeta(d) {
  const conf = typeof d.confidence === "number" ? d.confidence : null;
  const cls = conf === null ? "" : conf >= 0.7 ? "up" : conf >= 0.45 ? "" : "down";
  const mem = (d.memory && d.memory.length) ? ` · 记忆命中 ${d.memory.length} 条` : "";
  const tools = (d.tools && d.tools.length) ? ` · 调用 ${d.tools.map((t) => t.tool).join(" → ")}` : "";
  return `<div class="muted" style="font-size:11px;margin-top:8px">— ${escHtml(d.engine || "自主智能体")}`
    + (d.model ? ` · ${escHtml(d.model)}` : "")
    + (conf !== null ? ` · 置信度 <b class="${cls}">${(conf * 100).toFixed(0)}%</b>` : "")
    + mem + tools + `</div>`;
}

const MEM_KIND = { conclusion: "结论", note: "标的笔记", watch: "自主观察" };

async function loadAgentMemory() {
  const box = $("#mem-list"), sum = $("#mem-sum");
  if (!box) return;
  box.style.display = "block";
  box.innerHTML = '<div class="muted">读取中…</div>';
  try {
    const d = await api("/api/agent/memory?limit=50");
    // 自主观察有独立卡片，这里只展示结论与标的笔记
    const items = (d.items || []).filter((m) => m.kind !== "watch");
    if (sum) sum.textContent = items.length ? `共 ${items.length} 条` : "暂无记忆";
    if (!items.length) {
      box.innerHTML = '<div class="muted">还没有记忆。开启「深度思考」提一次问，就会开始积累结论与偏好。</div>';
      return;
    }
    box.innerHTML = items.map((m) => {
      const v = m.value && typeof m.value === "object" ? m.value : {};
      const q = v.q ? escHtml(v.q) : (typeof m.value === "string" ? escHtml(m.value.slice(0, 120)) : "");
      const a = v.a ? escHtml(String(v.a).slice(0, 180)) : "";
      const t = m.ts ? new Date(m.ts * 1000).toLocaleString("zh-CN", { hour12: false }) : "";
      const kind = MEM_KIND[m.kind] || m.kind;
      return `<div class="mem-item"><span class="mem-tag">${escHtml(kind)}</span>`
        + `<b>${q}</b>${a ? `<div class="muted">${a}</div>` : ""}`
        + `<div class="muted" style="font-size:11px">${escHtml(m.key || "")} ${t}</div></div>`;
    }).join("");
  } catch (e) {
    box.innerHTML = `<div class="muted">读取失败：${escHtml(e.message)}</div>`;
  }
}

async function clearAgentMemory() {
  if (!confirm("确定清空长期记忆（结论与标的笔记，保留自主观察记录）？")) return;
  try {
    await api("/api/agent/memory", { method: "DELETE" });
    await loadAgentMemory();
  } catch (e) { alert("清空失败：" + e.message); }
}

async function loadAgentWatch() {
  const box = $("#agent-watch-list");
  if (!box) return;
  try {
    const d = await api("/api/agent/watch?limit=10");
    const items = d.items || [];
    const head = d.last_text ? `<div style="margin-bottom:8px"><b>最近一次：</b>${escHtml(d.last_text)}</div>` : "";
    const rows = items.map((m) => {
      const v = m.value || {};
      const t = v.ts ? new Date(v.ts * 1000).toLocaleString("zh-CN", { hour12: false }) : "";
      return `<div class="mem-item"><span class="mem-tag">自主观察</span>${escHtml(String(v.text || ""))}`
        + `<div class="muted" style="font-size:11px">${t}</div></div>`;
    }).join("");
    box.innerHTML = head + (rows || '<div class="muted">暂无自主观察记录（服务启动约 1 分钟后生成首条，之后每 30 分钟一次）。</div>');
  } catch (e) {
    box.innerHTML = `<div class="muted">读取失败：${escHtml(e.message)}</div>`;
  }
}

async function runAgentWatch(btn) {
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = "扫描中…";
  try {
    await api("/api/agent/watch/run", { method: "POST" });
    await loadAgentWatch();
  } catch (e) {
    alert("扫描失败：" + e.message);
  } finally {
    btn.disabled = false; btn.textContent = old;
  }
}


function pushMsg(role, html) {
  state.chat.push({ role, html });
  const box = $("#chatMsgs");
  const div = document.createElement("div");
  div.className = "msg " + (role === "user" ? "user" : "bot");
  div.innerHTML = html;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

async function sendChat(text) {
  const q = (text || $("#chatInput").value).trim();
  if (!q) return;
  $("#chatInput").value = "";
  pushMsg("user", q);
  const useAgent = !!($("#agentToggle") && $("#agentToggle").checked);
  const loadingId = "load" + Date.now();
  pushMsg("bot", `<span class="ai-loading" id="${loadingId}"><span class="spin"></span>`
    + (useAgent ? "智能体自主推理中（多步取证，可能需要 20–60 秒）…" : "AI 分析师思考中…") + `</span>`);
  try {
    if (useAgent) {
      const d = await api("/api/ai/agent", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q, code: state.code, deep: true }),
      });
      const el = document.getElementById(loadingId);
      el.parentElement.innerHTML = `<div class="ai-md">${mdToHtml(d.answer || "")}</div>`
        + agentMeta(d) + agentStepsHtml(d.steps);
      return;
    }
    const d = await api("/api/ai/ask", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q, code: state.code }),
    });
    // 后端从问题中自动识别出了新标的时，同步切换上下文（全市场扫描不切换）
    if (d.code && d.code !== state.code) {
      state.code = d.code; state.name = d.name || d.code;
      const cs = $("#chatStock"); if (cs) cs.textContent = d.code + " " + (d.name || "");
    }
    const who = d.type === "scan" ? "全市场动量扫描"
      : d.type === "limitup" ? "涨停连板扫描"
      : d.type === "trade" ? "买卖点交易计划"
      : d.type === "predict" ? "盘后形态预测"
      : d.type === "demon" ? (d.code ? "妖股研判" : "全市场妖股扫描")
      : (d.code ? `${d.code} ${d.name || ""}` : `${state.code} ${state.name || ""}`);
    const el = document.getElementById(loadingId);
    el.parentElement.innerHTML = `<div class="ai-md">${mdToHtml(d.text)}</div><div class="muted" style="font-size:11px;margin-top:6px">— ${d.engine} · ${who}</div>`;
  } catch (e) {
    const el = document.getElementById(loadingId);
    if (el) el.parentElement.innerHTML = `<span class="muted">请求失败：${e.message}</span>`;
  }
}

function renderQuickChips() {
  const chips = [
    `明天什么股票会上涨？上涨持续多久？`,
    `哪些股票有妖股潜质？能不能涨成妖股？`,
    `哪些股票在连板？能走几个板？什么时候断板？`,
    `预测${state.name || state.code}接下来什么时候涨什么时候跌？能涨多少？`,
    `${state.name || state.code} 什么时候买入？什么时候卖出？`,
    `${state.name || state.code} 能不能涨成妖股？还能涨多久？`,
    `${state.name || state.code} 短期会涨还是会跌？`,
    "当前市场热度怎么样，该加仓还是减仓？",
    "特斯拉股票什么时候会上涨？能涨多少？会不会下跌？",
    "黄金价格还能涨吗？会不会下跌？下跌到多少？",
  ];
  $("#chatQuick").innerHTML = chips.map((c) => `<span class="qchip">${c}</span>`).join("");
  $$("#chatQuick .qchip").forEach((el) => el.addEventListener("click", () => sendChat(el.textContent)));
}

/* ---------------- 全球市场 ---------------- */
const gState = { asset: null, loaded: false };

function pctCls2(v) { return (v || 0) >= 0 ? "up" : "down"; }

async function loadGlobal(force) {
  const wrap = $("#g-overview");
  if (gState.loaded && !force) { if (gState.asset) renderGAsset(); return; }
  wrap.innerHTML = `<div class="muted" style="padding:20px">🌍 正在加载全球行情…</div>`;
  const ov = await api("/api/global/overview");
  const groups = [
    ["🌐 全球指数", ov.indices], ["🛢️ 大宗商品", ov.commodities],
    ["💱 外汇", ov.forex], ["🇺🇸 美股 · 🇭🇰 港股", ov.stocks],
  ];
  wrap.innerHTML = groups.map(([title, arr]) => `
    <div class="card" style="margin-bottom:14px">
      <h3>${title}</h3>
      <div class="g-grid">${(arr || []).map((q) => `
        <div class="g-card" data-cat="${q.cat}" data-sym="${q.symbol}" data-name="${q.name}">
          <div class="g-name">${q.name}</div>
          <div class="g-price">${fmt(q.price)}</div>
          <div class="g-pct ${pctCls2(q.pct)}">${pctText(q.pct || 0)}</div>
        </div>`).join("")}
      </div>
    </div>`).join("");
  wrap.dataset.loaded = "1";
  gState.loaded = true;
  wrap.querySelectorAll(".g-card").forEach((el) =>
    el.addEventListener("click", () => selectGlobalAsset(el.dataset.cat, el.dataset.sym, el.dataset.name)));
  if (gState.asset) renderGAsset();
}

async function selectGlobalAsset(cat, sym, name) {
  gState.asset = { cat, sym, name };
  renderGAsset();
}

async function renderGAsset() {
  const { cat, sym, name } = gState.asset;
  const box = $("#g-asset");
  box.innerHTML = `<div class="card" style="margin-top:14px"><div class="muted" style="padding:16px">正在加载 ${name} 的全历史数据与技术分析…</div></div>`;
  try {
    const ga = await api(`/api/global/asset/${cat}/${sym}`);
    const q = ga.quote || {};
    const daily = ga.daily;
    const sigHtml = daily && daily.signals ? daily.signals.map((s) => {
      const mark = { "多": "🟢", "空": "🔴", "中性": "⚪" }[s.verdict] || "⚪";
      return `<div class="qitem" style="display:block;padding:6px 0"><b>${mark} ${s.name}</b><div class="muted" style="font-size:12px">${s.desc}</div></div>`;
    }).join("") : "";
    const klineErr = ga.kline_error ? `<div class="muted" style="padding:10px 4px;font-size:12px">ℹ️ ${ga.kline_error}</div>` : "";
    box.innerHTML = `
      <div class="cards c4" style="margin-top:14px" id="g-kpis"></div>
      ${ga.kline_error ? "" : `<div class="card" style="margin-top:14px"><h3>${name} 历史K线（${ga.history_days} 个交易日 · ${ga.first_day} 至今）<button class="rng-btn" id="g-full" style="margin-left:12px">全部历史</button><button class="rng-btn" id="g-250" style="margin-left:6px">250日</button><button class="rng-btn active" id="g-120" style="margin-left:6px">120日</button></h3><div id="g-kline" class="chart lg"></div></div>`}
      <div class="cards c2" style="margin-top:14px">
        <div class="card"><h3>多空信号（复用 A 股量化引擎）</h3>${sigHtml || '<div class="muted" style="padding:10px">暂无信号</div>'}</div>
        <div class="card"><h3>关键位与区间</h3><div id="g-levels" style="padding:8px 4px"></div>
          <button class="btn primary" id="g-ask" style="margin-top:14px;width:100%">🤖 向 AI 提问：${name} 什么时候会上涨？能涨多少？会不会下跌？</button></div>
      </div>
      ${klineErr}`;
    // KPI 卡
    const kp = [
      ["最新价", fmt(q.price), pctText(q.pct || 0), pctCls2(q.pct)],
      ["今开 / 昨收", `${fmt(q.open)} / ${fmt(q.prev_close)}`, "", ""],
      ["最高 / 最低", `${fmt(q.high)} / ${fmt(q.low)}`, "", ""],
      ["数据范围", ga.history_days ? `${ga.history_days} 个交易日` : "仅实时", ga.first_day || "", ""],
    ];
    $("#g-kpis").innerHTML = kp.map(([t, v, sub, cls]) => `
      <div class="card kpi"><div class="label">${t}</div><div class="value">${v}</div>
      <div class="${cls}">${sub}</div></div>`).join("");
    // 关键位
    const srs = daily && daily.support_resistance || {};
    const st = daily && daily.stats || {};
    $("#g-levels").innerHTML = [
      ["支撑位", fmt(srs.support)], ["压力位", fmt(srs.resistance)],
      ["5日涨跌", pctText(st.pct_5d || 0)], ["20日涨跌", pctText(st.pct_20d || 0)],
      ["60日涨跌", pctText(st.pct_60d || 0)],
      ["20日最高/最低", `${fmt(st.high_20d)} / ${fmt(st.low_20d)}`],
    ].map(([t, v]) => `<div class="qitem"><span>${t}</span><b>${v}</b></div>`).join("");
    // K线
    if (!ga.kline_error) {
      let range = 120;
      const draw = async (n) => {
        const kl = await api(`/api/global/asset/${cat}/${sym}/kline${n ? "?days=" + n : ""}`);
        renderKlineChart("g-kline", kl.kline, { zoomPct: n ? [60, 100] : [0, 100] });
      };
      $("#g-120").addEventListener("click", () => { range = 120; draw(120); markRangeBtn(120); });
      $("#g-250").addEventListener("click", () => { range = 250; draw(250); markRangeBtn(250); });
      $("#g-full").addEventListener("click", () => { draw(0); markRangeBtn(0); });
      draw(range);
    }
    $("#g-ask").addEventListener("click", () => {
      sendChat(`${name} 什么时候会上涨？上涨多少？什么时候开始上涨？会不会下跌？下跌到多少？`);
    });
  } catch (e) {
    box.innerHTML = `<div class="card" style="margin-top:14px"><div class="muted" style="padding:16px">⚠ 加载失败：${e.message}</div></div>`;
  }
}

function markRangeBtn(n) {
  [["g-120", 120], ["g-250", 250], ["g-full", 0]].forEach(([id, v]) => {
    const el = document.getElementById(id);
    if (el) el.classList.toggle("active", v === n);
  });
}

function initGlobalSearch() {
  const input = $("#gSearchInput"), drop = $("#gSearchDrop");
  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    timer = setTimeout(async () => {
      if (!q) { drop.classList.remove("show"); return; }
      try {
        const data = await api("/api/global/search", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ q }),
        });
        if (!data.results.length) {
          drop.innerHTML = `<div class="search-item"><span class="nm muted">全球目录未匹配「${q}」</span></div>`;
        } else {
          drop.innerHTML = data.results.map((s) =>
            `<div class="search-item" data-cat="${s.cat}" data-sym="${s.symbol}" data-name="${s.name}">
               <span class="nm">${s.name} <span class="cd">${s.cat_name} ${s.symbol}</span></span>
               <span class="${pctCls2(s.pct)}">${fmt(s.price)} ${pctText(s.pct || 0)}</span>
             </div>`).join("");
        }
        drop.classList.add("show");
        drop.querySelectorAll(".search-item[data-cat]").forEach((el) =>
          el.addEventListener("click", () => {
            selectGlobalAsset(el.dataset.cat, el.dataset.sym, el.dataset.name);
            input.value = ""; drop.classList.remove("show");
          }));
      } catch (e) {}
    }, 250);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      const first = drop.querySelector(".search-item[data-cat]");
      if (first) { selectGlobalAsset(first.dataset.cat, first.dataset.sym, first.dataset.name); input.value = ""; drop.classList.remove("show"); }
    }
  });
  document.addEventListener("click", (e) => { if (!e.target.closest("#view-global .toolbar")) drop.classList.remove("show"); });
}

/* ---------------- 多模型引擎面板 ---------------- */
const mState = { models: [], collab: true, primary: null };
let mmPid = null;

async function loadModelsPanel() {
  const grid = $("#modelsGrid");
  try {
    const st = await api("/api/ai/status");
    mState.models = st.models || [];
    mState.collab = !!st.collab;
    mState.primary = st.primary;
    $("#collabToggle").checked = mState.collab;
    const nOn = mState.models.filter((m) => m.configured).length;
    // 未配置模型时默认关闭「自主推理」，避免每次提问都落到"未启用大模型"提示
    const at = $("#agentToggle");
    if (at) at.checked = nOn > 0;
    $("#modelsSummary").textContent = nOn
      ? `已上线 ${nOn} / ${mState.models.length} 家${mState.collab && nOn > 1 ? " · 委员会协作中" : ""}`
      : `${mState.models.length} 家全球模型待接入 · 点击任意卡片配置 API Key 即启用`;
    grid.innerHTML = mState.models.map((m) => `
      <div class="m-card ${m.configured ? "configured" : ""} ${m.network === "blocked" ? "blocked" : ""}" data-pid="${m.id}"
           title="${m.org} · ${m.model}${m.network === "blocked" ? "（本沙箱网络屏蔽，本地部署可用）" : ""}">
        <div class="m-dot"></div>
        <div class="m-meta">
          <div class="m-name">${m.name}${m.primary ? '<span class="m-star">★主</span>' : ""}</div>
          <div class="m-skill">${m.configured ? m.model : m.skill}</div>
        </div>
      </div>`).join("");
    grid.querySelectorAll(".m-card").forEach((el) =>
      el.addEventListener("click", () => openModelModal(el.dataset.pid)));
  } catch (e) {
    grid.innerHTML = `<div class="muted" style="padding:8px">加载失败：${e.message}</div>`;
  }
}

function openModelModal(pid) {
  const m = mState.models.find((x) => x.id === pid);
  if (!m) return;
  mmPid = pid;
  $("#mmTitle").textContent = `配置 ${m.name}（${m.org}）`;
  $("#mmDesc").textContent = `${m.skill} · 默认模型 ${m.model} · ` +
    (m.network === "blocked" ? "⚠ 本沙箱网络屏蔽该端点（配置后本地部署可用）" : "✓ 网络可达");
  $("#mmKey").value = "";
  $("#mmModel").value = "";
  $("#mmPrimary").checked = m.primary;
  $("#modelModal").style.display = "flex";
  $("#mmKey").focus();
}

function initModelsPanel() {
  $("#collabToggle").addEventListener("change", async (e) => {
    try {
      await api("/api/ai/config", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ collab: e.target.checked }) });
      loadModelsPanel();
    } catch (err) { alert("切换失败：" + err.message); }
  });
  $("#mmCancel").addEventListener("click", () => { $("#modelModal").style.display = "none"; });
  $("#modelModal").addEventListener("click", (e) => { if (e.target === $("#modelModal")) $("#modelModal").style.display = "none"; });
  $("#mmSave").addEventListener("click", async () => {
    const key = $("#mmKey").value.trim();
    if (!key) { alert("请输入 API Key"); return; }
    try {
      const st = await api("/api/ai/config", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ providers: [{ pid: mmPid, api_key: key, model: $("#mmModel").value.trim(), primary: $("#mmPrimary").checked }] }) });
      $("#modelModal").style.display = "none";
      const badge = $("#aiBadge");
      badge.classList.toggle("llm", st.configured);
      badge.innerHTML = `<span class="dot"></span>${st.engine} · ${st.base_url}`;
      loadModelsPanel();
    } catch (e) { alert("保存失败：" + e.message); }
  });
}

/* ---------------- 引擎回测验证 ---------------- */
async function runBacktest() {
  const btn = $("#bt-run"), body = $("#bt-body");
  btn.disabled = true;
  body.innerHTML = `<div class="muted" style="padding:14px 4px;font-size:13px">⏳ 回测进行中：在 15 个历史时点用「当时可见」的数据做预测，与之后 20 日真实走势比对（约 10~30 秒）…</div>`;
  try {
    const d = await api(`/api/backtest?code=${state.code}&samples=15`);
    const p = d.pred, f = d.factor, cal = p.calibration;
    const rateCls = p.rate !== null && p.rate >= 55 ? "up" : p.rate !== null && p.rate < 50 ? "down" : "";
    const calDiff = (cal.hi_avg - cal.lo_avg);
    const rateTxt = p.rate === null ? "样本不足" : `${p.dir_hit}/${p.gave_direction}（${p.rate}%）`;
    const neutralInfo = p.neutral_flat === null || p.neutral_flat === undefined ? "—"
      : `弃权后实际走势平淡（|涨跌|≤3%）占 ${p.neutral_flat}%（${p.neutral_ok}/${p.neutral}）`;
    body.innerHTML = `
      <div class="cards c4" style="margin-bottom:12px">
        <div class="card kpi"><div class="label">方向票胜率（引擎开口时）</div><div class="value ${rateCls}" style="font-size:20px">${rateTxt}</div><div class="muted" style="font-size:11px">共 ${p.total} 个时点 · 随机基准 50% · ≥55% 优秀</div></div>
        <div class="card kpi"><div class="label">中性弃权次数</div><div class="value" style="font-size:20px">${p.neutral}</div><div class="muted" style="font-size:11px">${neutralInfo}</div></div>
        <div class="card kpi"><div class="label">概率校准（高低组均收差）</div><div class="value ${calDiff >= 0 ? "up" : "down"}" style="font-size:20px">${calDiff >= 0 ? "+" : ""}${calDiff.toFixed(2)}%</div><div class="muted" style="font-size:11px">高概率组 ${cal.hi_avg}% / 低概率组 ${cal.lo_avg}% · 相关系数 ${cal.corr}</div></div>
        <div class="card kpi"><div class="label">七因子评分有效性</div><div class="value ${f.effective ? "up" : "down"}" style="font-size:20px">${f.effective ? "✓ 有效" : "✗ 无效"}</div><div class="muted" style="font-size:11px">看多信号日均收 ${f.bull_avg}%（胜率${f.bull_win}%）vs 全体 ${f.base_avg}%（${f.base_win}%）</div></div>
      </div>
      <div style="max-height:340px;overflow:auto">
      <table class="tbl"><thead><tr><th>回测时点</th><th>预测</th><th>置信度</th><th>当时20日上涨概率</th><th>实际20日</th><th>判定</th></tr></thead><tbody>
      ${p.detail.map((x) => `<tr><td>${x.date}</td><td>${x.pred}</td><td class="muted" style="font-size:11px">${x.conf || "—"}</td>
        <td>${x.up_prob === null || x.up_prob === undefined ? "—" : x.up_prob + "%"}</td>
        <td class="${x.actual > 0 ? "up" : x.actual < 0 ? "down" : ""}">${x.actual > 0 ? "+" : ""}${x.actual}%</td>
        <td>${x.ok ? "✓" : "✗"}</td></tr>`).join("")}
      </tbody></table></div>
      <div class="muted" style="padding-top:8px;font-size:12px">回测方法：截断信息流（模拟当时只能看到之前的数据）· 判定规则：方向预测按同向计，中性弃权按实际走势平淡计 · 历史回测不代表未来表现</div>`;
  } catch (e) {
    body.innerHTML = `<div class="muted" style="padding:12px 4px;font-size:13px">⚠ 回测失败：${e.message}</div>`;
  } finally {
    btn.disabled = false;
  }
}

/* ---------------- 启动 ---------------- */
/* ================= 基本面 ================= */
async function loadFundamental() {
  const code = state.code;
  $("#fund-loading").style.display = "block";
  $("#fund-content").style.display = "none";
  const d = await api(`/api/stock/${code}/fundamental`);
  if (code !== state.code) return;
  $("#fund-loading").style.display = "none";
  $("#fund-content").style.display = "block";
  const v = d.valuation || {}, f = d.financial || {}, p = d.peer_comparison || {};
  $("#fund-valuation").innerHTML = `
    <div class="card"><h3>PE 市盈率</h3><div class="big">${v.pe ?? '—'}</div><div class="sub">全市场分位 <b class="${pctCls((p.pe_percentile||50)-50)}">${p.pe_percentile ?? '—'}%</b></div></div>
    <div class="card"><h3>PB 市净率</h3><div class="big">${v.pb ?? '—'}</div><div class="sub">全市场分位 <b class="${pctCls((p.pb_percentile||50)-50)}">${p.pb_percentile ?? '—'}%</b></div></div>
    <div class="card"><h3>总市值</h3><div class="big">${v.mktcap_yi ?? '—'}</div><div class="sub">亿元</div></div>
    <div class="card"><h3>换手率</h3><div class="big">${v.turnover ?? '—'}</div><div class="sub">%</div></div>`;
  $("#fund-report-date").textContent = f.report_date ? `（${f.report_date}）` : "";
  const finMap = [["eps","每股收益(元)"],["roe","净资产收益率 ROE(%)"],["roa","总资产收益率 ROA(%)"],["net_margin","销售净利率(%)"],["debt_ratio","资产负债率(%)"],["profit_growth","净利润增速(%)"],["current_ratio","流动比率"],["quick_ratio","速动比率"]];
  $("#fund-financial").innerHTML = finMap.map(([k,n]) => `<div class="kv"><span>${n}</span><b>${f[k] ?? '—'}</b></div>`).join("");
  $("#fund-peer").innerHTML = `
    <div class="kv"><span>PE 全市场中位数</span><b>${p.pe_median ?? '—'}</b></div>
    <div class="kv"><span>PB 全市场中位数</span><b>${p.pb_median ?? '—'}</b></div>
    <div class="kv"><span>PE 分位</span><b>${p.pe_percentile ?? '—'}%</b></div>
    <div class="kv"><span>PB 分位</span><b>${p.pb_percentile ?? '—'}%</b></div>
    <div class="kv"><span>样本数</span><b>${p.sample_size ?? '—'}</b></div>`;
}

/* ================= 多因子 ================= */
async function loadFactors() {
  const code = state.code;
  $("#fac-loading").style.display = "block";
  $("#fac-content").style.display = "none";
  const d = await api(`/api/stock/${code}/factors`);
  if (code !== state.code) return;
  $("#fac-loading").style.display = "none";
  $("#fac-content").style.display = "block";
  const sc = d.composite_score;
  const rating = sc >= 70 ? "优秀" : sc >= 55 ? "良好" : sc >= 40 ? "一般" : "偏弱";
  $("#fac-score").textContent = sc ?? "—";
  $("#fac-score").style.color = sc >= 55 ? "#e74c3c" : sc >= 40 ? "#f39c12" : "#2ecc71";
  $("#fac-rating").textContent = rating;
  // 雷达图
  const cats = [["value","价值"],["growth","成长"],["quality","质量"],["momentum","动量"],["volatility","波动率"]];
  const radarData = cats.map(([k,n]) => d.category_scores[k] ?? 0);
  if (window.facRadar) window.facRadar.dispose();
  window.facRadar = echarts.init(document.getElementById("fac-radar"));
  window.facRadar.setOption({
    radar: { indicator: cats.map(([k,n]) => ({ name: n, max: 100 })) },
    series: [{ type: "radar", data: [{ value: radarData, name: "因子得分", areaStyle: { opacity: 0.3 } }] }],
  });
  // 因子明细
  const rows = [];
  for (const [k, v] of Object.entries(d.factors || {})) {
    const nm = d.factor_names?.[k] || k;
    const pct = d.percentiles?.[k];
    const pctHtml = pct != null ? `<span class="tag">分位 ${pct}%</span>` : "";
    rows.push(`<div class="kv"><span>${nm}</span><b>${fmt(v)} ${pctHtml}</b></div>`);
  }
  $("#fac-detail").innerHTML = rows.join("");
}

/* ================= 策略回测 ================= */
let btStrategy = "mr", btDays = 500;
async function loadBacktest() {
  // 回测页不自动运行，需用户点击按钮；按钮监听器已在 init() 中绑定一次
}
async function runStrategyBacktest() {
  const code = state.code;
  $("#bt-placeholder").style.display = "block";
  $("#bt-placeholder").innerHTML = `<div class="spin"></div> 回测运行中（${btStrategy} · ${btDays}日）…`;
  $("#bt-content").style.display = "none";
  const d = await api(`/api/stock/${code}/backtest/strategy?strategy=${btStrategy}&days=${btDays}`);
  if (d.error) { $("#bt-placeholder").innerHTML = `❌ ${d.error}`; return; }
  $("#bt-placeholder").style.display = "none";
  $("#bt-content").style.display = "block";
  const m = d.metrics;
  $("#bt-metrics").innerHTML = `
    <div class="card"><h3>总收益率</h3><div class="big ${pctCls(m.total_return)}">${m.total_return}%</div><div class="sub">基准 ${m.benchmark_return}% · 超额 ${m.excess_return}%</div></div>
    <div class="card"><h3>年化 CAGR</h3><div class="big ${pctCls(m.cagr)}">${m.cagr}%</div><div class="sub">波动率 ${m.annual_volatility}%</div></div>
    <div class="card"><h3>夏普 / 卡玛</h3><div class="big">${m.sharpe} <span style="font-size:14px">/ ${m.calmar}</span></div><div class="sub">索提诺 ${m.sortino}</div></div>
    <div class="card"><h3>最大回撤</h3><div class="big down">${m.max_drawdown}%</div><div class="sub">持续 ${m.max_dd_duration_days} 日</div></div>
    <div class="card"><h3>胜率</h3><div class="big">${m.win_rate}%</div><div class="sub">${m.trade_count} 笔交易</div></div>
    <div class="card"><h3>盈亏比</h3><div class="big">${m.profit_factor ?? '—'}</div><div class="sub">盈 ${m.avg_win_pct}% / 亏 ${m.avg_loss_pct}%</div></div>
    <div class="card"><h3>平均持仓</h3><div class="big">${m.avg_hold_days}</div><div class="sub">天</div></div>
    <div class="card"><h3>期末净值</h3><div class="big">${fmt(m.final_nav)}</div><div class="sub">初始 ${fmt(m.initial_capital)}</div></div>`;
  // 净值曲线
  const nav = d.nav_curve || [];
  const dates = nav.map(x => x.date);
  const navVals = nav.map(x => x.nav);
  const bench = nav.map((x, i) => m.initial_capital * (x.price / nav[0].price));
  if (window.btNav) window.btNav.dispose();
  window.btNav = echarts.init(document.getElementById("bt-nav"));
  window.btNav.setOption({
    tooltip: { trigger: "axis" },
    legend: { data: ["策略净值", "买入持有"] },
    xAxis: { type: "category", data: dates },
    yAxis: { type: "value" },
    series: [
      { name: "策略净值", type: "line", data: navVals, smooth: true, lineStyle: { width: 2 } },
      { name: "买入持有", type: "line", data: bench, smooth: true, lineStyle: { width: 1, type: "dashed" } },
    ],
  });
  // 交易记录
  const trades = d.trades || [];
  if (trades.length) {
    $("#bt-trades").innerHTML = `<table class="data-table"><thead><tr><th>买入日</th><th>卖出日</th><th>买入价</th><th>卖出价</th><th>持股</th><th>盈亏%</th><th>盈亏额</th></tr></thead><tbody>${
      trades.map(t => `<tr><td>${t.buy_date}</td><td>${t.sell_date}</td><td>${t.buy_price}</td><td>${t.sell_price}</td><td>${t.shares}</td><td class="${pctCls(t.pnl_pct)}">${t.pnl_pct}%</td><td class="${pctCls(t.pnl)}">${fmt(t.pnl)}</td></tr>`).join("")
    }</tbody></table>`;
  } else {
    $("#bt-trades").innerHTML = '<div class="muted">回测期间无交易</div>';
  }
  $("#bt-disclaimer").textContent = d.disclaimer || "";
}

/* ================= 组合回测 ================= */
let pbtStrategy = "factor", pbtDays = 500, pbtRebalance = 20, pbtWeight = "equal";
async function loadPortfolioBacktest() {
  // 组合回测页不自动运行，需用户点击按钮；监听器已在 init() 中绑定一次
}
async function runPortfolioBacktest() {
  $("#pbt-placeholder").style.display = "block";
  $("#pbt-placeholder").innerHTML = `<div class="spin"></div> 组合回测运行中（${pbtStrategy} · ${pbtDays}日 · ${pbtRebalance}日再平衡）…`;
  $("#pbt-content").style.display = "none";
  const d = await api(`/api/portfolio/backtest?strategy=${pbtStrategy}&days=${pbtDays}&rebalance=${pbtRebalance}&weighting=${pbtWeight}&max_holdings=10&max_industry_weight=0.35`);
  if (d.error) { $("#pbt-placeholder").innerHTML = `❌ ${d.error}`; return; }
  $("#pbt-placeholder").style.display = "none";
  $("#pbt-content").style.display = "block";
  const m = d.metrics;
  $("#pbt-metrics").innerHTML = `
    <div class="card"><h3>总收益率</h3><div class="big ${pctCls(m.total_return)}">${m.total_return}%</div><div class="sub">等权基准 ${m.benchmark_return}% · 超额 ${m.excess_return}%</div></div>
    <div class="card"><h3>年化 CAGR</h3><div class="big ${pctCls(m.cagr)}">${m.cagr}%</div><div class="sub">波动率 ${m.annual_volatility}%</div></div>
    <div class="card"><h3>夏普 / 卡玛</h3><div class="big">${m.sharpe} <span style="font-size:14px">/ ${m.calmar}</span></div><div class="sub">索提诺 ${m.sortino}</div></div>
    <div class="card"><h3>最大回撤</h3><div class="big down">${m.max_drawdown}%</div><div class="sub">持续 ${m.max_dd_duration_days} 日</div></div>
    <div class="card"><h3>胜率 / 盈亏比</h3><div class="big">${m.win_rate}%</div><div class="sub">${m.trade_count} 笔平仓 · 盈亏比 ${m.profit_factor ?? '—'}</div></div>
    <div class="card"><h3>平均持仓</h3><div class="big">${m.avg_holdings}</div><div class="sub">只 · 调仓 ${m.rebalance_count} 次</div></div>
    <div class="card"><h3>年化换手</h3><div class="big">${m.turnover_annual}</div><div class="sub">倍 · 交易成本 ${fmt(m.total_cost)} 元</div></div>
    <div class="card"><h3>期末净值</h3><div class="big">${fmt(m.final_nav)}</div><div class="sub">初始 ${fmt(m.initial_capital)}</div></div>`;
  // 净值曲线
  const nav = d.nav_curve || [], bench = d.bench_curve || [];
  if (window.pbtNav) window.pbtNav.dispose();
  window.pbtNav = echarts.init(document.getElementById("pbt-nav"));
  window.pbtNav.setOption({
    tooltip: { trigger: "axis" },
    legend: { data: ["组合净值", "等权基准"] },
    xAxis: { type: "category", data: nav.map(x => x.date) },
    yAxis: { type: "value", scale: true },
    series: [
      { name: "组合净值", type: "line", data: nav.map(x => x.nav), smooth: true, lineStyle: { width: 2 } },
      { name: "等权基准", type: "line", data: bench.map(x => x.nav), smooth: true, lineStyle: { width: 1, type: "dashed" } },
    ],
  });
  // 期末持仓
  const holdings = d.holdings || [];
  if (holdings.length) {
    $("#pbt-holdings").innerHTML = `<table class="data-table"><thead><tr><th>代码</th><th>名称</th><th>行业</th><th>权重</th><th>成本</th><th>现价</th><th>盈亏%</th></tr></thead><tbody>${
      holdings.map(h => `<tr><td>${h.code}</td><td>${h.name}</td><td style="font-size:11px">${h.industry}</td><td>${h.weight}%</td><td>${h.cost}</td><td>${h.last}</td><td class="${pctCls(h.pnl_pct)}">${h.pnl_pct}%</td></tr>`).join("")
    }</tbody></table>`;
  } else {
    $("#pbt-holdings").innerHTML = '<div class="muted">期末空仓</div>';
  }
  // 行业暴露
  const ind = Object.entries(d.industry_exposure || {});
  if (window.pbtInd) window.pbtInd.dispose();
  window.pbtInd = echarts.init(document.getElementById("pbt-industry"));
  window.pbtInd.setOption({
    tooltip: { trigger: "item", formatter: "{b}: {c}%" },
    series: [{ type: "pie", radius: ["35%", "68%"], data: ind.map(([k, v]) => ({ name: k, value: v })), label: { fontSize: 10 } }],
  });
  // 交易记录
  const trades = d.trades || [];
  if (trades.length) {
    $("#pbt-trades").innerHTML = `<table class="data-table"><thead><tr><th>日期</th><th>代码</th><th>名称</th><th>方向</th><th>原因</th><th>价格</th><th>股数</th><th>金额</th><th>盈亏%</th></tr></thead><tbody>${
      trades.map(t => `<tr><td>${t.date}</td><td>${t.code}</td><td>${t.name}</td><td class="${t.side === 'buy' ? 'up' : 'down'}">${t.side === 'buy' ? '买入' : '卖出'}</td><td>${t.reason || ''}</td><td>${t.price}</td><td>${t.shares}</td><td>${fmt(t.amount)}</td><td class="${pctCls(t.pnl_pct)}">${t.pnl_pct == null ? '—' : t.pnl_pct + '%'}</td></tr>`).join("")
    }</tbody></table>`;
  } else {
    $("#pbt-trades").innerHTML = '<div class="muted">回测期间无交易</div>';
  }
  $("#pbt-disclaimer").textContent = d.disclaimer || "";
}

/* ================= 盯盘信号 ================= */
let watchStrategy = "factor";
async function loadWatch() {
  $("#watch-actions").innerHTML = '<div class="muted">点击「▶ 生成今日信号」，按所选策略计算今日目标组合与买卖建议</div>';
  await Promise.all([refreshHoldings(), refreshMonitor(), loadAgentWatch()]);
}

async function refreshHoldings() {
  let d;
  try { d = await api("/api/holdings"); } catch (e) { d = { holdings: [], count: 0, total_value: 0 }; }
  const rows = d.holdings || [];
  if (!rows.length) {
    $("#watch-holdings").innerHTML = '<div class="muted">尚未录入持仓，可在下方添加（代码/股数/成本价）</div>';
    return;
  }
  $("#watch-holdings").innerHTML = `<table class="data-table"><thead><tr><th>代码</th><th>名称</th><th>股数</th><th>成本</th><th>现价</th><th>盈亏%</th><th>权重</th><th></th></tr></thead><tbody>${
    rows.map(h => `<tr><td>${h.code}</td><td>${h.name}</td><td>${h.shares}</td><td>${fmt(h.cost)}</td><td>${fmt(h.price)}</td>
      <td class="${pctCls(h.pnl_pct)}">${h.pnl_pct == null ? '—' : pctText(h.pnl_pct)}</td><td>${h.weight}%</td>
      <td><a href="javascript:void(0)" onclick="removeHolding('${h.code}')" style="color:#c0392b">删除</a></td></tr>`).join("")
  }</tbody></table><div class="muted" style="font-size:12px;margin-top:6px">总市值 ${fmt(d.total_value)} 元 · ${d.count} 只</div>`;
}

async function refreshMonitor() {
  let m;
  try { m = await api("/api/signals/monitor"); } catch (e) { m = { alerts: [] }; }
  const alerts = m.alerts || [];
  if (!alerts.length) {
    $("#watch-alerts").innerHTML = m.note ? `<div class="muted">${m.note}</div>` : '<div class="muted">暂无预警</div>';
    return;
  }
  const color = { "高": "#c0392b", "中": "#e67e22", "低": "#7f8c8d" };
  $("#watch-alerts").innerHTML = alerts.map(a => `<div class="kv" style="align-items:flex-start">
    <span><span class="tag" style="background:${color[a.level] || '#999'};color:#fff">${a.level}</span> ${a.name || a.code}</span>
    <b style="font-weight:500;text-align:right;max-width:60%">${a.message}</b></div>`).join("");
}

async function runWatchSignals() {
  $("#watch-actions").innerHTML = '<div class="spin"></div> 信号计算中…';
  let d;
  try { d = await api(`/api/signals/daily?strategy=${watchStrategy}&top=10`); }
  catch (e) { $("#watch-actions").innerHTML = `❌ ${e.message}`; return; }
  if (d.error) { $("#watch-actions").innerHTML = `❌ ${d.error}`; return; }
  const s = d.summary || {};
  $("#watch-summary").innerHTML = `
    <div class="card"><h3>🎯 目标组合</h3><div class="big">${d.target_count} <span style="font-size:14px">只</span></div><div class="sub">数据日期 ${d.date || '—'} · 候选池 ${d.universe_count}</div></div>
    <div class="card"><h3>📋 操作建议</h3><div class="big">买 ${s.buy || 0} / 卖 ${s.sell || 0}</div><div class="sub">持有 ${s.hold || 0} 只</div></div>
    <div class="card"><h3>💼 当前持仓</h3><div class="big">${d.holdings_count} <span style="font-size:14px">只</span></div><div class="sub">策略：${watchStrategy}</div></div>`;
  const acts = d.actions || [];
  if (!acts.length) { $("#watch-actions").innerHTML = '<div class="muted">无操作建议</div>'; return; }
  const ac = { "买入": "up", "卖出": "down", "持有": "" };
  $("#watch-actions").innerHTML = `<table class="data-table"><thead><tr><th>动作</th><th>代码</th><th>名称</th><th>行业</th><th>评分</th><th>现价</th><th>目标权重</th><th>理由</th></tr></thead><tbody>${
    acts.map(a => `<tr><td class="${ac[a.action] || ''}" style="font-weight:600">${a.action}</td><td>${a.code}</td><td>${a.name}</td>
      <td style="font-size:11px">${a.industry || '—'}</td><td>${fmt(a.score, 1)}</td><td>${fmt(a.price)}</td><td>${a.target_weight || 0}%</td>
      <td style="font-size:11px">${a.reason}</td></tr>`).join("")
  }</tbody></table>`;
}

async function runWatchBrief() {
  $("#watch-brief-text").textContent = "生成中…";
  try {
    const d = await api(`/api/signals/brief?strategy=${watchStrategy}&top=10`);
    $("#watch-brief-text").textContent = d.error || d.text || "";
  } catch (e) { $("#watch-brief-text").textContent = "❌ " + e.message; }
}

async function addHolding() {
  const code = ($("#hold-code").value || "").trim();
  const shares = parseInt($("#hold-shares").value || "0", 10);
  const cost = parseFloat($("#hold-cost").value || "0");
  if (!code || !shares) { $("#push-result").textContent = "请填写代码与股数"; return; }
  await api("/api/holdings/upsert", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code, shares, cost }),
  });
  $("#hold-code").value = $("#hold-shares").value = $("#hold-cost").value = "";
  await Promise.all([refreshHoldings(), refreshMonitor()]);
}

async function removeHolding(code) {
  await api(`/api/holdings/${code}`, { method: "DELETE" });
  await Promise.all([refreshHoldings(), refreshMonitor()]);
}
window.removeHolding = removeHolding;

async function clearHoldings() {
  await api("/api/holdings", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items: [] }),
  });
  await Promise.all([refreshHoldings(), refreshMonitor()]);
}

async function pushBrief() {
  const webhook = ($("#push-webhook").value || "").trim();
  $("#push-result").textContent = "推送中…";
  try {
    const d = await api("/api/signals/push", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ webhook, strategy: watchStrategy, top: 10 }),
    });
    $("#push-result").textContent = d.ok ? `✅ 推送成功（HTTP ${d.status}）` : `⚠️ ${d.reason}`;
  } catch (e) { $("#push-result").textContent = "❌ " + e.message; }
}

/* ================= 风险归因 ================= */
async function loadPortfolioRisk() {
  // 需用户点击按钮，监听器已在 init() 中绑定一次
}
function prShow(which) {
  $("#pr-placeholder").style.display = "none";
  $("#pr-content").style.display = "block";
  ["risk", "attr", "mix", "grid"].forEach(k => {
    const el = $("#pr-" + k);
    if (el) el.style.display = k === which ? "block" : "none";
  });
}
function prBusy(text) {
  $("#pr-placeholder").style.display = "block";
  $("#pr-placeholder").innerHTML = `<div class="spin"></div> ${text}…`;
  $("#pr-content").style.display = "none";
}
function riskQuery() {
  const codes = ($("#risk-codes").value || "").trim();
  const days = parseInt($("#risk-days").value || "250", 10) || 250;
  return { codes, days };
}

async function runRiskAnalysis() {
  const { codes, days } = riskQuery();
  prBusy("风险模型计算中");
  let d;
  try { d = await api(`/api/portfolio/risk?codes=${encodeURIComponent(codes)}&days=${days}`); }
  catch (e) { $("#pr-placeholder").innerHTML = `❌ ${e.message}`; return; }
  if (d.error) { $("#pr-placeholder").innerHTML = `❌ ${d.error}`; return; }
  prShow("risk");
  const r = d.risk;
  $("#risk-metrics").innerHTML = `
    <div class="card"><h3>年化波动率</h3><div class="big">${r.annual_volatility}%</div><div class="sub">Beta ${r.beta ?? '—'}</div></div>
    <div class="card"><h3>VaR 95%（1日）</h3><div class="big down">${r.var95_pct}%</div><div class="sub">约 ${fmt(r.var95_amount)} 元</div></div>
    <div class="card"><h3>最大回撤</h3><div class="big down">${r.max_drawdown}%</div><div class="sub">压力测试 ${d.stress_test.worst_20d ?? '—'}%（20日最差）</div></div>
    <div class="card"><h3>历史 VaR / ES</h3><div class="big">${r.historical_var95_pct}%</div><div class="sub">ES ${r.expected_shortfall95_pct}%</div></div>
    <div class="card"><h3>组合 HHI</h3><div class="big">${r.portfolio_hhi}</div><div class="sub">单票最大 ${r.max_single_weight}%</div></div>
    <div class="card"><h3>区间年化</h3><div class="big ${pctCls(r.cagr)}">${r.cagr}%</div><div class="sub">夏普 ${r.sharpe}</div></div>`;
  // 行业暴露
  const ind = Object.entries(d.industry_exposure || {});
  if (window.riskInd) window.riskInd.dispose();
  window.riskInd = echarts.init(document.getElementById("risk-industry"));
  window.riskInd.setOption({
    tooltip: { trigger: "item", formatter: "{b}: {c}%" },
    series: [{ type: "pie", radius: ["35%", "68%"], data: ind.map(([k, v]) => ({ name: k, value: v })), label: { fontSize: 10 } }],
  });
  // 风险贡献与风险平价
  const rp = {};
  (d.risk_parity_weights || []).forEach(x => rp[x.code] = x.weight);
  $("#risk-contrib").innerHTML = `<table class="data-table"><thead><tr><th>代码</th><th>当前权重</th><th>风险贡献</th><th>风险平价建议</th></tr></thead><tbody>${
    (d.risk_contribution || []).map(x => `<tr><td>${x.code}</td><td>${x.weight}%</td><td>${x.risk_contrib}%</td><td>${rp[x.code] ?? '—'}%</td></tr>`).join("")
  }</tbody></table>`;
  // 风格暴露
  const styleNames = { value: "价值", growth: "成长", quality: "质量", momentum: "动量", low_vol: "低波动" };
  $("#risk-style").innerHTML = Object.entries(styleNames).map(([k, n]) => {
    const v = d.style_exposure?.[k];
    return `<div class="kv"><span>${n}</span><b>${v == null ? '—' : v + ' 分位'}</b></div>`;
  }).join("");
  // 相关性 / 压力
  const corr = (d.top_correlations || []).slice(0, 5);
  $("#risk-stress").innerHTML = `<div class="kv"><span>最差5日</span><b class="down">${d.stress_test.worst_5d ?? '—'}%</b></div>
    <div class="kv"><span>最差20日</span><b class="down">${d.stress_test.worst_20d ?? '—'}%</b></div>
    <div class="kv"><span>近20日波动率</span><b>${d.stress_test.vol_regime.recent_20d ?? '—'}%</b></div>
    ${corr.map(c => `<div class="kv"><span>相关 ${c.a} ↔ ${c.b}</span><b>${c.corr}</b></div>`).join("")}`;
}

async function runAttribution() {
  const { codes, days } = riskQuery();
  prBusy("收益归因计算中");
  let d;
  try { d = await api(`/api/portfolio/attribution?codes=${encodeURIComponent(codes)}&days=${Math.min(days, 250)}`); }
  catch (e) { $("#pr-placeholder").innerHTML = `❌ ${e.message}`; return; }
  if (d.error) { $("#pr-placeholder").innerHTML = `❌ ${d.error}`; return; }
  prShow("attr");
  const s = d.summary;
  $("#attr-summary").innerHTML = `
    <div class="card"><h3>组合收益</h3><div class="big ${pctCls(d.portfolio_return)}">${d.portfolio_return}%</div><div class="sub">${d.period.start} ~ ${d.period.end}</div></div>
    <div class="card"><h3>基准收益</h3><div class="big ${pctCls(d.benchmark.return_pct)}">${d.benchmark.return_pct}%</div><div class="sub">${d.benchmark.name}（${d.benchmark.count}只）</div></div>
    <div class="card"><h3>主动收益</h3><div class="big ${pctCls(d.active_return)}">${d.active_return}%</div><div class="sub">组合 - 基准</div></div>
    <div class="card"><h3>归因分解</h3><div class="big" style="font-size:20px">配置 ${s.allocation}</div><div class="sub">选股 ${s.selection} · 交互 ${s.interaction}</div></div>`;
  $("#attr-industry").innerHTML = `<table class="data-table"><thead><tr><th>行业</th><th>组合权重</th><th>基准权重</th><th>组合收益</th><th>基准收益</th><th>配置</th><th>选股</th><th>交互</th><th>合计</th></tr></thead><tbody>${
    (d.industry_attribution || []).map(r => `<tr><td>${r.industry}</td><td>${r.portfolio_weight}%</td><td>${r.benchmark_weight}%</td>
      <td class="${pctCls(r.portfolio_return)}">${r.portfolio_return}%</td><td class="${pctCls(r.benchmark_return)}">${r.benchmark_return}%</td>
      <td class="${pctCls(r.allocation)}">${r.allocation}</td><td class="${pctCls(r.selection)}">${r.selection}</td>
      <td class="${pctCls(r.interaction)}">${r.interaction}</td><td class="${pctCls(r.total)}"><b>${r.total}</b></td></tr>`).join("")
  }</tbody></table>`;
  $("#attr-stock").innerHTML = `<table class="data-table"><thead><tr><th>代码</th><th>名称</th><th>行业</th><th>权重</th><th>区间收益</th><th>贡献</th></tr></thead><tbody>${
    (d.stock_contribution || []).map(x => `<tr><td>${x.code}</td><td>${x.name}</td><td style="font-size:11px">${x.industry}</td>
      <td>${x.weight}%</td><td class="${pctCls(x.return_pct)}">${x.return_pct}%</td><td class="${pctCls(x.contribution)}">${x.contribution}</td></tr>`).join("")
  }</tbody></table>`;
}

async function runMixOptimize() {
  prBusy("多策略回测与合成中（约需数秒）");
  let d;
  try { d = await api("/api/optimize/mix?strategies=factor,momentum,tb,mr&days=500&max_holdings=10"); }
  catch (e) { $("#pr-placeholder").innerHTML = `❌ ${e.message}`; return; }
  if (d.error) { $("#pr-placeholder").innerHTML = `❌ ${d.error}`; return; }
  prShow("mix");
  const m = d.methods;
  $("#mix-metrics").innerHTML = Object.entries(m).map(([k, v]) => `
    <div class="card"><h3>${v.name}</h3><div class="big ${pctCls(v.metrics.total_return)}">${v.metrics.total_return}%</div>
      <div class="sub">夏普 ${v.metrics.sharpe} · 回撤 ${v.metrics.max_drawdown}%${v.cash_weight ? ` · 现金 ${v.cash_weight}%` : ''}</div></div>`).join("");
  if (window.mixNav) window.mixNav.dispose();
  window.mixNav = echarts.init(document.getElementById("mix-nav"));
  window.mixNav.setOption({
    tooltip: { trigger: "axis" },
    legend: { data: Object.values(m).map(v => v.name) },
    xAxis: { type: "category", data: d.dates },
    yAxis: { type: "value", scale: true },
    series: Object.values(m).map(v => ({ name: v.name, type: "line", data: v.nav, smooth: true })),
  });
  const best = Object.entries(m).sort((a, b) => b[1].metrics.sharpe - a[1].metrics.sharpe)[0];
  $("#mix-detail").innerHTML = `<div class="muted" style="margin-bottom:8px">最优方案：<b>${best[1].name}</b>（夏普 ${best[1].metrics.sharpe}）</div>
    <table class="data-table"><thead><tr><th>策略</th><th>权重</th><th>区间收益</th><th>贡献</th></tr></thead><tbody>${
      (best[1].contributions || []).map(x => `<tr><td>${x.name}</td><td>${x.weight}%</td><td class="${pctCls(x.strategy_return)}">${x.strategy_return}%</td><td class="${pctCls(x.contribution)}">${x.contribution}</td></tr>`).join("")
    }</tbody></table>
    <div class="muted" style="font-size:12px;margin-top:8px">策略相关性：${(d.correlations || []).map(c => `${c.a}↔${c.b} ${c.corr}`).join(" · ")}</div>`;
}

async function runGridSearch() {
  prBusy("参数网格寻优中（12 组参数，约需数秒）");
  let d;
  try { d = await api("/api/optimize/grid?strategy=factor&days=500&train_ratio=0.7"); }
  catch (e) { $("#pr-placeholder").innerHTML = `❌ ${e.message}`; return; }
  if (d.error) { $("#pr-placeholder").innerHTML = `❌ ${d.error}`; return; }
  prShow("grid");
  $("#grid-summary").innerHTML = `
    <div class="card"><h3>参数组合</h3><div class="big">${d.combo_count}</div><div class="sub">策略 ${d.strategy}</div></div>
    <div class="card"><h3>样本内夏普中位数</h3><div class="big">${d.in_sample_sharpe_median}</div><div class="sub">前 ${(d.train_ratio * 100).toFixed(0)}% 区间</div></div>
    <div class="card"><h3>样本外夏普中位数</h3><div class="big ${pctCls(d.out_sample_sharpe_median)}">${d.out_sample_sharpe_median}</div><div class="sub">后段验证</div></div>
    <div class="card"><h3>样本内外秩相关</h3><div class="big">${d.rank_correlation ?? '—'}</div><div class="sub">越低越易过拟合</div></div>`;
  const bp = d.best_params;
  $("#grid-table").innerHTML = `<div class="muted" style="margin-bottom:8px">样本内最优：再平衡 ${bp.rebalance}日 · 持仓 ${bp.max_holdings}只 · 仓位 ${bp.weighting}；
    样本外夏普 ${d.best.out_sample.sharpe} · 样本外回撤 ${d.best.out_sample.max_drawdown}%</div>
    <table class="data-table"><thead><tr><th>再平衡</th><th>持仓数</th><th>仓位方案</th><th>IS收益</th><th>IS夏普</th><th>IS回撤</th><th>OS收益</th><th>OS夏普</th><th>OS回撤</th></tr></thead><tbody>${
      (d.rows || []).map(r => `<tr><td>${r.params.rebalance}日</td><td>${r.params.max_holdings}</td><td>${r.params.weighting}</td>
        <td class="${pctCls(r.in_sample.total_return)}">${r.in_sample.total_return}%</td><td>${r.in_sample.sharpe}</td><td class="down">${r.in_sample.max_drawdown}%</td>
        <td class="${pctCls(r.out_sample.total_return)}">${r.out_sample.total_return}%</td><td>${r.out_sample.sharpe}</td><td class="down">${r.out_sample.max_drawdown}%</td></tr>`).join("")
    }</tbody></table>`;
  $("#grid-warning").innerHTML = (d.warnings || []).length
    ? "⚠️ " + d.warnings.join("；")
    : "样本外表现未触发过拟合警示，但仍需谨慎。";
}

/* ================= 机构验证 ================= */
const VAL_STATUS = { have: ["已具备", "up"], partial: ["部分具备", ""], gap: ["缺失", "down"] };

async function loadValidation() {
  const host = $("#val-gap");
  if (!host) return;
  try {
    const d = await api("/api/validation/gap");
    const c = d.counts || {};
    $("#val-counts").innerHTML = `
      <div class="card"><h3>对标能力项</h3><div class="big">${d.total}</div><div class="sub">数据 / 因子 / 组合 / 验证 / 执行</div></div>
      <div class="card"><h3>已具备</h3><div class="big up">${c.have || 0}</div><div class="sub">日频多因子研究闭环</div></div>
      <div class="card"><h3>部分具备</h3><div class="big">${c.partial || 0}</div><div class="sub">有雏形，精度/约束不足</div></div>
      <div class="card"><h3>缺失</h3><div class="big down">${c.gap || 0}</div><div class="sub">本环境受限或需付费授权</div></div>`;
    host.className = "";
    host.innerHTML = `<table class="data-table"><thead><tr><th>层级</th><th>能力项</th><th>私募标准</th><th>本站</th><th>说明</th></tr></thead><tbody>${
      (d.rows || []).map((r) => {
        const st = VAL_STATUS[r.status] || ["—", ""];
        return `<tr><td class="muted" style="font-size:11px">${escHtml(r.layer)}</td><td>${escHtml(r.capability)}</td>
          <td class="muted" style="font-size:11px">${escHtml(r.standard)}</td>
          <td><span class="${st[1]}" style="font-weight:600">${st[0]}</span></td>
          <td class="muted" style="font-size:11.5px">${escHtml(r.note)}</td></tr>`;
      }).join("")
    }</tbody></table>
    <div class="muted" style="font-size:12px;margin-top:10px;line-height:1.7">${escHtml(d.conclusion)}</div>`;
  } catch (e) {
    host.textContent = "对标矩阵加载失败：" + e.message;
  }
}

async function runValidationIC(btn) {
  const host = $("#val-ic");
  if (btn) btn.disabled = true;
  host.className = "";
  host.innerHTML = '<div class="ai-loading"><span class="spin"></span>计算横截面 IC（中性化 + BH 多重检验 + 可交易过滤）…</div>';
  try {
    const d = await api("/api/validation/ic?days=500");
    const signNote = (r) => {
      const bhOk = r.p_bh < 0.05;
      const bonfOk = r.p_bonf < 0.05;
      const dot = bhOk ? "●" : bonfOk ? "◎" : "○";
      return `${dot} ${bhOk ? "BH<0.05" : bonfOk ? "Bonf<0.05" : "不显著"}`;
    };
    const validCnt = (d.rows || []).filter(r => r.verdict === "有效").length;
    host.innerHTML = `
      <div class="cards c4" style="margin-bottom:10px">
        <div class="card"><h3>股票池</h3><div class="big">${d.universe}</div><div class="sub">区间 ${d.days} 交易日</div></div>
        <div class="card"><h3>中性化</h3><div class="big ${d.neutralized ? 'up' : 'down'}">${d.neutralized ? '已开启' : '未开启'}</div><div class="sub">行业 + 市值 OLS 残差</div></div>
        <div class="card"><h3>有效因子</h3><div class="big up">${validCnt}</div><div class="sub">BH p<0.05 且 |IC|≥0.02</div></div>
        <div class="card"><h3>有效率</h3><div class="big">${d.rows ? (validCnt / d.rows.length * 100).toFixed(0) : 0}%</div><div class="sub">${d.rows?.length || 0} 组 (因子×持有期)</div></div>
      </div>
      <table class="data-table"><thead><tr>
        <th>因子</th><th>持有期</th><th>IC 均值</th><th>ICIR</th><th>t 值</th><th>p (raw)</th><th>p (Bonf)</th><th>p (BH)</th><th>胜率</th><th>判定</th>
      </tr></thead><tbody>${
        (d.rows || []).map((r) => `<tr>
          <td>${escHtml(r.factor_cn)}</td><td>${r.horizon}日</td>
          <td class="${pctCls(r.ic_mean)}">${r.ic_mean}</td>
          <td>${r.icir}</td>
          <td class="${pctCls(r.t_stat)}">${r.t_stat}</td>
          <td class="muted">${r.p_raw}</td>
          <td class="${r.p_bonf < 0.05 ? 'up' : 'muted'}">${r.p_bonf}</td>
          <td class="${r.p_bh < 0.05 ? 'up' : 'down'}"><b>${r.p_bh}</b></td>
          <td>${r.win_rate}%</td>
          <td class="${r.verdict === '有效' ? 'up' : r.verdict === '无效' ? 'down' : ''}">${r.verdict}</td>
        </tr>`).join("")
      }</tbody></table>
      <div class="muted" style="font-size:12px;margin-top:8px">${escHtml(d.note)}</div>`;
  } catch (e) {
    host.innerHTML = `<div class="down">❌ ${escHtml(e.message)}</div>`;
  } finally { if (btn) btn.disabled = false; }
}

async function runValidationWF(btn) {
  const host = $("#val-wf");
  if (btn) btn.disabled = true;
  host.className = "";
  host.innerHTML = '<div class="ai-loading"><span class="spin"></span>多因子 ICIR 加权样本外验证中…</div>';
  try {
    const d = await api("/api/validation/walkforward?days=750&top_n=10&rebalance=20&cost_bps=10");
    const ins = d.in_sample.portfolio, isb = d.in_sample.benchmark;
    const oos = d.out_of_sample.portfolio, oob = d.out_of_sample.benchmark;
    const decayCls = d.decay.sharpe_drop > 0.5 ? "down" : (d.decay.cagr_drop > 0 ? "" : "up");
    const flags = [
      d.neutralized ? "行业+市值中性化" : null,
      d.amihud_calibrated ? "Amihud 冲击成本" : null,
      "多因子 ICIR 加权",
    ].filter(Boolean).join(" · ");
    host.innerHTML = `
      <div class="cards c4" style="margin-bottom:12px">
        <div class="card"><h3>入选因子</h3><div class="big" style="font-size:18px">${d.selected.length} 个</div>
          <div class="sub">${flags}</div></div>
        <div class="card"><h3>样本内</h3><div class="big">${fmt(ins.sharpe)}</div>
          <div class="sub">年化 ${fmt(ins.cagr)}% · 回撤 ${fmt(ins.mdd)}%</div></div>
        <div class="card"><h3>样本外（组合）</h3><div class="big ${pctCls(oos.sharpe)}">${fmt(oos.sharpe)}</div>
          <div class="sub">年化 ${fmt(oos.cagr)}% · 回撤 ${fmt(oos.mdd)}%</div></div>
        <div class="card"><h3>样本外（等权基准）</h3><div class="big">${fmt(oob.sharpe)}</div>
          <div class="sub">年化 ${fmt(oob.cagr)}% · 样本内基准 ${fmt(isb.sharpe)}</div></div>
      </div>
      <div class="card" style="margin-bottom:10px"><h3>因子权重（ICIR 加权）</h3>
        <table class="data-table"><thead><tr><th>因子</th><th>IC</th><th>ICIR</th><th>权重</th><th>方向</th></tr></thead><tbody>${
          (d.selected || []).map((s) => `<tr><td>${escHtml(s.factor_cn)}</td>
            <td class="${pctCls(s.ic)}">${fmt(s.ic, 4)}</td>
            <td class="${pctCls(s.icir)}">${fmt(s.icir, 3)}</td>
            <td>${fmt((d.weights || {})[s.factor] ?? 0, 3)}</td>
            <td>${s.sign > 0 ? "正向" : "反向"}</td></tr>`).join("")
        }</tbody></table></div>
      <div class="card" style="margin-bottom:12px"><h3>样本外净值（组合 vs 等权基准，元）</h3>
        <div id="val-wf-chart" style="height:260px"></div></div>
      <div class="muted" style="font-size:12px;margin-bottom:8px">
        衰减判定：<b class="${decayCls}">${escHtml(d.decay.verdict)}</b> · 年化差 ${d.decay.cagr_drop}pp · 夏普差 ${d.decay.sharpe_drop}
        · 平均换手 ${d.avg_turnover}% · ${escHtml(d.note)}
      </div>
      <div class="cards c2">
        <div class="card"><h3>全部候选因子（训练段 IC）</h3>
          <table class="data-table"><thead><tr><th>因子</th><th>IC</th><th>t 值</th><th>方向</th><th>入选</th></tr></thead><tbody>${
            (d.candidates || []).map((r) => `<tr><td>${escHtml(r.factor_cn)}</td>
              <td class="${pctCls(r.ic)}">${fmt(r.ic, 4)}</td><td class="${pctCls(r.t)}">${fmt(r.t, 2)}</td>
              <td>${r.sign > 0 ? "正向做多" : "反向做多"}</td>
              <td>${r.ok ? "✓" : "✗"}</td></tr>`).join("")
          }</tbody></table></div>
        <div class="card"><h3>成本敏感性（样本外年化 %）</h3>
          <table class="data-table"><thead><tr><th>单边成本</th><th>年化收益</th><th>夏普</th></tr></thead><tbody>${
            (d.cost_sensitivity || []).map((r) => `<tr><td>${r.cost_bps} bp</td>
              <td class="${pctCls(r.cagr)}">${r.cagr == null ? "—" : r.cagr + "%"}</td><td>${r.sharpe ?? "—"}</td></tr>`).join("")
          }</tbody></table></div>
      </div>`;
    // 净值曲线
    try {
      const c = chartEl("val-wf-chart");
      const labels = ["起点"].concat((d.folds || []).map((f) => f.end));
      const nav = d.oos_nav || [], bench = d.oos_bench || [];
      const n = Math.min(labels.length, nav.length, bench.length);
      c.setOption({
        ...baseOpt(),
        grid: { left: 64, right: 24, top: 30, bottom: 30 },
        legend: { data: ["组合", "等权基准"], top: 0, textStyle: { color: "#8291b0", fontSize: 11 } },
        xAxis: { type: "category", data: labels.slice(0, n), axisLine: { lineStyle: { color: "#233049" } }, axisLabel: { color: "#8291b0", fontSize: 10 } },
        yAxis: { type: "value", scale: true, splitLine: { lineStyle: { color: "#1a2440" } }, axisLabel: { fontSize: 10 } },
        series: [
          { name: "组合", type: "line", symbol: "circle", data: nav.slice(0, n), lineStyle: { color: ACCENT, width: 2 } },
          { name: "等权基准", type: "line", symbol: "none", data: bench.slice(0, n), lineStyle: { color: "#8291b0", width: 1.5, type: "dashed" } },
        ],
      });
    } catch (e) { /* 图表库未就绪时不影响表格 */ }
  } catch (e) {
    host.innerHTML = `<div class="down">❌ ${escHtml(e.message)}</div>`;
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function runValidationCal(btn) {
  const host = $("#val-cal");
  if (btn) btn.disabled = true;
  host.className = "";
  host.innerHTML = '<div class="ai-loading"><span class="spin"></span>保序回归概率校准中…</div>';
  try {
    const d = await api(`/api/validation/calibration?code=${state.code}&days=500`);
    const R = d.raw, C = d.calibrated;
    host.innerHTML = `
      <div class="cards c3" style="margin-bottom:10px">
        <div class="card"><h3>Brier（原始 → 校准）</h3>
          <div class="big ${R.brier > C.brier ? 'up' : 'down'}">${R.brier} → ${C.brier}</div>
          <div class="sub">Brier 越小越好，降低 ${d.improvement.brier_delta}</div></div>
        <div class="card"><h3>Skill（原始 → 校准）</h3>
          <div class="big ${C.skill > 0 ? 'up' : 'down'}">${R.skill}% → ${C.skill}%</div>
          <div class="sub">${d.improvement.skill_delta > 0 ? '↑' : '↓'} ${d.improvement.skill_delta}pp</div></div>
        <div class="card"><h3>样本 / 基础涨率</h3>
          <div class="big">${d.samples}</div><div class="sub">真实上涨 ${d.base_rate}% · 校准器 ${d.calibrator}</div></div>
      </div>
      <div class="card"><h3>分桶偏差（越接近 0 越好）</h3>
        <table class="data-table"><thead><tr>
          <th>区间</th><th>原始n</th><th>原始偏差</th><th>校准n</th><th>校准偏差</th>
        </tr></thead><tbody>${
          (R.buckets || []).map((b, i) => {
            const cb = (C.buckets || [])[i] || {};
            return `<tr><td>${b.bucket}</td>
              <td>${b.n}</td>
              <td class="${pctCls(b.gap)}">${b.gap == null ? "—" : b.gap + "pp"}</td>
              <td>${cb.n ?? "—"}</td>
              <td class="${pctCls(cb.gap)}">${cb.gap == null ? "—" : cb.gap + "pp"}</td></tr>`;
          }).join("")
        }</tbody></table></div>
      <div class="muted" style="font-size:12px;margin-top:8px">
        看多信号 ${C.bullish.n} 次命中 ${C.bullish.hit ?? "—"}% · 看空信号 ${C.bearish.n} 次命中 ${C.bearish.hit ?? "—"}%
        <br>${escHtml(d.note)}
      </div>`;
  } catch (e) {
    host.innerHTML = `<div class="down">❌ ${escHtml(e.message)}</div>`;
  } finally { if (btn) btn.disabled = false; }
}

async function runValidationAudit(btn) {
  const host = $("#val-audit");
  if (btn) btn.disabled = true;
  host.className = "";
  host.innerHTML = '<div class="ai-loading"><span class="spin"></span>偏差审计中…</div>';
  try {
    const d = await api("/api/validation/audit");
    const lvlCls = { "高": "down", "中": "", "低": "muted" };
    host.innerHTML = `<div class="muted" style="font-size:12px;margin-bottom:8px">股票池 ${d.universe} 只 · 面板 ${d.days} 个交易日</div>
      <table class="data-table"><thead><tr><th>偏差项</th><th>影响</th><th>说明</th><th>可修复</th></tr></thead><tbody>${
        (d.items || []).map((r) => `<tr><td>${escHtml(r.item)}</td>
          <td class="${lvlCls[r.level] || ""}" style="font-weight:600">${escHtml(r.level)}</td>
          <td class="muted" style="font-size:11.5px">${escHtml(r.detail)}</td>
          <td>${r.fixable ? "可" : "不可"}</td></tr>`).join("")
      }</tbody></table>
      <div class="muted" style="font-size:12px;margin-top:8px">${escHtml(d.note)}</div>`;
  } catch (e) {
    host.innerHTML = `<div class="down">❌ ${escHtml(e.message)}</div>`;
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function runValidationPaper(btn) {
  const host = $("#val-paper");
  if (btn) btn.disabled = true;
  host.className = "";
  host.innerHTML = '<div class="ai-loading"><span class="spin"></span>Paper trading 逐日模拟盘中…</div>';
  try {
    const d = await api("/api/validation/paper?days=500&top_n=10&rebalance=20&cost_bps=10");
    const pf = d.portfolio, bm = d.benchmark;
    const alpha = +(pf.cagr - bm.cagr).toFixed(2);
    const alphaCls = alpha >= 0 ? "up" : "down";
    const better = pf.sharpe > bm.sharpe ? "组合优于基准" : "基准优于组合";
    host.innerHTML = `
      <div class="cards c4" style="margin-bottom:12px">
        <div class="card"><h3>入选因子</h3><div class="big" style="font-size:18px">${d.selected.length} 个</div>
          <div class="sub">训练段 60% 估因子 · 模拟段 ${d.days} 天执行</div></div>
        <div class="card"><h3>组合年化 / 夏普</h3><div class="big ${pctCls(pf.cagr)}">${fmt(pf.cagr)}% / ${fmt(pf.sharpe)}</div>
          <div class="sub">回撤 ${fmt(pf.mdd)}% · 累计 ${fmt(pf.total)}%</div></div>
        <div class="card"><h3>基准年化 / 夏普</h3><div class="big ${pctCls(bm.cagr)}">${fmt(bm.cagr)}% / ${fmt(bm.sharpe)}</div>
          <div class="sub">回撤 ${fmt(bm.mdd)}% · 累计 ${fmt(bm.total)}%</div></div>
        <div class="card"><h3>年化 Alpha / 换手</h3><div class="big ${alphaCls}">${alpha >= 0 ? "+" : ""}${alpha}pp</div>
          <div class="sub">${better} · 平均换手 ${d.avg_turnover}%</div></div>
      </div>
      <div class="card" style="margin-bottom:10px"><h3>净值曲线（组合 vs 等权基准，起始 1.0）</h3>
        <div id="val-paper-chart" style="height:280px"></div></div>
      <div class="cards c2">
        <div class="card"><h3>因子权重（ICIR 加权）</h3>
          <table class="data-table"><thead><tr><th>因子</th><th>IC</th><th>ICIR</th><th>权重</th></tr></thead><tbody>${
            (d.selected || []).map((s) => `<tr><td>${escHtml(s.factor_cn)}</td>
              <td class="${pctCls(s.ic)}">${fmt(s.ic, 4)}</td>
              <td class="${pctCls(s.icir)}">${fmt(s.icir, 3)}</td>
              <td>${fmt(s.weight, 3)}</td></tr>`).join("") ||
            `<tr><td colspan="4" class="muted">无入选因子</td></tr>`
          }</tbody></table></div>
        <div class="card"><h3>最近交易（${(d.trades || []).length} 笔）</h3>
          <table class="data-table" style="font-size:12px"><thead><tr><th>区间</th><th>毛收益</th><th>净收益</th><th>换手</th><th>冲击</th></tr></thead><tbody>${
            (d.trades || []).slice(-12).reverse().map((t) => `<tr><td>${escHtml(t.start)}→${escHtml(t.end)}</td>
              <td class="${pctCls(t.gross)}">${fmt(t.gross)}%</td>
              <td class="${pctCls(t.net)}">${fmt(t.net)}%</td>
              <td>${fmt(t.turnover)}%</td>
              <td>${fmt(t.impact_bps)} bp</td></tr>`).join("")
          }</tbody></table></div>
      </div>
      <div class="muted" style="font-size:12px;margin-top:8px">${escHtml(d.note)}</div>`;
    // 净值曲线
    try {
      const c = chartEl("val-paper-chart");
      const folds = d.trades || [];
      const labels = ["起点"].concat(folds.map((f) => f.end));
      const nav = d.nav_curve || [], bench = d.bench_curve || [];
      const n = Math.min(labels.length, nav.length, bench.length);
      c.setOption({
        ...baseOpt(),
        grid: { left: 64, right: 24, top: 30, bottom: 50 },
        legend: { data: ["组合", "等权基准"], top: 0, textStyle: { color: "#8291b0", fontSize: 11 } },
        xAxis: { type: "category", data: labels.slice(0, n), axisLine: { lineStyle: { color: "#233049" } },
                 axisLabel: { color: "#8291b0", fontSize: 9, rotate: 30 } },
        yAxis: { type: "value", scale: true, splitLine: { lineStyle: { color: "#1a2440" } },
                 axisLabel: { fontSize: 10, formatter: (v) => v.toFixed(2) } },
        tooltip: { trigger: "axis", formatter: (ps) => ps.map(p => `${p.seriesName}：${p.value.toFixed(4)}`).join("<br/>") },
        series: [
          { name: "组合", type: "line", symbol: "circle", symbolSize: 4, data: nav.slice(0, n),
            lineStyle: { color: ACCENT, width: 2 }, itemStyle: { color: ACCENT } },
          { name: "等权基准", type: "line", symbol: "none", data: bench.slice(0, n),
            lineStyle: { color: "#8291b0", width: 1.5, type: "dashed" } },
        ],
      });
    } catch (e) { /* 图表库未就绪时不影响表格 */ }
  } catch (e) {
    host.innerHTML = `<div class="down">❌ ${escHtml(e.message)}</div>`;
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* ================= 机构级（顶级私募对标） ================= */
const INST_STATUS = { have: ["已具备", "up"], partial: ["部分具备", ""], gap: ["缺失", "down"] };

async function loadInst() {
  const host = $("#inst-matrix");
  if (!host) return;
  try {
    const d = await api("/api/inst/matrix");
    const c = d.counts || {};
    $("#inst-counts").innerHTML = `
      <div class="card"><h3>对标能力项</h3><div class="big">${d.total}</div><div class="sub">数据/因子/模型/组合/风控/执行/验证</div></div>
      <div class="card"><h3>已具备</h3><div class="big up">${c.have || 0}</div><div class="sub">可运行、可验证</div></div>
      <div class="card"><h3>部分具备</h3><div class="big">${c.partial || 0}</div><div class="sub">有雏形，精度/约束不足</div></div>
      <div class="card"><h3>缺失</h3><div class="big down">${c.gap || 0}</div><div class="sub">需付费数据 / 硬件 / 牌照</div></div>`;
    host.className = "";
    host.innerHTML = `<table class="data-table"><thead><tr>
        <th>层级</th><th>能力项</th><th>私募标准</th><th>所需数据</th><th>本站</th><th>说明</th>
      </tr></thead><tbody>${
        (d.rows || []).map((r) => {
          const st = INST_STATUS[r.status] || ["—", ""];
          return `<tr><td class="muted" style="font-size:11px">${escHtml(r.layer)}</td>
            <td>${escHtml(r.capability)}</td>
            <td class="muted" style="font-size:11px">${escHtml(r.standard)}</td>
            <td class="muted" style="font-size:11px">${escHtml(r.data)}</td>
            <td><span class="${st[1]}" style="font-weight:600">${st[0]}</span></td>
            <td class="muted" style="font-size:11.5px">${escHtml(r.note)}</td></tr>`;
        }).join("")
      }</tbody></table>
      <div class="muted" style="font-size:12px;margin:10px 0;line-height:1.7">${escHtml(d.conclusion)}</div>
      <h3 style="margin-top:14px">个人交易 vs 机构：差在哪里</h3>
      <table class="data-table"><thead><tr>
        <th>维度</th><th>私募机构</th><th>个人交易</th><th>差距</th><th>本站现状</th>
      </tr></thead><tbody>${
        (d.retail || []).map((r) => `<tr><td><b>${escHtml(r.dim)}</b></td>
          <td class="muted" style="font-size:11.5px">${escHtml(r.inst)}</td>
          <td class="muted" style="font-size:11.5px">${escHtml(r.retail)}</td>
          <td class="muted" style="font-size:11.5px">${escHtml(r.gap)}</td>
          <td style="font-size:11.5px">${escHtml(r.we_have)}</td></tr>`).join("")
      }</tbody></table>`;
  } catch (e) {
    host.textContent = "能力矩阵加载失败：" + e.message;
  }
}

async function runInstBarra(btn) {
  const host = $("#inst-barra");
  if (btn) btn.disabled = true;
  host.className = "";
  host.innerHTML = '<div class="ai-loading"><span class="spin"></span>Barra 横截面回归 + 协方差估计中…</div>';
  try {
    const d = await api("/api/inst/barra?days=250");
    const rk = d.risk || {}, cap = rk.cap || {}, eq = rk.equal || {};
    const styleRows = (d.exposures || []).filter((x) => !x.factor.startsWith("IND:"));
    host.innerHTML = `
      <div class="cards c4" style="margin-bottom:10px">
        <div class="card"><h3>组合总波动（市值加权）</h3><div class="big">${fmt(cap.total_vol)}%</div><div class="sub">等权基准 ${fmt(eq.total_vol)}%</div></div>
        <div class="card"><h3>因子风险</h3><div class="big">${fmt(cap.factor_vol)}%</div><div class="sub">占总风险 ${fmt(cap.factor_share)}%</div></div>
        <div class="card"><h3>特质风险</h3><div class="big">${fmt(cap.specific_vol)}%</div><div class="sub">占总风险 ${fmt(cap.specific_share)}%</div></div>
        <div class="card"><h3>估计窗口</h3><div class="big" style="font-size:18px">${d.days} 日</div><div class="sub">${d.universe} 只 · ${(d.industries || []).length} 个行业组</div></div>
      </div>
      <div class="card" style="margin-bottom:10px"><h3>风格暴露（横截面标准化 z）</h3>
        <table class="data-table" style="font-size:12px"><thead><tr>
          <th>风格因子</th><th>市值加权组合</th><th>等权组合</th><th>横截面均值</th><th>最小</th><th>最大</th><th>标准差</th>
        </tr></thead><tbody>${
          styleRows.map((x) => `<tr><td>${escHtml(x.name)}</td>
            <td class="${pctCls(x.portfolio)}">${fmt(x.portfolio, 3)}</td>
            <td class="${pctCls(x.portfolio_eq)}">${fmt(x.portfolio_eq, 3)}</td>
            <td class="muted">${fmt(x.cross_mean, 3)}</td>
            <td class="muted">${fmt(x.cross_min)}</td>
            <td class="muted">${fmt(x.cross_max)}</td>
            <td class="muted">${fmt(x.std, 3)}</td></tr>`).join("")
        }</tbody></table></div>
      <div class="card" style="margin-bottom:10px"><h3>风险贡献 TOP12（占组合总风险）</h3>
        <table class="data-table" style="font-size:12px"><thead><tr>
          <th>因子 / 行业</th><th>暴露</th><th>风险贡献</th>
        </tr></thead><tbody>${
          (d.contributions || []).map((x) => `<tr><td>${escHtml(x.name || x.factor)}</td>
            <td class="${pctCls(x.exposure)}">${fmt(x.exposure, 3)}</td>
            <td class="${pctCls(x.risk_contrib)}">${fmt(x.risk_contrib)}%</td></tr>`).join("")
        }</tbody></table></div>
      <div class="muted" style="font-size:12px;line-height:1.7">${escHtml(d.note)}</div>`;
  } catch (e) {
    host.innerHTML = `<div class="down">❌ ${escHtml(e.message)}</div>`;
  } finally { if (btn) btn.disabled = false; }
}

async function runInstOpt(btn) {
  const host = $("#inst-opt");
  if (btn) btn.disabled = true;
  host.className = "";
  host.innerHTML = '<div class="ai-loading"><span class="spin"></span>投影梯度求解约束最优组合…</div>';
  try {
    const d = await api("/api/inst/optimize?days=400&top_n=12");
    const m = d.metrics || {}, ck = d.constraints || {};
    host.innerHTML = `
      <div class="cards c4" style="margin-bottom:10px">
        <div class="card"><h3>预期年化超额</h3><div class="big ${pctCls(m.pred_return_ann_pct)}">${fmt(m.pred_return_ann_pct)}%</div><div class="sub">IC 关系估算 · 5日 ${fmt(m.pred_return_5d_pct)}%</div></div>
        <div class="card"><h3>预期波动</h3><div class="big">${fmt(m.expected_vol)}%</div><div class="sub">等权基准 ${fmt(m.baseline_vol)}% · 变化 ${fmt(m.vol_change_pct)}%</div></div>
        <div class="card"><h3>信息比率</h3><div class="big">${fmt(m.info_ratio, 3)}</div><div class="sub">预期收益 / 预期波动</div></div>
        <div class="card"><h3>换手 / 成本</h3><div class="big">${fmt(m.turnover, 1)}%</div><div class="sub">单边成本 ${fmt(m.cost_pct, 3)}%</div></div>
      </div>
      <div class="cards c2" style="margin-bottom:10px">
        <div class="card"><h3>持仓（前 20）</h3>
          <table class="data-table" style="font-size:12px"><thead><tr><th>代码</th><th>权重</th><th>alpha z</th><th>特质波动</th></tr></thead><tbody>${
            (d.holdings || []).map((h) => `<tr><td>${escHtml(h.code)}</td><td>${fmt(h.weight)}%</td>
              <td class="${pctCls(h.alpha_z)}">${fmt(h.alpha_z, 3)}</td><td class="muted">${fmt(h.spec_vol, 1)}%</td></tr>`).join("")
          }</tbody></table></div>
        <div class="card"><h3>约束校验</h3>
          <table class="data-table" style="font-size:12px"><tbody>
            <tr><td>权重合计</td><td>${fmt(ck.sum, 4)}</td></tr>
            <tr><td>最大单票</td><td>${fmt(ck.max_weight * 100, 2)}%</td></tr>
            <tr><td>最大行业</td><td>${fmt(ck.max_industry * 100, 2)}%</td></tr>
            <tr><td>最大风格暴露</td><td>${fmt(ck.max_style, 3)}</td></tr>
            <tr><td>最小权重（非负约束）</td><td>${fmt(ck.min_weight, 4)}</td></tr>
          </tbody></table>
          <h3 style="margin-top:12px">风格暴露：优化前 → 后</h3>
          <table class="data-table" style="font-size:12px"><thead><tr><th>风格</th><th>前</th><th>后</th></tr></thead><tbody>${
            (d.styles || []).map((s) => `<tr><td>${escHtml(s.name)}</td>
              <td class="muted">${fmt(s.before, 3)}</td><td class="${pctCls(s.after)}">${fmt(s.after, 3)}</td></tr>`).join("")
          }</tbody></table></div>
      </div>
      <div class="card" style="margin-bottom:10px"><h3>入选因子（训练段 ICIR 加权）</h3>
        <table class="data-table" style="font-size:12px"><thead><tr><th>因子</th><th>IC</th><th>ICIR</th><th>权重</th></tr></thead><tbody>${
          (d.selected_factors || []).map((s) => `<tr><td>${escHtml(s.factor_cn)}</td>
            <td class="${pctCls(s.ic)}">${fmt(s.ic, 4)}</td><td class="${pctCls(s.icir)}">${fmt(s.icir, 3)}</td>
            <td>${fmt(s.weight, 3)}</td></tr>`).join("")
        }</tbody></table></div>
      <div class="muted" style="font-size:12px;line-height:1.7">${escHtml(d.note)}</div>`;
  } catch (e) {
    host.innerHTML = `<div class="down">❌ ${escHtml(e.message)}</div>`;
  } finally { if (btn) btn.disabled = false; }
}

async function runInstTCA(btn) {
  const host = $("#inst-tca");
  if (btn) btn.disabled = true;
  host.className = "";
  host.innerHTML = '<div class="ai-loading"><span class="spin"></span>冲击成本与容量曲线计算中…</div>';
  try {
    const d = await api("/api/inst/tca?days=400");
    host.innerHTML = `
      <div class="cards c4" style="margin-bottom:10px">
        <div class="card"><h3>组合规模</h3><div class="big">${d.portfolio_size} 只</div><div class="sub">持仓均为高流动性标的</div></div>
        <div class="card"><h3>代理回测毛年化</h3><div class="big up">${fmt(d.gross_cagr_pct)}%</div><div class="sub">多因子打分前 20% 等权</div></div>
        <div class="card"><h3>平均单边换手</h3><div class="big">${fmt(d.avg_turnover_pct, 1)}%</div><div class="sub">按 5 日持有近似</div></div>
        <div class="card"><h3>容量上限</h3><div class="big ${d.capacity_limit_yi == null ? 'up' : 'down'}">${d.capacity_limit_yi == null ? "未触及" : d.capacity_limit_yi + " 亿"}</div><div class="sub">净年化跌到 0 的 AUM</div></div>
      </div>
      <div class="cards c3" style="margin-bottom:10px">${
        (d.algos || []).map((a) => `<div class="card"><h3>${escHtml(a.algo)}</h3>
          <div class="big">${fmt(a.avg_impact_bps)}<span style="font-size:12px"> bp</span></div>
          <div class="sub">最大 ${fmt(a.max_impact_bps)} bp · ${escHtml(a.desc)}</div></div>`).join("")
      }</div>
      <table class="data-table"><thead><tr>
        <th>AUM（亿元）</th><th>平均冲击(bp)</th><th>年化成本(%)</th><th>净年化(%)</th><th>占 ADV(%)</th>
      </tr></thead><tbody>${
        (d.capacity || []).map((r) => `<tr><td>${fmt(r.aum, 1)}</td>
          <td>${fmt(r.avg_impact_bps)}</td><td class="${pctCls(-r.annual_cost_pct)}">${fmt(r.annual_cost_pct)}</td>
          <td class="${pctCls(r.net_cagr)}">${fmt(r.net_cagr)}</td><td class="muted">${fmt(r.pct_of_adv)}</td></tr>`).join("")
      }</tbody></table>
      <div class="muted" style="font-size:12px;margin-top:8px;line-height:1.7">${escHtml(d.note)}</div>`;
  } catch (e) {
    host.innerHTML = `<div class="down">❌ ${escHtml(e.message)}</div>`;
  } finally { if (btn) btn.disabled = false; }
}

async function runInstDSR(btn) {
  const host = $("#inst-dsr");
  if (btn) btn.disabled = true;
  host.className = "";
  host.innerHTML = '<div class="ai-loading"><span class="spin"></span>DSR + PBO 计算中…</div>';
  try {
    const d = await api("/api/inst/dsr?days=600");
    const b = d.best || {};
    const probeCls = (d.pbo || 0) > 0.5 ? "down" : "up";
    host.innerHTML = `
      <div class="cards c2" style="margin-bottom:10px">
        <div class="card"><h3>最佳因子夏普</h3><div class="big">${fmt(b.sharpe)}</div>
          <div class="sub">${escHtml(b.factor_cn || "")} · t=${fmt(b.t_stat)}</div></div>
        <div class="card"><h3>PBO 过拟合概率</h3><div class="big ${probeCls}">${fmt(d.pbo, 3)}</div>
          <div class="sub">${d.pbo > 0.5 ? "样本内最优大概率不可复现" : "相对稳健"}</div></div>
      </div>
      <div class="card" style="margin-bottom:10px"><h3>DSR：${d.n_trials} 次试验后的折减夏普</h3>
        <table class="data-table" style="font-size:12px"><tbody>
          <tr><td>样本夏普</td><td>${fmt(b.sharpe)}</td></tr>
          <tr><td>期望最大夏普 SR₀（纯噪声）</td><td>${fmt(b.deflated_sr0)}</td></tr>
          <tr><td>Deflated Sharpe（≈ 真实概率）</td><td class="${b.dsr > 0.9 ? 'up' : 'down'}">${fmt(b.dsr, 4)}</td></tr>
          <tr><td>PBO logit 均值</td><td>${fmt(d.pbo_logit_mean, 3)}</td></tr>
        </tbody></table>
        <div class="muted" style="font-size:12px;margin-top:8px">判定：<b class="${b.dsr > 0.9 ? 'up' : 'down'}">${escHtml(d.verdict)}</b></div></div>
      <table class="data-table" style="font-size:12px"><thead><tr>
        <th>因子</th><th>夏普</th><th>年化收益</th><th>年化波动</th><th>t 值</th>
      </tr></thead><tbody>${
        (d.trials || []).map((r) => `<tr><td>${escHtml(r.factor_cn)}</td>
          <td class="${pctCls(r.sharpe)}">${fmt(r.sharpe)}</td><td class="${pctCls(r.ann_ret)}">${fmt(r.ann_ret)}%</td>
          <td class="muted">${fmt(r.ann_vol)}%</td><td class="${pctCls(r.t)}">${fmt(r.t)}</td></tr>`).join("")
      }</tbody></table>`;
  } catch (e) {
    host.innerHTML = `<div class="down">❌ ${escHtml(e.message)}</div>`;
  } finally { if (btn) btn.disabled = false; }
}

async function runInstDecay(btn) {
  const host = $("#inst-decay");
  if (btn) btn.disabled = true;
  host.className = "";
  host.innerHTML = '<div class="ai-loading"><span class="spin"></span>因子 IC 衰减拟合中…</div>';
  try {
    const d = await api("/api/inst/decay?days=500");
    const rows = (d.rows || []).slice().sort((a, b) => Math.abs(b.ic_1) - Math.abs(a.ic_1));
    host.innerHTML = `
      <div class="cards c2" style="margin-bottom:10px">
        <div class="card"><h3>有效持有期</h3><div class="big" style="font-size:17px">${d.horizons.join("/")} 日</div><div class="sub">半衰期越长，越适合低频调仓</div></div>
        <div class="card"><h3>衰减最快</h3><div class="big down" style="font-size:17px">${escHtml((rows.filter(r => r.half_life).sort((a,b)=>a.half_life-b.half_life)[0] || {}).factor_cn || "—")}</div><div class="sub">半衰期最短，必须快进快出</div></div>
      </div>
      <div id="inst-decay-chart" style="height:220px;margin-bottom:10px"></div>
      <table class="data-table" style="font-size:12px"><thead><tr>
        <th>因子</th><th>IC(1日)</th><th>IC(5日)</th><th>IC(20日)</th><th>IC(60日)</th><th>半衰期</th><th>衰减比</th><th>结论</th>
      </tr></thead><tbody>${
        rows.slice(0, 12).map((r) => `<tr><td>${escHtml(r.factor_cn)}</td>
          <td class="${pctCls(r.ic_1)}">${fmt(r.ic_1, 4)}</td><td class="${pctCls(r.ic_5)}">${fmt(r.ic_5, 4)}</td>
          <td class="${pctCls(r.ic_20)}">${fmt(r.ic_20, 4)}</td><td class="${pctCls(r.ic_60)}">${fmt(r.ic_60, 4)}</td>
          <td>${r.half_life == null ? "—" : r.half_life + " 日"}</td><td class="muted">${fmt(r.decay_ratio, 3)}</td>
          <td class="muted" style="font-size:11px">${escHtml(r.persistence || "")}</td></tr>`).join("")
      }</tbody></table>
      <div class="muted" style="font-size:12px;margin-top:8px;line-height:1.7">${escHtml(d.note)}</div>`;
    try {
      const c = chartEl("inst-decay-chart");
      const picked = rows.slice(0, 6);
      const curveMap = {};
      (d.curves || []).forEach((x) => { curveMap[x.factor] = x.ic || []; });
      c.setOption({
        ...baseOpt(),
        grid: { left: 52, right: 16, top: 28, bottom: 28 },
        legend: { data: picked.map((r) => r.factor_cn), top: 0, textStyle: { color: "#8291b0", fontSize: 10 } },
        tooltip: { trigger: "axis" },
        xAxis: { type: "category", data: (d.horizons || []).map((h) => h + "日"), axisLabel: { color: "#8291b0", fontSize: 10 } },
        yAxis: { type: "value", name: "IC", splitLine: { lineStyle: { color: "#1a2440" } }, axisLabel: { fontSize: 10 } },
        series: picked.map((r) => ({
          name: r.factor_cn, type: "line", symbol: "circle", symbolSize: 4,
          data: (curveMap[r.factor] || []).map((v) => +(v || 0).toFixed(4)),
          lineStyle: { width: 1.8 },
        })),
      });
    } catch (e) { /* 图表不可用不影响表格 */ }
  } catch (e) {
    host.innerHTML = `<div class="down">❌ ${escHtml(e.message)}</div>`;
  } finally { if (btn) btn.disabled = false; }
}

async function runInstML(btn) {
  const host = $("#inst-ml");
  if (btn) btn.disabled = true;
  host.className = "";
  host.innerHTML = '<div class="ai-loading"><span class="spin"></span>训练直方图 GBDT 并做样本外对比…</div>';
  try {
    const d = await api("/api/inst/ml?days=700");
    const g = d.gbdt || {}, l = d.linear || {};
    host.innerHTML = `
      <div class="cards c3" style="margin-bottom:10px">
        <div class="card"><h3>GBDT 样本外 IC</h3><div class="big ${pctCls(g.ic_mean)}">${fmt(g.ic_mean, 4)}</div><div class="sub">ICIR ${fmt(g.icir, 3)} · 胜率 ${fmt(g.win_rate, 1)}%</div></div>
        <div class="card"><h3>线性 样本外 IC</h3><div class="big ${pctCls(l.ic_mean)}">${fmt(l.ic_mean, 4)}</div><div class="sub">ICIR ${fmt(l.icir, 3)} · 胜率 ${fmt(l.win_rate, 1)}%</div></div>
        <div class="card"><h3>增量</h3><div class="big ${pctCls(d.improve_ic)}">${fmt(d.improve_ic, 4)}</div><div class="sub">${escHtml(d.verdict)}</div></div>
      </div>
      <div class="muted" style="font-size:12px;margin-bottom:8px">
        训练 ${d.train_days} 日 / 样本外 ${d.test_days} 日 · ${d.n_train} 条样本 · 特征 ${d.features.length} 个
      </div>
      <table class="data-table" style="font-size:12px"><thead><tr><th>特征</th><th>分裂次数</th><th>重要性</th></tr></thead><tbody>${
        (d.importance || []).map((r) => `<tr><td>${escHtml(r.factor_cn)}</td>
          <td>${r.splits}</td><td>${fmt(r.importance, 1)}%</td></tr>`).join("")
      }</tbody></table>
      <div class="muted" style="font-size:12px;margin-top:8px;line-height:1.7">${escHtml(d.note)}</div>`;
  } catch (e) {
    host.innerHTML = `<div class="down">❌ ${escHtml(e.message)}</div>`;
  } finally { if (btn) btn.disabled = false; }
}

async function runInstAlloc(btn) {
  const host = $("#inst-alloc");
  if (btn) btn.disabled = true;
  host.className = "";
  host.innerHTML = '<div class="ai-loading"><span class="spin"></span>多策略权重求解 + 相关性分析中…</div>';
  try {
    const d = await api("/api/inst/alloc?days=600");
    const ms = d.methods || [];
    const best = ms.slice().sort((a, b) => b.sharpe - a.sharpe)[0] || {};
    host.innerHTML = `
      <div class="cards c3" style="margin-bottom:10px">
        <div class="card"><h3>最优方案</h3><div class="big" style="font-size:17px">${escHtml(best.name || "—")}</div><div class="sub">夏普 ${fmt(best.sharpe)} · 年化 ${fmt(best.ann_ret)}%</div></div>
        <div class="card"><h3>子策略数</h3><div class="big">${(d.strategies || []).length}</div><div class="sub">因子多空，${d.days} 日样本</div></div>
        <div class="card"><h3>最大回撤（最优）</h3><div class="big down">${fmt(best.mdd)}%</div><div class="sub">波动 ${fmt(best.ann_vol)}%</div></div>
      </div>
      <table class="data-table" style="font-size:12px"><thead><tr>
        <th>方案</th><th>年化</th><th>波动</th><th>夏普</th><th>最大回撤</th><th>权重分布</th>
      </tr></thead><tbody>${
        ms.map((m) => `<tr><td>${escHtml(m.name)}</td>
          <td class="${pctCls(m.ann_ret)}">${fmt(m.ann_ret)}%</td><td>${fmt(m.ann_vol)}%</td>
          <td class="${pctCls(m.sharpe)}">${fmt(m.sharpe)}</td><td class="down">${fmt(m.mdd)}%</td>
          <td class="muted" style="font-size:11px">${
            (d.strategies || []).map((s) => `${escHtml(s.key)}=${fmt(m.weights[s.key], 1)}%`).join(" · ")
          }</td></tr>`).join("")
      }</tbody></table>
      <div class="muted" style="font-size:12px;margin-top:8px">
        策略间相关性：${(d.correlations || []).slice(0, 6).map((c) => `${escHtml(c.a)}↔${escHtml(c.b)} ${fmt(c.corr, 2)}`).join(" · ")}
      </div>
      <div class="muted" style="font-size:12px;margin-top:6px;line-height:1.7">${escHtml(d.note)}</div>`;
  } catch (e) {
    host.innerHTML = `<div class="down">❌ ${escHtml(e.message)}</div>`;
  } finally { if (btn) btn.disabled = false; }
}

/* ================= 全球数据融合 ================= */
const WORLD_STATUS = { have: ["✅ 已接入", "up"], partial: ["⚠ 部分", ""], gap: ["❌ 缺口", "down"] };

async function loadWorld() {
  const covHost = $("#world-coverage");
  if (!covHost) return;
  const on = (sel, fn) => { const el = $(sel); if (el) el.onclick = fn; };
  on("#world-dash-run", () => runWorldDashboard($("#world-dash-run")));
  on("#world-lag-run", () => runWorldLeadlag($("#world-lag-run")));
  on("#world-beta-run", () => runWorldBeta($("#world-beta-run")));
  on("#world-regime-run", () => runWorldRegime($("#world-regime-run")));
  on("#world-alpha-run", () => runWorldAlpha($("#world-alpha-run")));

  try {
    renderWorldCoverage(await api("/api/world/coverage"));
  } catch (e) {
    covHost.innerHTML = `<div class="down">❌ 覆盖度加载失败：${escHtml(e.message)}</div>`;
  }
  try {
    const q = await api("/api/world/quotes?limit_per_class=4");
    renderWorldQuotes(q.quotes || []);
  } catch (e) {
    $("#world-quotes").innerHTML = `<div class="down">❌ 全球行情加载失败：${escHtml(e.message)}</div>`;
  }
  try {
    renderWorldMatrix(await api("/api/world/matrix"));
  } catch (e) {
    $("#world-matrix").innerHTML = `<div class="down">❌ 矩阵加载失败：${escHtml(e.message)}</div>`;
  }
}

function renderWorldCoverage(d) {
  const cls = d.classes || [];
  const cached = cls.reduce((s, r) => s + (r.cached || 0), 0);
  const a = d.a_share || {};
  $("#world-counts").innerHTML = `
    <div class="card"><h3>全球标的</h3><div class="big">${d.total_symbols || 0}</div><div class="sub">${cls.length} 大类资产</div></div>
    <div class="card"><h3>已缓存历史</h3><div class="big up">${cached}</div><div class="sub">SQLite 本地缓存标的数</div></div>
    <div class="card"><h3>A 股全市场</h3><div class="big">${a.symbols || 0}</div><div class="sub">${a.with_kline || 0} 只有历史K线</div></div>
    <div class="card"><h3>数据源</h3><div class="big">10</div><div class="sub">新浪 + ECB/Frankfurter</div></div>`;
  const host = $("#world-coverage");
  host.className = "";
  host.innerHTML = `<table class="data-table"><thead><tr>
      <th>类别</th><th>标的数</th><th>已缓存</th><th>数据源</th><th>历史起点</th><th>模式</th><th>局限</th></tr></thead><tbody>${
    cls.map((r) => `<tr><td><b>${escHtml(r.cat_name)}</b></td>
      <td>${r.symbols}</td><td>${r.cached || 0}</td>
      <td class="muted" style="font-size:11px">${escHtml(r.source)}</td>
      <td class="muted" style="font-size:11px">${escHtml(r.hist)}</td>
      <td class="muted" style="font-size:11px">${escHtml(r.mode)}</td>
      <td class="muted" style="font-size:11px">${escHtml(r.limitation)}</td></tr>`).join("")
  }<tr style="border-top:2px solid var(--border)"><td><b>${escHtml(a.cat_name || "A股个股")}</b></td>
      <td>${a.symbols || 0}</td><td>${a.with_kline || 0}</td>
      <td class="muted" style="font-size:11px">${escHtml(a.source || "")}</td>
      <td class="muted" style="font-size:11px">${escHtml(a.hist || "")}</td>
      <td class="muted" style="font-size:11px">${escHtml(a.mode || "")}</td>
      <td class="muted" style="font-size:11px">—</td></tr>
    </tbody></table>
    <div class="muted" style="font-size:12px;margin-top:10px;line-height:1.7">${escHtml(d.note || "")}</div>`;
}

function renderWorldQuotes(rows) {
  const host = $("#world-quotes");
  if (!host) return;
  const by = {};
  rows.forEach((r) => { (by[r.cat_name] = by[r.cat_name] || []).push(r); });
  host.className = "";
  host.innerHTML = Object.keys(by).map((cn) => `
    <div style="margin-bottom:10px">
      <div class="muted" style="font-size:11px;margin-bottom:6px">${escHtml(cn)}</div>
      <div class="g-grid">${by[cn].map((q) => `
        <div class="g-card" data-cat="${escHtml(q.cat)}" data-sym="${escHtml(q.symbol)}" data-name="${escHtml(q.name)}">
          <div class="g-name">${escHtml(q.name)}</div>
          <div class="g-price">${fmt(q.price)}</div>
          <div class="g-pct ${pctCls2(q.pct)}">${pctText(q.pct || 0)}</div>
        </div>`).join("")}</div>
    </div>`).join("");
  host.querySelectorAll(".g-card").forEach((el) => el.addEventListener("click", () =>
    showWorldKline(el.dataset.cat, el.dataset.sym, el.dataset.name)));
}

async function showWorldKline(cat, sym, name) {
  const host = $("#world-chart");
  host.innerHTML = `<div class="card" style="margin-top:16px"><h3>${escHtml(name)} <span class="muted" style="font-size:11px">${escHtml(cat)} / ${escHtml(sym)}</span></h3>
    <div id="world-kline" class="chart lg"><div class="muted" style="padding:16px">加载中…</div></div></div>`;
  try {
    const d = await api(`/api/world/kline/${encodeURIComponent(cat)}/${encodeURIComponent(sym)}?days=250`);
    if (!(d.kline || []).length) throw new Error("该标的无历史K线（可能仅支持实时行情）");
    renderKlineChart("world-kline", d.kline, { zoomPct: [0, 100] });
  } catch (e) {
    host.innerHTML = `<div class="card" style="margin-top:16px"><div class="down" style="padding:14px">❌ ${escHtml(name)}：${escHtml(e.message)}</div></div>`;
  }
}

function renderWorldMatrix(d) {
  const host = $("#world-matrix");
  if (!host) return;
  const c = d.counts || {};
  host.className = "";
  host.innerHTML = `<div class="muted" style="font-size:12px;margin-bottom:8px">
      已接入 ${c.have || 0} · 部分 ${c.partial || 0} · 缺口 ${c.gap || 0}（共 ${d.total || 0} 个接入点）</div>
    <table class="data-table"><thead><tr>
      <th>层级</th><th>接入项</th><th>实现方式</th><th>数据</th><th>状态</th><th>说明</th></tr></thead><tbody>${
    (d.rows || []).map((r) => {
      const st = WORLD_STATUS[r.status] || ["—", ""];
      return `<tr><td class="muted" style="font-size:11px">${escHtml(r.layer)}</td>
        <td>${escHtml(r.item)}</td>
        <td class="muted" style="font-size:11px">${escHtml(r.how)}</td>
        <td class="muted" style="font-size:11px">${escHtml(r.data)}</td>
        <td><span class="${st[1]}" style="font-weight:600">${st[0]}</span></td>
        <td class="muted" style="font-size:11.5px">${escHtml(r.note)}</td></tr>`;
    }).join("")
  }</tbody></table>
    <div class="muted" style="font-size:12px;margin-top:10px;line-height:1.7">${escHtml(d.conclusion || "")}</div>`;
}

function worldStatCards(pairs) {
  return `<div class="cards c4" style="margin-bottom:10px">${pairs.map(([t, v, sub, cls]) => `
    <div class="card"><h3>${t}</h3><div class="big ${cls || ""}">${v}</div><div class="sub">${sub || ""}</div></div>`).join("")}</div>`;
}

async function runWorldDashboard(btn) {
  const host = $("#world-dashboard");
  if (btn) btn.disabled = true;
  host.className = "";
  host.innerHTML = '<div class="ai-loading"><span class="spin"></span>合成 6 个全球因子中…</div>';
  try {
    const d = await api("/api/world/dashboard?days=600");
    const rows = d.rows || [];
    const codes = d.corr_codes || [];
    const cm = d.corr_matrix || [];
    const nm = {};
    rows.forEach((r) => { nm[r.key] = r.name; });
    const cell = (v) => {
      if (v === null || v === undefined) return '<td class="muted" style="text-align:center">—</td>';
      const a = Math.min(1, Math.abs(v));
      const bg = v >= 0 ? `rgba(239,68,68,${0.08 + a * 0.32})` : `rgba(34,197,94,${0.08 + a * 0.32})`;
      return `<td style="background:${bg};text-align:center">${fmt(v, 2)}</td>`;
    };
    host.innerHTML = `
      <table class="data-table"><thead><tr>
        <th>全球因子</th><th>1日</th><th>5日</th><th>20日</th><th>60日</th>
        <th>年化波动</th><th>与A股同日</th><th>全球t-1→A股t</th><th>样本</th>
      </tr></thead><tbody>${rows.map((r) => `<tr>
        <td><b>${escHtml(r.name)}</b><div class="muted" style="font-size:10px">${escHtml(r.key)}</div></td>
        <td class="${pctCls(r.d1)}">${fmt(r.d1)}%</td>
        <td class="${pctCls(r.d5)}">${fmt(r.d5)}%</td>
        <td class="${pctCls(r.d20)}">${fmt(r.d20)}%</td>
        <td class="${pctCls(r.d60)}">${fmt(r.d60)}%</td>
        <td>${fmt(r.ann_vol)}%</td>
        <td>${fmt(r.corr_a_same, 3)}</td>
        <td>${fmt(r.corr_a_lag1, 3)}</td>
        <td class="muted">${r.n}</td></tr>`).join("")}</tbody></table>
      <h3 style="margin-top:16px;font-size:13px">因子相关矩阵</h3>
      <table class="data-table"><thead><tr><th></th>${codes.map((c) => `<th>${escHtml(nm[c] || c)}</th>`).join("")}</tr></thead>
      <tbody>${cm.map((row, i) => `<tr><td><b>${escHtml(nm[codes[i]] || codes[i])}</b></td>${row.map(cell).join("")}</tr>`).join("")}</tbody></table>
      <div class="muted" style="font-size:12px;margin-top:10px;line-height:1.7">${escHtml(d.note || "")}</div>`;
  } catch (e) {
    host.innerHTML = `<div class="down">❌ ${escHtml(e.message)}</div>`;
  } finally { if (btn) btn.disabled = false; }
}

async function runWorldLeadlag(btn) {
  const host = $("#world-leadlag");
  if (btn) btn.disabled = true;
  host.className = "";
  host.innerHTML = '<div class="ai-loading"><span class="spin"></span>计算 lag 0~5 相关系数…</div>';
  try {
    const d = await api("/api/world/leadlag?days=500&max_lag=5");
    const rows = d.rows || [];
    host.innerHTML = `
      <table class="data-table"><thead><tr>
        <th>全球因子</th><th>最优 lag</th><th>相关系数</th><th>t 值</th><th>lag 0~5 曲线</th><th>解读</th>
      </tr></thead><tbody>${rows.map((r) => `<tr>
        <td><b>${escHtml(r.name)}</b><div class="muted" style="font-size:10px">${escHtml(r.key)}</div></td>
        <td>${r.best_lag === null ? "—" : r.best_lag}</td>
        <td class="${pctCls(r.best_corr)}">${fmt(r.best_corr, 4)}</td>
        <td>${fmt(r.best_t)}</td>
        <td class="muted" style="font-size:11px">${(r.curves || []).map((v, i) => `L${i} ${fmt(v, 3)}`).join(" · ")}</td>
        <td class="muted" style="font-size:11.5px">${escHtml(r.verdict || "")}</td></tr>`).join("")}</tbody></table>
      <div class="muted" style="font-size:12px;margin-top:10px;line-height:1.7">${escHtml(d.note || "")}</div>`;
  } catch (e) {
    host.innerHTML = `<div class="down">❌ ${escHtml(e.message)}</div>`;
  } finally { if (btn) btn.disabled = false; }
}

async function runWorldBeta(btn) {
  const host = $("#world-beta");
  if (btn) btn.disabled = true;
  host.className = "";
  host.innerHTML = '<div class="ai-loading"><span class="spin"></span>OLS 回归组合对全球因子的 beta…</div>';
  try {
    const d = await api("/api/world/beta?days=500");
    const rows = d.rows || [];
    host.innerHTML = worldStatCards([
      ["R²", fmt(d.r2, 4), `样本 ${d.days} 日 · 股票池 ${d.universe}`, ""],
      ["个股 beta 均值", fmt(d.stock_beta_mean, 3), "对 G_EQ（全球风险偏好）", ""],
      ["个股 beta 标准差", fmt(d.stock_beta_std, 3), "离散度越大越需分层", ""],
      ["α（日）", fmt(d.intercept, 6), "截距项", "muted"],
    ]) + `<table class="data-table"><thead><tr>
        <th>全球因子</th><th>beta</th><th>t 值</th><th>显著性</th><th>完全对冲名义(亿)</th><th>对冲工具</th>
      </tr></thead><tbody>${rows.map((r) => `<tr>
        <td><b>${escHtml(r.name)}</b></td>
        <td class="${pctCls(r.beta)}">${fmt(r.beta, 4)}</td>
        <td>${fmt(r.t)}</td>
        <td class="${r.sig === "显著" ? "up" : "muted"}">${escHtml(r.sig)}</td>
        <td>${fmt(r.hedge_notional_yi)}</td>
        <td class="muted" style="font-size:11px">${escHtml(r.hedge_hint)}</td></tr>`).join("")}</tbody></table>
      <h3 style="margin-top:14px;font-size:13px">个股对 G_EQ 的 beta 前十（敞口最集中）</h3>
      <div class="muted" style="font-size:12px;line-height:1.9">${
        (d.stock_beta_top || []).map(([c, b]) => `${escHtml(c)} <b class="${pctCls(b)}">${fmt(b, 3)}</b>`).join(" · ")
      }</div>
      <div class="muted" style="font-size:12px;margin-top:10px;line-height:1.7">${escHtml(d.note || "")}</div>`;
  } catch (e) {
    host.innerHTML = `<div class="down">❌ ${escHtml(e.message)}</div>`;
  } finally { if (btn) btn.disabled = false; }
}

const STATE_LABEL = (s) => String(s || "—")
  .replace("risk_on", "风险偏好↑").replace("risk_off", "风险偏好↓")
  .replace("usd_up", "美元↑").replace("usd_down", "美元↓");

async function runWorldRegime(btn) {
  const host = $("#world-regime");
  if (btn) btn.disabled = true;
  host.className = "";
  host.innerHTML = '<div class="ai-loading"><span class="spin"></span>统计四象限下 A 股未来收益…</div>';
  try {
    const d = await api("/api/world/regime?days=1000&horizon=5");
    const rows = d.rows || [];
    host.innerHTML = worldStatCards([
      ["当前状态", STATE_LABEL(d.current), "G_EQ × G_USD 60日趋势", ""],
      ["全样本未来收益", fmt(d.base_mean_pct, 3) + "%", `未来 ${d.horizon} 日沪深300`, pctCls(d.base_mean_pct)],
    ]) + `<table class="data-table"><thead><tr>
        <th>状态</th><th>样本数</th><th>未来收益均值</th><th>胜率</th><th>年化波动</th><th>建议仓位系数</th>
      </tr></thead><tbody>${rows.map((r) => `<tr>
        <td><b>${escHtml(STATE_LABEL(r.state))}</b><div class="muted" style="font-size:10px">${escHtml(r.state)}</div></td>
        <td>${r.n}</td>
        <td class="${pctCls(r.mean_fwd_pct)}">${fmt(r.mean_fwd_pct)}%</td>
        <td>${fmt(r.win_rate, 1)}%</td>
        <td>${fmt(r.ann_vol)}%</td>
        <td><b>${fmt(r.pos_coef, 2)}</b></td></tr>`).join("")}</tbody></table>
      <div class="muted" style="font-size:12px;margin-top:10px;line-height:1.7">${escHtml(d.note || "")}</div>`;
  } catch (e) {
    host.innerHTML = `<div class="down">❌ ${escHtml(e.message)}</div>`;
  } finally { if (btn) btn.disabled = false; }
}

async function runWorldAlpha(btn) {
  const host = $("#world-alpha");
  if (btn) btn.disabled = true;
  host.className = "";
  host.innerHTML = '<div class="ai-loading"><span class="spin"></span>训练段选因子 + 样本外 IC 对比中（约 5~10 秒）…</div>';
  try {
    const d = await api("/api/world/alpha?days=600&horizon=5");
    const b = d.base_oos || {}, x = d.extended_oos || {};
    const imp = d.improve_ic;
    host.innerHTML = worldStatCards([
      ["原 14 因子 样本外 IC", fmt(b.ic, 4), `ICIR ${fmt(b.icir)} · t ${fmt(b.t)} · 胜率 ${fmt(b.win, 1)}%`, pctCls(b.ic)],
      ["+3 全球因子 样本外 IC", fmt(x.ic, 4), `ICIR ${fmt(x.icir)} · t ${fmt(x.t)} · 胜率 ${fmt(x.win, 1)}%`, pctCls(x.ic)],
      ["IC 增量", (imp > 0 ? "+" : "") + fmt(imp, 4), imp > 0 ? "全球因子有正贡献" : "无正贡献，不启用", pctCls(imp)],
      ["样本外天数", b.n || x.n || 0, `训练 ${d.train_days} / 测试 ${d.test_days}`, "muted"],
    ]) + `<div class="muted" style="font-size:13px;margin:8px 0;padding:8px;border-left:3px solid var(--border)">${
      escHtml(d.verdict || "")
    }</div>
      <h3 style="margin-top:12px;font-size:13px">训练段选中的因子</h3>
      <div class="muted" style="font-size:12px;line-height:1.9">原有：${(d.selected_base || []).map(escHtml).join(" · ") || "—"}
        <br>全球：${(d.selected_global || []).map(escHtml).join(" · ") || "—"}</div>
      <h3 style="margin-top:14px;font-size:13px">全球因子单独 IC（训练段）</h3>
      <table class="data-table"><thead><tr><th>因子</th><th>说明</th><th>IC</th><th>ICIR</th><th>t 值</th><th>胜率</th><th>样本</th></tr></thead>
      <tbody>${(d.global_factors || []).map((r) => `<tr>
        <td><b>${escHtml(r.factor)}</b></td>
        <td class="muted" style="font-size:11px">${escHtml(r.cn)}</td>
        <td class="${pctCls(r.ic)}">${fmt(r.ic, 4)}</td>
        <td>${fmt(r.icir, 3)}</td><td>${fmt(r.t)}</td><td>${fmt(r.win, 1)}%</td><td class="muted">${r.n}</td></tr>`).join("")}</tbody></table>
      <h3 style="margin-top:14px;font-size:13px">不同全球状态下的复合 IC</h3>
      <table class="data-table"><thead><tr><th>全球状态</th><th>天数</th><th>IC</th><th>胜率</th></tr></thead>
      <tbody>${(d.regime_ic || []).map((r) => `<tr>
        <td><b>${escHtml(STATE_LABEL(r.state))}</b></td><td>${r.n}</td>
        <td class="${pctCls(r.ic)}">${fmt(r.ic, 4)}</td><td>${fmt(r.win, 1)}%</td></tr>`).join("")}</tbody></table>
      <div class="muted" style="font-size:12px;margin-top:10px;line-height:1.7">${escHtml(d.note || "")}</div>`;
  } catch (e) {
    host.innerHTML = `<div class="down">❌ ${escHtml(e.message)}</div>`;
  } finally { if (btn) btn.disabled = false; }
}

/* ================= 资金流向 ================= */
async function loadCapitalFlow() {
  const code = state.code;
  $("#cf-loading").style.display = "block";
  $("#cf-content").style.display = "none";
  const d = await api(`/api/stock/${code}/capital_flow`);
  if (code !== state.code) return;
  $("#cf-loading").style.display = "none";
  $("#cf-content").style.display = "block";
  const verdictCls = d.flow_verdict === "多" ? "up" : d.flow_verdict === "空" ? "down" : "";
  $("#cf-summary").innerHTML = `
    <div class="card"><h3>主力资金信号</h3><div class="big ${verdictCls}">${d.flow_signal}</div><div class="sub">${d.flow_verdict}</div></div>
    <div class="card"><h3>最新主力净流入</h3><div class="big ${pctCls(d.latest_net_amount_yi)}">${d.latest_net_amount_yi ?? '—'}</div><div class="sub">亿元</div></div>
    <div class="card"><h3>更新时间</h3><div class="big" style="font-size:18px">${d.update_time}</div></div>`;
  // 资金流向柱状图
  const flows = (d.money_flow || []).slice().reverse();
  const dates = flows.map(r => r.date);
  const nets = flows.map(r => +(r.net_amount / 1e8).toFixed(2));
  if (window.cfChart) window.cfChart.dispose();
  window.cfChart = echarts.init(document.getElementById("cf-chart"));
  window.cfChart.setOption({
    tooltip: { trigger: "axis", formatter: p => `${p[0].name}<br/>主力净流入：${p[0].value} 亿` },
    xAxis: { type: "category", data: dates },
    yAxis: { type: "value", name: "亿元" },
    series: [{ type: "bar", data: nets.map(v => ({ value: v, itemStyle: { color: v >= 0 ? "#e74c3c" : "#2ecc71" } })) }],
  });
  // 北向资金
  const nb = d.northbound;
  if (nb) {
    $("#cf-north").innerHTML = `
      <div class="kv"><span>持股日期</span><b>${nb.date ?? '—'}</b></div>
      <div class="kv"><span>持股数量</span><b>${nb.hold_shares ? fmt(nb.hold_shares) + ' 股' : '—'}</b></div>
      <div class="kv"><span>持股市值</span><b>${nb.hold_market_cap ? fmt(nb.hold_market_cap / 1e8) + ' 亿' : '—'}</b></div>
      <div class="kv"><span>占A股流通比</span><b>${nb.a_shares_ratio ?? '—'}%</b></div>`;
  } else {
    $("#cf-north").innerHTML = '<div class="muted">暂无北向资金数据</div>';
  }
  // 龙虎榜
  const lhb = d.dragon_tiger || [];
  if (lhb.length) {
    $("#cf-lhb").innerHTML = `<table class="data-table"><thead><tr><th>日期</th><th>净买入(亿)</th><th>上榜原因</th></tr></thead><tbody>${
      lhb.slice(0, 8).map(r => `<tr><td>${r.date}</td><td class="${pctCls((r.net_amount||0)/1e8)}">${fmt((r.net_amount||0)/1e8)}</td><td style="font-size:11px">${r.reason || ''}</td></tr>`).join("")
    }</tbody></table>`;
  } else {
    $("#cf-lhb").innerHTML = '<div class="muted">近30天未上龙虎榜</div>';
  }
}

async function init() {
  await ensureEcharts();
  initSearch();
  initGlobalSearch();
  initModelsPanel();
  bindRangeButtons();
  // 自主智能体：长期记忆 + 主动观察
  const memBtn = $("#mem-refresh");
  if (memBtn) memBtn.addEventListener("click", loadAgentMemory);
  const memClear = $("#mem-clear");
  if (memClear) memClear.addEventListener("click", clearAgentMemory);
  const watchRun = $("#agent-watch-run");
  if (watchRun) watchRun.addEventListener("click", () => runAgentWatch(watchRun));
  $$(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));
  $("#chatSend").addEventListener("click", () => sendChat());
  $("#chatInput").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); } });
  const btBtn = $("#bt-run");
  if (btBtn) btBtn.addEventListener("click", runBacktest);
  // 策略回测 tab：策略/区间选择 + 运行按钮（只绑定一次）
  $$("#view-backtest .rng-btn").forEach(b => {
    b.addEventListener("click", () => {
      if (b.dataset.bt) { btStrategy = b.dataset.bt; $$("#view-backtest [data-bt]").forEach(x => x.classList.toggle("active", x === b)); }
      if (b.dataset.days) { btDays = +b.dataset.days; $$("#view-backtest [data-days]").forEach(x => x.classList.toggle("active", x === b)); }
    });
  });
  const btStratBtn = $("#bt-strategy-run");
  if (btStratBtn) btStratBtn.addEventListener("click", runStrategyBacktest);
  // 组合回测 tab：策略/区间/再平衡/仓位选择 + 运行按钮
  $$("#view-portfoliobt .rng-btn").forEach(b => {
    b.addEventListener("click", () => {
      if (b.dataset.pbt) { pbtStrategy = b.dataset.pbt; $$("#view-portfoliobt [data-pbt]").forEach(x => x.classList.toggle("active", x === b)); }
      if (b.dataset.days) { pbtDays = +b.dataset.days; $$("#view-portfoliobt [data-days]").forEach(x => x.classList.toggle("active", x === b)); }
      if (b.dataset.rb) { pbtRebalance = +b.dataset.rb; $$("#view-portfoliobt [data-rb]").forEach(x => x.classList.toggle("active", x === b)); }
      if (b.dataset.wt) { pbtWeight = b.dataset.wt; $$("#view-portfoliobt [data-wt]").forEach(x => x.classList.toggle("active", x === b)); }
    });
  });
  const pbtBtn = $("#pbt-run");
  if (pbtBtn) pbtBtn.addEventListener("click", runPortfolioBacktest);
  // 盯盘信号 tab
  $$("#view-watch .rng-btn").forEach(b => {
    b.addEventListener("click", () => {
      if (!b.dataset.wstr) return;
      watchStrategy = b.dataset.wstr;
      $$("#view-watch [data-wstr]").forEach(x => x.classList.toggle("active", x === b));
    });
  });
  const bind = (sel, fn) => { const el = $(sel); if (el) el.addEventListener("click", fn); };
  bind("#watch-run", runWatchSignals);
  bind("#watch-brief", runWatchBrief);
  bind("#hold-add", addHolding);
  bind("#hold-clear", clearHoldings);
  bind("#push-btn", pushBrief);
  // 风险归因 tab
  bind("#risk-run", runRiskAnalysis);
  bind("#attr-run", runAttribution);
  bind("#mix-run", runMixOptimize);
  bind("#grid-run", runGridSearch);
  // 机构验证 tab
  bind("#val-ic-run", () => runValidationIC($("#val-ic-run")));
  bind("#val-wf-run", () => runValidationWF($("#val-wf-run")));
  bind("#val-cal-run", () => runValidationCal($("#val-cal-run")));
  bind("#val-audit-run", () => runValidationAudit($("#val-audit-run")));
  bind("#val-paper-run", () => runValidationPaper($("#val-paper-run")));
  // 机构级 tab
  bind("#inst-barra-run", () => runInstBarra($("#inst-barra-run")));
  bind("#inst-opt-run", () => runInstOpt($("#inst-opt-run")));
  bind("#inst-tca-run", () => runInstTCA($("#inst-tca-run")));
  bind("#inst-dsr-run", () => runInstDSR($("#inst-dsr-run")));
  bind("#inst-decay-run", () => runInstDecay($("#inst-decay-run")));
  bind("#inst-ml-run", () => runInstML($("#inst-ml-run")));
  bind("#inst-alloc-run", () => runInstAlloc($("#inst-alloc-run")));
  try {
    const st = await api("/api/ai/status");
    const badge = $("#aiBadge");
    badge.classList.toggle("llm", st.configured);
    badge.innerHTML = `<span class="dot"></span>${st.engine}${st.configured ? " · " + st.base_url : ""}`;
  } catch (e) {}
  loadDaily().catch((e) => {
    $("#view-daily").innerHTML = `<div class="empty-tip"><div class="icon">📡</div>${e.message}</div>`;
  });
}
document.addEventListener("DOMContentLoaded", init);
