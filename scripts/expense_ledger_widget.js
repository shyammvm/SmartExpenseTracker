// Variables used by Scriptable.
// These must be at the very top of the file. Do not edit.
// icon-color: green; icon-glyph: chart-line;

/**
 * 🧾 EXPENSE LEDGER — Apple Stocks Style Visual Widget
 * 
 * Styled after Apple/Tesla Stocks Cards + Daily Spending Trend:
 *  - Dynamic Card Color:
 *      🟢 Vibrant Emerald Green when Today <= Daily Average
 *      🔴 Vibrant Crimson Red when Today > Daily Average
 *  - Daily Spending Area Graph with:
 *      - Dotted Benchmark Line representing your Daily Average
 *      - Smooth Bezier curve showing daily spend progression
 *      - Glowing highlight beacon on today's value
 *      - Soft translucent area fill under the curve
 *  - Real-time Spend Metric & Percentage Indicator (▼ 27% / ▲ 125%)
 *  - Dynamic Month Name + Monthly & Variable totals
 *  - 100% Native Scriptable APIs (zero non-standard polyfills or crashing methods)
 *  - Taps directly open GitHub Pages ledger site
 */

// =====================================================================
// ⚙️ CONFIGURATION
// =====================================================================
const CONFIG = {
  // Your deployed FastAPI backend URL
  apiUrl: "https://smartexpensetracker-vtkb.onrender.com",

  // Your endpoint secret (x-endpoint-secret header)
  apiSecret: "2546698",

  // Web app URL opened when tapping the widget (GitHub Pages PWA)
  webAppUrl: "https://shyammvm.github.io/SmartExpenseTracker/index.html",

  // Widget Title
  widgetTitle: "Ledger",

  // Compact Currency Settings (₹ 312, ₹ 1.2K, ₹ 55.9K)
  useCompactNumbers: true,
  compactStyle: "indian", // "indian" (K, L, Cr) or "standard" (K, M, B)

  // Color Palette: Green Card (Spend <= Daily Avg)
  greenGradientTop: "#1A6C47",
  greenGradientBottom: "#0A281A",
  greenAccent: "#30D158",
  greenSubtext: "#A2F8BF",

  // Color Palette: Red Card (Spend > Daily Avg)
  redGradientTop: "#851C1C",
  redGradientBottom: "#2E0808",
  redAccent: "#FF453A",
  redSubtext: "#FFB3B0",

  // Widget auto-refresh interval in minutes
  refreshIntervalMinutes: 15,

  // Network timeout in seconds (failover to cache quickly if Render is sleeping)
  timeoutSeconds: 5,

  // In-app interactive preview size ("small" or "medium")
  previewSize: "small",
};

// =====================================================================
// 📦 SCRIPT ENTRY POINT
// =====================================================================
(async () => {
  const data = await fetchExpenseData();

  const isRunningInWidget = (typeof config !== "undefined" && Boolean(config.runsInWidget));
  const widgetFamily = isRunningInWidget
    ? (config.widgetFamily || "small")
    : (CONFIG.previewSize || "small");

  let widget;
  if (widgetFamily === "medium") {
    widget = await createMediumWidget(data);
  } else {
    widget = await createSmallWidget(data);
  }

  // Register widget for iOS Home Screen
  Script.setWidget(widget);

  // When testing inside Scriptable app, display on-screen preview
  if (!isRunningInWidget) {
    if (widgetFamily === "medium") {
      await widget.presentMedium();
    } else {
      await widget.presentSmall();
    }
  }

  Script.complete();
})();

// =====================================================================
// 🎨 SMALL WIDGET BUILDER (Stocks Card Style)
// =====================================================================

async function createSmallWidget(data) {
  const todayTotal = data ? Number(data.today_total) || 0 : 0;
  const avgDaily = data ? Number(data.avg_daily_variable_spend) || 1 : 1;

  // Determine State: Green (Safe <= Avg) vs Red (High > Avg)
  const isOverBudget = todayTotal > avgDaily;
  const burnPct = Math.round((todayTotal / (avgDaily || 1)) * 100);

  const topColor = isOverBudget ? CONFIG.redGradientTop : CONFIG.greenGradientTop;
  const bottomColor = isOverBudget ? CONFIG.redGradientBottom : CONFIG.greenGradientBottom;
  const subtextColor = isOverBudget ? CONFIG.redSubtext : CONFIG.greenSubtext;

  const widget = new ListWidget();
  widget.backgroundGradient = makeLinearGradient(topColor, bottomColor);
  widget.setPadding(12, 12, 12, 12);

  if (CONFIG.webAppUrl) {
    widget.url = CONFIG.webAppUrl;
  }

  // --- 1. TOP HEADER (Title + Subtitle + Frosted Icon Badge) ---
  const headerRow = widget.addStack();
  headerRow.layoutHorizontally();
  headerRow.centerAlignContent();
  if (CONFIG.webAppUrl) headerRow.url = CONFIG.webAppUrl;

  const titleCol = headerRow.addStack();
  titleCol.layoutVertically();
  titleCol.spacing = 1;

  const monthTotal = data ? Number(data.month_total) || 0 : 0;
  const monthVar = data ? Number(data.month_variable_total) || 0 : 0;
  const currentMonth = getCurrentMonthName(true);

  const titleText = titleCol.addText(CONFIG.widgetTitle);
  titleText.font = Font.boldSystemFont(15);
  titleText.textColor = new Color("#FFFFFF");

  // Dynamic Month Name + Monthly Total & Variable in header subtitle
  const subtitleText = titleCol.addText(
    `${currentMonth} · ${formatCurrency(monthTotal)} (Var ${formatCurrency(monthVar)})`
  );
  subtitleText.font = Font.systemFont(9);
  subtitleText.textColor = new Color("#FFFFFF", 0.8);
  subtitleText.minimumScaleFactor = 0.75;
  subtitleText.lineLimit = 1;

  headerRow.addSpacer();

  // Frosted Icon Badge (Top Right)
  const badgeStack = headerRow.addStack();
  badgeStack.size = new Size(26, 26);
  badgeStack.cornerRadius = 7;
  badgeStack.backgroundColor = new Color("#FFFFFF", 0.18);
  badgeStack.borderWidth = 0.5;
  badgeStack.borderColor = new Color("#FFFFFF", 0.25);
  badgeStack.centerAlignContent();

  const symImg = getSFSymbolImage("creditcard.fill", 12);
  if (symImg) {
    const badgeIcon = badgeStack.addImage(symImg);
    badgeIcon.tintColor = new Color("#FFFFFF");
    badgeIcon.imageSize = new Size(13, 13);
  } else {
    const rupeeText = badgeStack.addText("₹");
    rupeeText.font = Font.boldSystemFont(12);
    rupeeText.textColor = new Color("#FFFFFF");
  }

  widget.addSpacer(4);

  // --- 2. DAILY SPENDING GRAPH (Image 2 Style with Dotted Benchmark) ---
  const history = getHistoryArray(data, todayTotal, avgDaily);
  const chartImg = renderAreaChart(148, 68, history, avgDaily, isOverBudget);
  if (chartImg) {
    const chartStack = widget.addStack();
    chartStack.layoutHorizontally();
    chartStack.centerAlignContent();
    if (CONFIG.webAppUrl) chartStack.url = CONFIG.webAppUrl;

    const chartWidgetImg = chartStack.addImage(chartImg);
    chartWidgetImg.imageSize = new Size(148, 68);
  }

  widget.addSpacer(4);

  // --- 3. BOTTOM ROW: SPEND AMOUNT + DAY-OVER-DAY PERCENTAGE (Image 1 Style) ---
  const bottomRow = widget.addStack();
  bottomRow.layoutHorizontally();
  bottomRow.bottomAlignContent();
  if (CONFIG.webAppUrl) bottomRow.url = CONFIG.webAppUrl;

  const todayStack = bottomRow.addStack();
  todayStack.layoutVertically();
  todayStack.spacing = 1;

  const amountText = todayStack.addText(formatCurrency(todayTotal));
  amountText.font = Font.boldSystemFont(17);
  amountText.textColor = new Color("#FFFFFF");
  amountText.minimumScaleFactor = 0.8;
  amountText.lineLimit = 1;

  const todayVar = getTodayVariable(data, history);
  const varText = todayStack.addText(formatCurrency(todayVar));
  varText.font = Font.systemFont(10);
  varText.textColor = new Color("#FFFFFF", 0.65);
  varText.minimumScaleFactor = 0.8;
  varText.lineLimit = 1;

  bottomRow.addSpacer();

  // Percentage & Arrow Badge based on previous day with "yes." indicator
  const yesterdayTotal = getYesterdayAmount(data, history);
  const dod = calculateDayOverDayChange(todayTotal, yesterdayTotal);

  const pctStack = bottomRow.addStack();
  pctStack.layoutHorizontally();
  pctStack.centerAlignContent();

  const pctText = pctStack.addText(dod.text);
  pctText.font = Font.boldSystemFont(11);
  pctText.textColor = new Color(subtextColor);
  pctText.minimumScaleFactor = 0.8;
  pctText.lineLimit = 1;

  const refreshDate = new Date(Date.now() + 1000 * 60 * CONFIG.refreshIntervalMinutes);
  widget.refreshAfterDate = refreshDate;

  return widget;
}

// =====================================================================
// 🎨 MEDIUM WIDGET BUILDER (Panoramic Trend)
// =====================================================================

async function createMediumWidget(data) {
  const todayTotal = data ? Number(data.today_total) || 0 : 0;
  const avgDaily = data ? Number(data.avg_daily_variable_spend) || 1 : 1;
  const isOverBudget = todayTotal > avgDaily;

  const history = getHistoryArray(data, todayTotal, avgDaily);
  const yesterdayTotal = getYesterdayAmount(data, history);
  const dod = calculateDayOverDayChange(todayTotal, yesterdayTotal);

  const topColor = isOverBudget ? CONFIG.redGradientTop : CONFIG.greenGradientTop;
  const bottomColor = isOverBudget ? CONFIG.redGradientBottom : CONFIG.greenGradientBottom;
  const subtextColor = isOverBudget ? CONFIG.redSubtext : CONFIG.greenSubtext;

  const widget = new ListWidget();
  widget.backgroundGradient = makeLinearGradient(topColor, bottomColor);
  widget.setPadding(14, 14, 14, 14);

  if (CONFIG.webAppUrl) {
    widget.url = CONFIG.webAppUrl;
  }

  // --- TOP ROW ---
  const headerRow = widget.addStack();
  headerRow.layoutHorizontally();
  headerRow.centerAlignContent();
  if (CONFIG.webAppUrl) headerRow.url = CONFIG.webAppUrl;

  const symImg = getSFSymbolImage("creditcard.fill", 14);
  if (symImg) {
    const icon = headerRow.addImage(symImg);
    icon.tintColor = new Color("#FFFFFF");
    icon.imageSize = new Size(16, 16);
    headerRow.addSpacer(6);
  }

  const monthTotal = data ? Number(data.month_total) || 0 : 0;
  const monthVar = data ? Number(data.month_variable_total) || 0 : 0;
  const currentMonth = getCurrentMonthName(true);

  const titleCol = headerRow.addStack();
  titleCol.layoutVertically();

  const title = titleCol.addText(CONFIG.widgetTitle);
  title.font = Font.boldSystemFont(14);
  title.textColor = new Color("#FFFFFF");

  const sub = titleCol.addText(`${currentMonth} TOTAL: ${formatCurrency(monthTotal)} · VAR: ${formatCurrency(monthVar)}`);
  sub.font = Font.systemFont(9);
  sub.textColor = new Color("#FFFFFF", 0.8);

  headerRow.addSpacer();

  // Metrics right
  const rightCol = headerRow.addStack();
  rightCol.layoutVertically();

  const valRow = rightCol.addStack();
  valRow.layoutHorizontally();
  valRow.bottomAlignContent();
  valRow.spacing = 3;

  const valText = valRow.addText(formatCurrency(todayTotal));
  valText.font = Font.boldSystemFont(18);
  valText.textColor = new Color("#FFFFFF");

  const todayVar = getTodayVariable(data, history);
  const varText = valRow.addText(formatCurrency(todayVar));
  varText.font = Font.systemFont(10);
  varText.textColor = new Color("#FFFFFF", 0.65);

  const yestFormatted = formatCurrency(yesterdayTotal);
  const pText = rightCol.addText(`${dod.text} (${yestFormatted})`);
  pText.font = Font.systemFont(10);
  pText.textColor = new Color(subtextColor);

  widget.addSpacer(10);

  // --- PANORAMIC DAILY SPEND GRAPH ---
  const chartImg = renderAreaChart(292, 76, history, avgDaily, isOverBudget);
  if (chartImg) {
    const chartStack = widget.addStack();
    chartStack.layoutHorizontally();
    chartStack.centerAlignContent();
    if (CONFIG.webAppUrl) chartStack.url = CONFIG.webAppUrl;

    const chartWidgetImg = chartStack.addImage(chartImg);
    chartWidgetImg.imageSize = new Size(292, 76);
  }

  const refreshDate = new Date(Date.now() + 1000 * 60 * CONFIG.refreshIntervalMinutes);
  widget.refreshAfterDate = refreshDate;

  return widget;
}

// =====================================================================
// 📈 HIGH-DPI CHART ENGINE (DrawContext)
// =====================================================================

function renderAreaChart(width, height, history, avgDaily, isOverBudget) {
  try {
    const dc = new DrawContext();
    dc.size = new Size(width, height);
    dc.opaque = false;
    dc.respectScreenScale = true;

    const padX = 2;
    const padTop = 14;
    const padBottom = 8;
    const chartHeight = height - padTop - padBottom;

    // Use full widget width: dock the benchmark pill at far right, expand graph up to the pill
    const isSmall = width <= 200;
    const pillW = isSmall ? 28 : 46;
    const pillRightMargin = isSmall ? 0 : 1;        // 👈 Tighter edge spacing
    const pillX = width - pillW - pillRightMargin;
    const graphEndX = pillX - (isSmall ? 1 : 2);    // 👈 Reduced gap so graph extends closer to the pill
    const graphWidth = graphEndX - padX;

    if (!Array.isArray(history) || history.length < 2) {
      return null;
    }

    const amounts = history.map(h => Number(h.amount) || 0);
    const maxVal = Math.max(...amounts, avgDaily * 1.35, 100);
    const minVal = 0;

    // Calculate (x, y) coordinates for each day within the expanded graph area [padX, graphEndX]
    const points = amounts.map((val, idx) => {
      const x = padX + (idx / Math.max(1, amounts.length - 1)) * graphWidth;
      const norm = (val - minVal) / (maxVal - minVal);
      const y = height - padBottom - norm * chartHeight;
      return { x: Math.round(x * 10) / 10, y: Math.round(y * 10) / 10 };
    });

    // 1. DOTTED BENCHMARK LINE (spans across the graph area)
    const avgNorm = (avgDaily - minVal) / (maxVal - minVal);
    const avgY = Math.round((height - padBottom - avgNorm * chartHeight) * 10) / 10;
    drawDottedLine(dc, padX, graphEndX, avgY, "#FFFFFF", 0.45, 4, 3);

    const bottomY = height - padBottom;

    // 2. AREA TRANSLUCENT FILL UNDER THE CURVE
    const fillPath = new Path();
    fillPath.move(new Point(points[0].x, bottomY));
    fillPath.addLine(new Point(points[0].x, points[0].y));

    for (let i = 0; i < points.length - 1; i++) {
      const p0 = points[i];
      const p1 = points[i + 1];
      const dx = (p1.x - p0.x) / 2;
      fillPath.addCurve(
        new Point(p1.x, p1.y),
        new Point(p0.x + dx, p0.y),
        new Point(p1.x - dx, p1.y)
      );
    }
    fillPath.addLine(new Point(points[points.length - 1].x, bottomY));
    fillPath.closeSubpath();

    dc.addPath(fillPath);
    dc.setFillColor(new Color("#FFFFFF", 0.16));
    dc.fillPath();

    // 3. UPPER GLOWING STROKE LINE
    const strokePath = new Path();
    strokePath.move(new Point(points[0].x, points[0].y));

    for (let i = 0; i < points.length - 1; i++) {
      const p0 = points[i];
      const p1 = points[i + 1];
      const dx = (p1.x - p0.x) / 2;
      strokePath.addCurve(
        new Point(p1.x, p1.y),
        new Point(p0.x + dx, p0.y),
        new Point(p1.x - dx, p1.y)
      );
    }

    dc.addPath(strokePath);
    dc.setStrokeColor(new Color("#FFFFFF", 0.95));
    dc.setLineWidth(2.2);
    dc.strokePath();

    // 4. TODAY'S GLOWING BEACON (Last Point at x = graphEndX)
    const lastPt = points[points.length - 1];

    dc.setFillColor(new Color("#FFFFFF", 0.35));
    dc.fillEllipse(new Rect(lastPt.x - 5, lastPt.y - 5, 10, 10));

    dc.setFillColor(new Color("#FFFFFF", 1.0));
    dc.fillEllipse(new Rect(lastPt.x - 2.5, lastPt.y - 2.5, 5, 5));

    // 5. BENCHMARK NUMBER DOCKED TO THE FAR RIGHT
    drawRightAvgLabel(dc, width, height, pillX, pillW, avgY, avgDaily, padBottom);

    return dc.getImage();
  } catch (e) {
    console.warn("Chart render error: " + e);
    return null;
  }
}

function drawDottedLine(dc, startX, endX, y, hexColor, alpha = 0.5, dash = 4, gap = 3) {
  dc.setStrokeColor(new Color(hexColor, alpha));
  dc.setLineWidth(1);

  for (let x = startX; x < endX; x += dash + gap) {
    const x2 = Math.min(x + dash, endX);
    const p = new Path();
    p.move(new Point(x, y));
    p.addLine(new Point(x2, y));
    dc.addPath(p);
    dc.strokePath();
  }
}

function drawRightAvgLabel(dc, width, height, pillX, pillW, avgY, avgDaily, padBottom = 8) {
  try {
    const compactVal = formatCompactCurrency(avgDaily);
    const isSmall = width <= 200;
    const text = isSmall ? compactVal.replace("₹ ", "₹") : compactVal;
    const pillH = 13;
    const pillY = Math.max(2, Math.min(height - padBottom - pillH, avgY - pillH / 2));

    const pillPath = new Path();
    pillPath.addRoundedRect(new Rect(pillX, pillY, pillW, pillH), 3.5, 3.5);
    dc.addPath(pillPath);
    dc.setFillColor(new Color("#000000", 0.55));
    dc.fillPath();

    dc.addPath(pillPath);
    dc.setStrokeColor(new Color("#FFFFFF", 0.30));
    dc.setLineWidth(0.75);
    dc.strokePath();

    dc.setFont(Font.boldSystemFont(isSmall ? 7.5 : 8.5));
    dc.setTextColor(new Color("#FFFFFF", 0.95));
    dc.setTextAlignedCenter();
    dc.drawTextInRect(text, new Rect(pillX, pillY + 1.5, pillW, pillH));
  } catch (e) {
    console.warn("Right avg label render error: " + e);
  }
}

function getHistoryArray(data, todayTotal, avgDaily) {
  if (data && Array.isArray(data.daily_history) && data.daily_history.length >= 3) {
    return data.daily_history;
  }
  // Smooth fallback curve if backend hasn't been redeployed yet
  return [
    { amount: avgDaily * 0.75 },
    { amount: avgDaily * 0.95 },
    { amount: avgDaily * 0.40 },
    { amount: avgDaily * 1.15 },
    { amount: avgDaily * 0.80 },
    { amount: avgDaily * 0.60 },
    { amount: todayTotal }
  ];
}

function getYesterdayAmount(data, history) {
  if (data && Array.isArray(data.daily_history) && data.daily_history.length >= 2) {
    const yest = data.daily_history[data.daily_history.length - 2];
    if (yest && typeof yest.amount !== "undefined") {
      return Number(yest.amount) || 0;
    }
  }
  if (Array.isArray(history) && history.length >= 2) {
    const yest = history[history.length - 2];
    if (yest && typeof yest.amount !== "undefined") {
      return Number(yest.amount) || 0;
    }
  }
  return 0;
}

function getTodayVariable(data, history) {
  if (data && typeof data.today_variable_total !== "undefined" && data.today_variable_total !== null) {
    return Number(data.today_variable_total) || 0;
  }
  if (data && Array.isArray(data.daily_history) && data.daily_history.length >= 1) {
    const todayEntry = data.daily_history[data.daily_history.length - 1];
    if (todayEntry && typeof todayEntry.amount !== "undefined") {
      return Number(todayEntry.amount) || 0;
    }
  }
  if (Array.isArray(history) && history.length >= 1) {
    const todayEntry = history[history.length - 1];
    if (todayEntry && typeof todayEntry.amount !== "undefined") {
      return Number(todayEntry.amount) || 0;
    }
  }
  return 0;
}

function calculateDayOverDayChange(todayTotal, yesterdayTotal) {
  if (yesterdayTotal > 0) {
    const diff = todayTotal - yesterdayTotal;
    const pct = Math.round((diff / yesterdayTotal) * 100);
    const absPct = Math.abs(pct);
    if (diff > 0) {
      return { pct: absPct, arrow: "▲ ", isIncrease: true, text: `▲ ${absPct}% yes.` };
    } else if (diff < 0) {
      return { pct: absPct, arrow: "▼ ", isIncrease: false, text: `▼ ${absPct}% yes.` };
    } else {
      return { pct: 0, arrow: "", isIncrease: false, text: "0% yes." };
    }
  } else if (todayTotal > 0) {
    return { pct: 100, arrow: "▲ ", isIncrease: true, text: "▲ 100% yes." };
  } else {
    return { pct: 0, arrow: "", isIncrease: false, text: "0% yes." };
  }
}

// =====================================================================
// 🖌️ UI & UTILITIES
// =====================================================================

function makeLinearGradient(topHex, bottomHex) {
  const gradient = new LinearGradient();
  gradient.colors = [new Color(topHex), new Color(bottomHex)];
  gradient.locations = [0.0, 1.0];
  return gradient;
}

function getSFSymbolImage(symbolName, pointSize = 12) {
  try {
    if (typeof SFSymbol !== "undefined" && SFSymbol && typeof SFSymbol.named === "function") {
      const sym = SFSymbol.named(symbolName);
      if (sym) {
        sym.applyFont(Font.systemFont(pointSize));
        return sym.image;
      }
    }
  } catch (e) { }
  return null;
}

// =====================================================================
// 🌐 DATA FETCHING & OFFLINE CACHE
// =====================================================================

async function fetchExpenseData() {
  const secret = (typeof args !== "undefined" && args && typeof args.widgetParameter === "string" && args.widgetParameter.trim().length > 0)
    ? args.widgetParameter.trim()
    : CONFIG.apiSecret;
  const baseUrl = CONFIG.apiUrl.replace(/\/+$/, "");
  const endpoint = `${baseUrl}/summary/entry-page`;

  try {
    const req = new Request(endpoint);
    req.method = "GET";
    req.headers = {
      "Content-Type": "application/json",
      "x-endpoint-secret": secret,
    };
    req.timeoutInterval = CONFIG.timeoutSeconds || 5;
    const json = await req.loadJSON();
    if (json && typeof json.today_total !== "undefined") {
      saveToCache(json);
      return json;
    }
  } catch (err) {
    console.warn("API fetch error or timeout: " + err);
  }

  // Fallback to local cache so iOS NEVER kills the widget with a timeout
  const cached = loadFromCache();
  if (cached) {
    return cached;
  }

  // Clean default placeholder if cache is empty on the very first run
  return {
    today_total: 0,
    today_variable_total: 0,
    month_total: 0,
    month_variable_total: 0,
    month_fixed_total: 0,
    avg_daily_variable_spend: 1000,
    daily_history: []
  };
}

function getCacheFilePath() {
  const fm = FileManager.local();
  const dir = fm.documentsDirectory();
  return fm.joinPath(dir, "expense_ledger_widget_cache.json");
}

function saveToCache(data) {
  try {
    const fm = FileManager.local();
    fm.writeString(getCacheFilePath(), JSON.stringify(data));
  } catch (e) { }
}

function loadFromCache() {
  try {
    const fm = FileManager.local();
    const path = getCacheFilePath();
    if (fm.fileExists(path)) {
      return JSON.parse(fm.readString(path));
    }
  } catch (e) { }
  return null;
}

// =====================================================================
// 🛠️ NUMBER FORMATTING & HELPERS
// =====================================================================

function formatCurrency(amount) {
  if (amount === null || amount === undefined || isNaN(amount)) {
    return "₹ -";
  }

  if (CONFIG.useCompactNumbers) {
    return formatCompactCurrency(amount);
  }

  try {
    const rounded = Math.round(amount);
    return `₹ ${rounded.toLocaleString("en-IN")}`;
  } catch (e) {
    return `₹ ${Math.round(amount)}`;
  }
}

function formatCompactCurrency(amount) {
  const abs = Math.abs(amount);
  const sign = amount < 0 ? "-" : "";

  function trimDecimals(val) {
    const fixed = val.toFixed(1);
    return fixed.replace(/\.0+$/, "").replace(/(\.[0-9]*[1-9])0+$/, "$1");
  }

  if (CONFIG.compactStyle === "standard") {
    if (abs >= 1000000000) return `${sign}₹ ${trimDecimals(abs / 1000000000)}B`;
    if (abs >= 1000000) return `${sign}₹ ${trimDecimals(abs / 1000000)}M`;
    if (abs >= 1000) return `${sign}₹ ${trimDecimals(abs / 1000)}K`;
  } else {
    if (abs >= 10000000) return `${sign}₹ ${trimDecimals(abs / 10000000)}Cr`;
    if (abs >= 100000) return `${sign}₹ ${trimDecimals(abs / 100000)}L`;
    if (abs >= 1000) return `${sign}₹ ${trimDecimals(abs / 1000)}K`;
  }

  try {
    return `${sign}₹ ${Math.round(abs).toLocaleString("en-IN")}`;
  } catch (e) {
    return `${sign}₹ ${Math.round(abs)}`;
  }
}

function getCurrentMonthName(short = true) {
  const shortMonths = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
  const longMonths = ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"];
  const d = new Date();
  const monthIdx = d.getMonth();
  return short ? shortMonths[monthIdx] : longMonths[monthIdx];
}
