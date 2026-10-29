(() => {
  "use strict";
  const data = JSON.parse(document.getElementById("chart-data").textContent);
  const toolbar = document.querySelector(".time-controls");
  const startInput = document.getElementById("chart-start");
  const endInput = document.getElementById("chart-end");
  const status = document.getElementById("chart-range-status");
  const dayMs = 86400000;
  const epoch = date => Date.parse(date + "T00:00:00Z");
  const iso = ms => new Date(ms).toISOString().slice(0, 10);
  const validDate = value => /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(epoch(value)) && iso(epoch(value)) === value;
  const fmt = value => value.toLocaleString("zh-CN", {maximumFractionDigits: 2});
  const measure = document.createElement("canvas").getContext("2d");
  measure.font = "11px system-ui";
  const ns = "http://www.w3.org/2000/svg";
  function el(tag, attrs = {}, text = null) {
    const node = document.createElementNS(ns, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    if (text !== null) node.textContent = text;
    return node;
  }
  function html(tag, className, text) {
    const node = document.createElement(tag);
    node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  let range = [Math.max(epoch(data.start), epoch(data.end) - data.default_days * dayMs), epoch(data.end)];
  const views = [];
  for (const chart of data.charts) {
    const host = document.querySelector(`[data-chart-id="${chart.id}"]`);
    if (!host) continue;
    const fallback = host.querySelector("img");
    const panel = html("div", "interactive-chart");
    panel.hidden = true;
    if (!chart.id.startsWith("index-")) panel.append(html("div", "interactive-title", chart.title));
    const svg = el("svg", {role: "img", tabindex: "0", "aria-label": chart.title, "data-plot": chart.id});
    const legend = html("div", "chart-legend");
    const values = html("div", "chart-values");
    const coverage = html("p", "chart-coverage");
    panel.append(svg, legend, values, coverage);
    host.append(panel);
    views.push({chart, host, fallback, panel, svg, legend, values, coverage, cursor: null, select: null});
  }
  function render(view) {
    const {chart, host, panel, svg, values, coverage} = view;
    const width = Math.max(240, Math.floor(host.getBoundingClientRect().width));
    const height = 244, right = 14, top = 25, bottom = 32;
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.replaceChildren();
    const title = el("title", {}, `${chart.title}，${iso(range[0])}至${iso(range[1])}，${chart.unit}`);
    svg.append(title);
    const lines = chart.series.map(s => ({...s, points: s.points.filter(p => epoch(p[0]) >= range[0] && epoch(p[0]) <= range[1])}));
    view.legend.replaceChildren();
    const keys = new Set();
    for (const series of lines) {
      const key = series.label + series.color + series.dashed;
      if (keys.has(key) || !series.points.some(p => p[1] !== null)) continue;
      keys.add(key);
      const item = html("span", "");
      const swatch = html("i", "chart-swatch");
      swatch.style.borderColor = series.color;
      if (series.dashed) swatch.style.borderTopStyle = "dashed";
      item.append(swatch, document.createTextNode(series.label));
      view.legend.append(item);
    }
    const real = lines.flatMap(s => s.points).filter(p => p[1] !== null && Number.isFinite(p[1]));
    const ys = real.map(p => p[1]).concat(chart.guides.map(g => g.value));
    let low = chart.y_bounds ? chart.y_bounds[0] : (ys.length ? Math.min(...ys) : 0);
    let high = chart.y_bounds ? chart.y_bounds[1] : (ys.length ? Math.max(...ys) : 1);
    if (!chart.y_bounds) {
      const pad = (high - low || Math.max(Math.abs(high) * 0.05, 1)) * 0.12;
      low -= pad; high += pad;
    }
    const labels = Array.from({length:5}, (_,i) => fmt(low + (high-low)*i/4));
    const left = Math.max(51, Math.ceil(Math.max(...labels.map(v=>measure.measureText(v).width)))+12);
    const w = width - left - right, h = height - top - bottom;
    const x = ms => left + (ms - range[0]) / Math.max(dayMs, range[1] - range[0]) * w;
    const y = value => top + (high - value) / (high - low) * h;
    const text = (x, y, value, extra = {}) => el("text", {x, y, fill: "#63757a", "font-size": 11, ...extra}, value);
    svg.append(text(left, 13, chart.unit));
    for (let i = 0; i <= 4; i++) {
      const value = low + (high - low) * i / 4, py = y(value);
      svg.append(el("line", {x1:left, y1:py, x2:width-right, y2:py, stroke:"#e5eaed"}));
      svg.append(text(left - 7, py + 4, fmt(value), {"text-anchor":"end"}));
    }
    const ticks = width < 350 ? 2 : 3;
    for (let i = 0; i <= ticks; i++) {
      const ms = range[0] + (range[1] - range[0]) * i / ticks;
      svg.append(text(x(ms), height-10, iso(ms).slice(5), {"text-anchor":i===0?"start":i===ticks?"end":"middle"}));
    }
    for (const guide of chart.guides) {
      if (guide.value < low || guide.value > high) continue;
      svg.append(el("line", {x1:left, y1:y(guide.value), x2:width-right, y2:y(guide.value),
        stroke:guide.color, "stroke-width":1, "stroke-dasharray":"4 3", "data-guide":guide.value}));
      svg.append(text(left+4, y(guide.value)-4, guide.label, {fill:guide.color, "font-size":10}));
    }
    for (const series of lines) {
      let path = "", connected = false;
      for (const [date, value] of series.points) {
        if (value === null || !Number.isFinite(value)) { connected = false; continue; }
        const px=x(epoch(date)), py=y(value);
        path += `${connected?"L":"M"}${px.toFixed(2)},${py.toFixed(2)} `;
        connected=true;
        svg.append(el("circle", {cx:px, cy:py, r:series.points.length<12?2.6:1.5,
          fill:series.color, "data-date":date, "data-value":value}));
      }
      if (path) svg.append(el("path", {d:path, fill:"none", stroke:series.color, "stroke-width":1.7,
        "stroke-dasharray":series.dashed?"5 4":"none", "data-series":series.label}));
    }
    const observedDates = [...new Set(real.map(p => p[0]))].sort();
    coverage.textContent = observedDates.length ? `有效观测 ${observedDates[0]} 至 ${observedDates.at(-1)} · ${observedDates.length} 个日期` : "此区间无有效观测";
    if (!real.length) svg.append(text(left+w/2, top+h/2, "此区间无有效观测", {"text-anchor":"middle"}));
    const dates = [...new Set(lines.flatMap(s=>s.points.map(p=>p[0])))].sort();
    const cursor = el("line", {x1:0, x2:0, y1:top, y2:height-bottom, stroke:"#6e8088", "stroke-dasharray":"2 3", visibility:"hidden"});
    svg.append(cursor);
    function show(date) {
      view.cursor = date;
      values.replaceChildren();
      if (!date) { values.textContent = "暂无区间数据"; cursor.setAttribute("visibility", "hidden"); return; }
      cursor.setAttribute("x1", x(epoch(date))); cursor.setAttribute("x2", x(epoch(date)));
      cursor.setAttribute("visibility", "visible");
      values.append(html("strong", "", date + " · "));
      const found = new Set();
      for (const s of lines) {
        const point = s.points.find(p => p[0] === date);
        if (!point || point[1] === null) continue;
        const label = s.label + " " + fmt(point[1]);
        if (found.has(label)) continue;
        found.add(label); values.append(document.createTextNode((found.size>1?"；":"") + label));
      }
      if (!found.size) values.append(document.createTextNode("缺测，不作插值"));
    }
    function nearest(ms) {
      return dates.reduce((best, d) => best===null || Math.abs(epoch(d)-ms)<Math.abs(epoch(best)-ms)?d:best, null);
    }
    view.select = show;
    svg.onpointermove = event => {
      const rect = svg.getBoundingClientRect();
      const fraction = Math.max(0, Math.min(1, (event.clientX-rect.left-left)/w));
      show(nearest(range[0] + fraction * (range[1]-range[0])));
    };
    svg.onpointerleave = () => show(observedDates.at(-1) || null);
    svg.onkeydown = event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      let index = dates.indexOf(view.cursor);
      if (event.key === "Home") index=0;
      else if (event.key === "End") index=dates.length-1;
      else index=Math.max(0,Math.min(dates.length-1,index+(event.key==="ArrowLeft"?-1:1)));
      show(dates[index] || null);
    };
    show(observedDates.at(-1) || null);
    svg.dataset.start = iso(range[0]); svg.dataset.end = iso(range[1]);
    panel.hidden = false;
    view.fallback.classList.add("chart-fallback"); view.fallback.hidden = true;
  }
  function refresh() {
    for (const view of views) {
      try { render(view); } catch (_) { view.panel.hidden=true; view.fallback.hidden=false; }
    }
    status.textContent=`${iso(range[0])} 至 ${iso(range[1])} · 冻结数据截至 ${data.end}`;
  }
  function preset(value) {
    range=[value==="all"?epoch(data.start):Math.max(epoch(data.start),epoch(data.end)-Number(value)*dayMs),epoch(data.end)];
    startInput.value=iso(range[0]); endInput.value=iso(range[1]); refresh();
  }
  for (const input of [startInput,endInput]) {
    input.min=data.start; input.max=data.end;
    input.addEventListener("change", () => {
      const a=startInput.value, b=endInput.value;
      if (!validDate(a) || !validDate(b) || a<data.start || b>data.end || a>=b) {
        status.textContent=`日期无效：起始须早于截止，范围 ${data.start} 至 ${data.end}`; return;
      }
      range=[epoch(a),epoch(b)];
      document.querySelectorAll('input[name="chart-range"]').forEach(r=>{r.checked=false;});
      refresh();
    });
  }
  document.querySelectorAll('input[name="chart-range"]').forEach(r=>r.addEventListener("change",()=>preset(r.value)));
  let pending;
  const resize = () => { cancelAnimationFrame(pending); pending=requestAnimationFrame(refresh); };
  window.addEventListener("resize",resize);
  toolbar.hidden=false; preset(String(data.default_days));
})();
