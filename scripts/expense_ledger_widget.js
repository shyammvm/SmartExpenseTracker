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
// 🟢 CATEGORY DOT COMPONENT (F, G, S)
// =====================================================================

function createCategoryDot(parentStack, label, isOver) {
  const dotStack = parentStack.addStack();
  dotStack.size = new Size(16, 16);
  dotStack.cornerRadius = 8;
  dotStack.centerAlignContent();

  if (isOver) {
    // Red dot: Over budget
    dotStack.backgroundColor = new Color("#FF3B30", 0.95);
    dotStack.borderWidth = 0.75;
    dotStack.borderColor = new Color("#FFA39E", 0.85);
  } else {
    // Green dot: Under budget / safe
    dotStack.backgroundColor = new Color("#30D158", 0.95);
    dotStack.borderWidth = 0.75;
    dotStack.borderColor = new Color("#A3F7BF", 0.85);
  }

  const dotText = dotStack.addText(label);
  dotText.font = Font.boldSystemFont(9);
  dotText.textColor = new Color("#FFFFFF");
  dotText.centerAlignText();

  return dotStack;
}

// =====================================================================
// 🎨 SMALL WIDGET BUILDER (Stocks Card Style)
// =====================================================================

async function createSmallWidget(data) {
  const todayTotal = data ? Number(data.today_total) || 0 : 0;
  const avgDaily = data ? Number(data.avg_daily_variable_spend) || 1 : 1;

  // Determine State: Green (Safe <= Avg) vs Red (High > Avg)
  const isOverBudget = todayTotal > avgDaily;

  const topColor = isOverBudget ? CONFIG.redGradientTop : CONFIG.greenGradientTop;
  const bottomColor = isOverBudget ? CONFIG.redGradientBottom : CONFIG.greenGradientBottom;
  const subtextColor = isOverBudget ? CONFIG.redSubtext : CONFIG.greenSubtext;

  const widget = new ListWidget();
  widget.backgroundGradient = makeLinearGradient(topColor, bottomColor);
  widget.setPadding(10, 11, 10, 11);

  if (CONFIG.webAppUrl) {
    widget.url = CONFIG.webAppUrl;
  }

  // --- 1. TOP HEADER (Title + Total Spend + 3 Category Dots F, G, S) ---
  const headerRow = widget.addStack();
  headerRow.layoutHorizontally();
  headerRow.centerAlignContent();
  if (CONFIG.webAppUrl) headerRow.url = CONFIG.webAppUrl;

  const titleCol = headerRow.addStack();
  titleCol.layoutVertically();
  titleCol.spacing = 1;

  const monthTotal = data ? Number(data.month_total) || 0 : 0;
  const currentMonth = getCurrentMonthName(true);

  const titleText = titleCol.addText(CONFIG.widgetTitle);
  titleText.font = Font.boldSystemFont(14);
  titleText.textColor = new Color("#FFFFFF");

  // Dynamic Month Name + Updated Real Spend Total
  const subtitleText = titleCol.addText(
    `${currentMonth} · ${formatCurrency(monthTotal)}`
  );
  subtitleText.font = Font.systemFont(9);
  subtitleText.textColor = new Color("#FFFFFF", 0.85);
  subtitleText.minimumScaleFactor = 0.75;
  subtitleText.lineLimit = 1;

  headerRow.addSpacer();

  // 3 Category Dots: [ F ] [ G ] [ S ] (Food, Grocery, Shopping)
  const dotsStack = headerRow.addStack();
  dotsStack.layoutHorizontally();
  dotsStack.centerAlignContent();
  dotsStack.spacing = 3.5;

  const keyCats = (data && data.key_categories) ? data.key_categories : {};
  const fOver = keyCats.food ? Boolean(keyCats.food.is_over) : false;
  const gOver = keyCats.grocery ? Boolean(keyCats.grocery.is_over) : false;
  const sOver = keyCats.shopping ? Boolean(keyCats.shopping.is_over) : false;

  createCategoryDot(dotsStack, "F", fOver);
  createCategoryDot(dotsStack, "G", gOver);
  createCategoryDot(dotsStack, "S", sOver);

  widget.addSpacer(3);

  // --- 2. DAILY SPENDING GRAPH ---
  const history = getHistoryArray(data, todayTotal, avgDaily);
  const chartImg = renderAreaChart(148, 54, history, avgDaily, isOverBudget);
  if (chartImg) {
    const chartStack = widget.addStack();
    chartStack.layoutHorizontally();
    chartStack.centerAlignContent();
    if (CONFIG.webAppUrl) chartStack.url = CONFIG.webAppUrl;

    const chartWidgetImg = chartStack.addImage(chartImg);
    chartWidgetImg.imageSize = new Size(148, 54);
  }

  widget.addSpacer(3);

  // --- 3. CURRENT SAVINGS MICRO-BAR ---
  const currentSavings = data ? Number(data.current_savings || 0) : 0;

  const savBar = widget.addStack();
  savBar.layoutHorizontally();
  savBar.centerAlignContent();
  savBar.backgroundColor = new Color("#000000", 0.28);
  savBar.cornerRadius = 4;
  savBar.setPadding(2, 6, 2, 6);
  if (CONFIG.webAppUrl) savBar.url = CONFIG.webAppUrl;

  const savLabel = savBar.addText("Savings: ");
  savLabel.font = Font.systemFont(8.5);
  savLabel.textColor = new Color("#FFFFFF", 0.85);

  const savVal = savBar.addText(formatCurrency(currentSavings));
  savVal.font = Font.boldSystemFont(8.5);
  savVal.textColor = currentSavings >= 0 ? new Color("#30D158") : new Color("#FF453A");
  savVal.minimumScaleFactor = 0.8;
  savVal.lineLimit = 1;

  widget.addSpacer(3);

  // --- 4. BOTTOM ROW: SPEND AMOUNT + DAY-OVER-DAY PERCENTAGE ---
  const bottomRow = widget.addStack();
  bottomRow.layoutHorizontally();
  bottomRow.bottomAlignContent();
  if (CONFIG.webAppUrl) bottomRow.url = CONFIG.webAppUrl;

  const todayStack = bottomRow.addStack();
  todayStack.layoutVertically();
  todayStack.spacing = 1;

  const amountText = todayStack.addText(formatCurrency(todayTotal));
  amountText.font = Font.boldSystemFont(16);
  amountText.textColor = new Color("#FFFFFF");
  amountText.minimumScaleFactor = 0.8;
  amountText.lineLimit = 1;

  const todayVar = getTodayVariable(data, history);
  const varText = todayStack.addText(formatCurrency(todayVar));
  varText.font = Font.systemFont(9.5);
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
  widget.setPadding(12, 14, 12, 14);

  if (CONFIG.webAppUrl) {
    widget.url = CONFIG.webAppUrl;
  }

  // --- TOP ROW ---
  const headerRow = widget.addStack();
  headerRow.layoutHorizontally();
  headerRow.centerAlignContent();
  if (CONFIG.webAppUrl) headerRow.url = CONFIG.webAppUrl;

  const monthTotal = data ? Number(data.month_total) || 0 : 0;
  const monthVar = data ? Number(data.month_variable_total) || 0 : 0;
  const totalBudget = data ? Number(data.total_budget || data.total_proposed_variable_budget || 0) : 0;
  const currentSavings = data ? Number(data.current_savings || 0) : 0;
  const currentMonth = getCurrentMonthName(true);

  // Left column: Title + Totals + Budget vs Savings
  const titleCol = headerRow.addStack();
  titleCol.layoutVertically();
  titleCol.spacing = 1;

  const title = titleCol.addText(CONFIG.widgetTitle);
  title.font = Font.boldSystemFont(14);
  title.textColor = new Color("#FFFFFF");

  const sub = titleCol.addText(`${currentMonth} TOTAL: ${formatCurrency(monthTotal)} (Var ${formatCurrency(monthVar)})`);
  sub.font = Font.systemFont(9);
  sub.textColor = new Color("#FFFFFF", 0.85);

  const savRow = titleCol.addStack();
  savRow.layoutHorizontally();
  savRow.spacing = 3;

  const sTitle = savRow.addText("SAVINGS: ");
  sTitle.font = Font.boldSystemFont(8.5);
  sTitle.textColor = new Color("#FFFFFF", 0.85);

  const sVal = savRow.addText(formatCurrency(currentSavings));
  sVal.font = Font.boldSystemFont(8.5);
  sVal.textColor = currentSavings >= 0 ? new Color("#30D158") : new Color("#FF453A");

  headerRow.addSpacer();

  // Center: 3 Category Dots F, G, S
  const keyCats = (data && data.key_categories) ? data.key_categories : {};
  const fOver = keyCats.food ? Boolean(keyCats.food.is_over) : false;
  const gOver = keyCats.grocery ? Boolean(keyCats.grocery.is_over) : false;
  const sOver = keyCats.shopping ? Boolean(keyCats.shopping.is_over) : false;

  const dotsContainer = headerRow.addStack();
  dotsContainer.layoutHorizontally();
  dotsContainer.centerAlignContent();
  dotsContainer.backgroundColor = new Color("#000000", 0.25);
  dotsContainer.cornerRadius = 6;
  dotsContainer.setPadding(3, 6, 3, 6);
  dotsContainer.spacing = 4;

  createCategoryDot(dotsContainer, "F", fOver);
  createCategoryDot(dotsContainer, "G", gOver);
  createCategoryDot(dotsContainer, "S", sOver);

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

  widget.addSpacer(8);

  // --- PANORAMIC DAILY SPEND GRAPH ---
  const chartImg = renderAreaChart(292, 70, history, avgDaily, isOverBudget);
  if (chartImg) {
    const chartStack = widget.addStack();
    chartStack.layoutHorizontally();
    chartStack.centerAlignContent();
    if (CONFIG.webAppUrl) chartStack.url = CONFIG.webAppUrl;

    const chartWidgetImg = chartStack.addImage(chartImg);
    chartWidgetImg.imageSize = new Size(292, 70);
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
      // Defensive fallback: If key_categories not populated yet, fetch from /budgets
      if (!json.key_categories) {
        try {
          const bReq = new Request(`${baseUrl}/budgets`);
          bReq.headers = { "x-endpoint-secret": secret };
          bReq.timeoutInterval = 3;
          const bJson = await bReq.loadJSON();
          if (bJson) {
            json.monthly_income = Number(bJson.monthly_income || 0);
            json.total_budget = Number(bJson.totals?.total_budgeted_outflow || bJson.totals?.total_proposed_variable_budget || 0);
            json.current_savings = Number(bJson.savings?.based_on_spending?.current_savings || 0);
            const bCats = bJson.categories || [];
            json.key_categories = {};
            for (const c of bCats) {
              const nameLower = (c.category || "").toLowerCase();
              if (nameLower === "food" || nameLower === "grocery" || nameLower === "shopping") {
                json.key_categories[nameLower] = {
                  name: c.category,
                  spent: Number(c.this_month_spent || 0),
                  budget: Number(c.proposed_budget || 0),
                  is_over: Number(c.this_month_spent || 0) > Number(c.proposed_budget || 0),
                  remaining: Number(c.remaining || 0)
                };
              }
            }
          }
        } catch (bErr) {
          console.warn("Budgets secondary fetch: " + bErr);
        }
      }

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
    daily_history: [],
    monthly_income: 0,
    total_budget: 0,
    current_savings: 0,
    key_categories: {
      food: { name: "Food", is_over: false, spent: 0, budget: 0 },
      grocery: { name: "Grocery", is_over: false, spent: 0, budget: 0 },
      shopping: { name: "Shopping", is_over: false, spent: 0, budget: 0 },
    }
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
