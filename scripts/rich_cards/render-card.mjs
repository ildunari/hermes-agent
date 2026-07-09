#!/usr/bin/env node
import fs from 'node:fs/promises';
import path from 'node:path';
import React from 'react';
import { renderToPng, renderToSvg } from '@message-ui/render';
import { Section, Row, Column, Text, Heading } from '@message-ui/components';

function arg(name, fallback = '') {
  const idx = process.argv.indexOf(name);
  return idx >= 0 && process.argv[idx + 1] ? process.argv[idx + 1] : fallback;
}

const input = arg('--input');
const output = arg('--output');
const svgOutput = arg('--svg');
if (!input || !output) {
  console.error('usage: render-card.mjs --input spec.json --output card.png [--svg card.svg]');
  process.exit(2);
}

const spec = JSON.parse(await fs.readFile(input, 'utf8'));
const look = resolveLook(spec);
const tokens = designTokens(spec, look);
const width = Math.max(420, Math.min(Number(spec?.style?.width) || tokens.width, 1200));
const height = estimateHeight(spec, tokens);
const element = cardElement(spec, tokens, width, height);
const svg = await renderToSvg(element, { width, height });
const png = await renderToPng(element, { width, height, scale: 2 });
await fs.mkdir(path.dirname(output), { recursive: true });
await fs.writeFile(output, png);
if (svgOutput) await fs.writeFile(svgOutput, svg);

function h(component, props, ...children) {
  return React.createElement(component, props || {}, ...children);
}

function resolveLook(spec) {
  const requested = String(spec?.style?.look || 'auto').toLowerCase();
  const allowed = new Set(['default', 'dashboard', 'minimal', 'editorial', 'terminal', 'receipt', 'status']);
  if (allowed.has(requested)) return requested;
  if (spec.kind === 'receipt') return 'receipt';
  if (spec.kind === 'status') return 'status';
  if (spec.kind === 'timeline') return 'terminal';
  if (spec.kind === 'chart' || spec.kind === 'metric_grid') return 'dashboard';
  if (spec.kind === 'comparison' || spec.kind === 'table') return 'minimal';
  return 'default';
}

function designTokens(spec, look) {
  const themeRequest = String(spec?.style?.theme || 'auto').toLowerCase();
  const autoDark = ['dashboard', 'terminal', 'status', 'default'].includes(look);
  const dark = themeRequest === 'dark' || (themeRequest !== 'light' && autoDark);
  const density = String(spec?.style?.density || 'normal').toLowerCase();
  const accent = safeAccent(spec?.style?.accent) || defaultAccent(look, dark);
  const scale = density === 'compact'
    ? { outer: 14, panelPad: 18, gap: 12, row: 54, title: 34, body: 21, small: 17, metric: 38 }
    : density === 'roomy'
      ? { outer: 20, panelPad: 28, gap: 20, row: 70, title: 42, body: 25, small: 20, metric: 48 }
      : { outer: 16, panelPad: 22, gap: 16, row: 62, title: 38, body: 23, small: 18, metric: 44 };

  const base = {
    look,
    dark,
    accent,
    density,
    width: 980,
    ...scale,
    font: "Geist, 'SF Pro Display', -apple-system, BlinkMacSystemFont, sans-serif",
    mono: "'JetBrains Mono', 'SF Mono', ui-monospace, monospace",
    radius: 16,
    panelRadius: 13,
    shadow: 'none',
    headerCase: 'none',
  };

  const palettes = {
    dashboard: dark
      ? { bg: '#0b1020', panel: '#10182a', panel2: '#16213a', fg: '#f8fafc', muted: '#a9b6cc', border: '#26334f', badgeBg: '#182542', badgeFg: '#dbeafe' }
      : { bg: '#f6f8fc', panel: '#ffffff', panel2: '#eef3fb', fg: '#111827', muted: '#4b5563', border: '#d8e0ec', badgeBg: '#e8f0ff', badgeFg: '#1e3a8a' },
    minimal: dark
      ? { bg: '#111111', panel: '#171717', panel2: '#202020', fg: '#f5f5f4', muted: '#c8c6bf', border: '#3a3a3a', badgeBg: '#252525', badgeFg: '#f0eeea' }
      : { bg: '#fbfbfa', panel: '#ffffff', panel2: '#f4f4f2', fg: '#171717', muted: '#3f4440', border: '#d4d4cd', badgeBg: '#eeeeea', badgeFg: '#222222' },
    editorial: dark
      ? { bg: '#15110d', panel: '#1f1a14', panel2: '#2a2118', fg: '#fff8ed', muted: '#dccbb0', border: '#44372a', badgeBg: '#332618', badgeFg: '#ffdda8' }
      : { bg: '#fbfaf7', panel: '#fffdf8', panel2: '#f3efe7', fg: '#1d1914', muted: '#57493a', border: '#d8c9b5', badgeBg: '#f1e2cb', badgeFg: '#5b310a' },
    terminal: dark
      ? { bg: '#0a0d0a', panel: '#101510', panel2: '#151c15', fg: '#e8f1e8', muted: '#9eaa9e', border: '#2a352a', badgeBg: '#172217', badgeFg: '#b9ffbf' }
      : { bg: '#f4f4f0', panel: '#fbfbf7', panel2: '#ebebe5', fg: '#111111', muted: '#55584f', border: '#c9cac0', badgeBg: '#e8e8de', badgeFg: '#222222' },
    receipt: dark
      ? { bg: '#121212', panel: '#1a1a18', panel2: '#22221f', fg: '#f8f5ef', muted: '#d0c4b5', border: '#443d34', badgeBg: '#29251f', badgeFg: '#f7e0b8' }
      : { bg: '#f5f2eb', panel: '#fffdfa', panel2: '#f2ede4', fg: '#201a14', muted: '#5d5045', border: '#d2c2ae', badgeBg: '#eadcc5', badgeFg: '#613708' },
    status: dark
      ? { bg: '#07111c', panel: '#0d1b2a', panel2: '#12263a', fg: '#f3f8ff', muted: '#a9b9ca', border: '#23415d', badgeBg: '#12324e', badgeFg: '#d6efff' }
      : { bg: '#f5f9fc', panel: '#ffffff', panel2: '#edf5fb', fg: '#102033', muted: '#53657a', border: '#d5e5f1', badgeBg: '#e4f4ff', badgeFg: '#0f4a6e' },
    default: dark
      ? { bg: '#10131a', panel: '#171b24', panel2: '#1f2530', fg: '#f6f7fb', muted: '#aeb7c8', border: '#2b3240', badgeBg: '#202838', badgeFg: '#dce6ff' }
      : { bg: '#f8fafc', panel: '#ffffff', panel2: '#eef2f7', fg: '#111827', muted: '#475569', border: '#dbe3ef', badgeBg: '#e8eef8', badgeFg: '#1f2937' },
  };

  const selected = palettes[look] || palettes.default;
  const shaped = { ...base, ...selected };
  if (look === 'terminal') Object.assign(shaped, { radius: 0, panelRadius: 0, font: base.mono, headerCase: 'uppercase', width: 900 });
  if (look === 'receipt') Object.assign(shaped, { radius: 8, panelRadius: 6, width: 840, font: "'SF Pro Text', -apple-system, BlinkMacSystemFont, sans-serif" });
  if (look === 'editorial') Object.assign(shaped, { radius: 12, panelRadius: 10, width: 980 });
  if (look === 'minimal') Object.assign(shaped, { radius: 10, panelRadius: 8, width: 980 });
  if (look === 'dashboard' || look === 'status') Object.assign(shaped, { radius: 12, panelRadius: 10, width: 1040 });
  return shaped;
}

function safeAccent(value) {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  return /^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$/.test(trimmed) ? trimmed : null;
}

function defaultAccent(look, dark) {
  if (look === 'terminal') return dark ? '#8cff9a' : '#1f6f3a';
  if (look === 'receipt') return '#a16207';
  if (look === 'editorial') return dark ? '#f4b86a' : '#9a4f13';
  if (look === 'minimal') return dark ? '#d6d3d1' : '#111111';
  if (look === 'status') return '#38bdf8';
  if (look === 'dashboard') return '#7c9cff';
  return '#7c9cff';
}

function cardElement(spec, t, width, height) {
  const head = header(spec, t);
  return h(Section, { style: { width, height, padding: t.outer, backgroundColor: t.bg, color: t.fg, fontFamily: t.font, display: 'flex', flexDirection: 'column', gap: t.gap } },
    head,
    h(Section, { style: { backgroundColor: t.panel, border: `1px solid ${t.border}`, borderRadius: t.panelRadius, padding: t.panelPad, display: 'flex', flexDirection: 'column', gap: t.gap, flex: 1 } }, renderBody(spec, t))
  );
}

function header(spec, t) {
  if (!spec.title && !spec.subtitle) return null;
  const title = spec.title;
  const subtitle = spec.subtitle;
  const textTransform = t.headerCase === 'uppercase' ? 'uppercase' : 'none';
  return h(Column, { style: { display: 'flex', flexDirection: 'column', gap: 8, borderBottom: `1px solid ${t.border}`, paddingBottom: 12 } },
    title ? h(Heading, { level: 2, style: { color: t.fg, margin: 0, fontSize: t.title, lineHeight: 1.08, letterSpacing: '-0.025em', textTransform } }, String(title)) : null,
    subtitle ? h(Text, { style: { color: t.muted, margin: 0, fontSize: t.body, lineHeight: 1.35, maxWidth: 860 } }, String(subtitle)) : null,
  );
}

function renderBody(spec, t) {
  if (['table', 'comparison', 'status'].includes(spec.kind) && spec.columns && spec.rows) return table(spec, t);
  if (spec.kind === 'chart' && spec.chart) return chart(spec.chart, t);
  if (spec.kind === 'metric_grid' && spec.metrics) return metrics(spec.metrics, t);
  if (spec.kind === 'status' && spec.metrics) return metrics(spec.metrics, t);
  if (spec.kind === 'timeline') return timeline(spec, t);
  if (spec.kind === 'receipt') return receipt(spec, t);
  return h(Text, { style: { color: t.fg, fontSize: t.body, lineHeight: 1.35, whiteSpace: 'pre-wrap' } }, JSON.stringify(spec.data || spec.items || spec.events || {}, null, 2));
}

function table(spec, t) {
  const cols = spec.columns.map(String);
  const rows = spec.rows.slice(0, 30);
  const colWidth = `${100 / Math.max(cols.length, 1)}%`;
  const children = [];
  children.push(h(Row, { key: 'head', style: { display: 'flex', backgroundColor: t.panel2, border: `1px solid ${t.border}`, borderRadius: t.look === 'terminal' ? 0 : 8, padding: '10px 12px', gap: 8 } },
    ...cols.map((c, i) => h(Column, { key: i, style: { width: colWidth, flexBasis: colWidth } }, h(Text, { style: { color: t.muted, fontSize: t.small + 1, fontWeight: 800, margin: 0, fontFamily: t.look === 'terminal' ? t.mono : t.font, textTransform: t.look === 'terminal' ? 'uppercase' : 'none', letterSpacing: t.look === 'terminal' ? '0.05em' : 0 } }, c)))
  ));
  for (let r = 0; r < rows.length; r++) {
    children.push(h(Row, { key: `r${r}`, style: { display: 'flex', gap: 8, minHeight: t.row, padding: '9px 12px', backgroundColor: r % 2 === 0 ? 'transparent' : t.panel2, borderBottom: r === rows.length - 1 ? 'none' : `1px solid ${t.border}` } },
      ...cols.map((_, c) => h(Column, { key: c, style: { width: colWidth, flexBasis: colWidth } }, h(Text, { style: { color: t.fg, fontSize: t.body, lineHeight: 1.25, margin: 0, overflowWrap: 'break-word', fontFamily: shouldUseMono(spec, c, t) ? t.mono : t.font } }, String((rows[r] || [])[c] ?? ''))))
    ));
  }
  if (spec.rows.length > rows.length) children.push(h(Text, { key: 'more', style: { color: t.muted, fontSize: t.body, margin: '8px 0 0 0' } }, `+ ${spec.rows.length - rows.length} more rows`));
  return h(Section, { style: { display: 'flex', flexDirection: 'column' } }, ...children);
}

function shouldUseMono(spec, colIndex, t) {
  if (t.look === 'terminal') return true;
  const header = String(spec.columns?.[colIndex] || '').toLowerCase();
  return /id|time|date|cost|amount|qty|count|score|version|status|result/.test(header);
}

function chart(chart, t) {
  const labels = chart.labels || [];
  const series = chart.series || [];
  const allValues = series.flatMap(s => s.values || []);
  const max = Math.max(...allValues.map(v => Math.abs(Number(v) || 0)), 1);
  const palette = [t.accent, '#60d394', '#fbbf24', '#f87171'];
  const rows = [];
  for (let sidx = 0; sidx < series.length; sidx++) {
    const current = series[sidx] || { values: [] };
    rows.push(h(Text, { key: `legend${sidx}`, style: { color: t.muted, fontSize: t.small + 2, margin: sidx ? '12px 0 2px 0' : '0 0 2px 0', fontWeight: 700 } }, String(current.name || `Series ${sidx + 1}`)));
    for (let i = 0; i < (current.values || []).length; i++) {
      const v = current.values[i];
      const pct = Math.max(2, Math.round((Math.abs(Number(v) || 0) / max) * 100));
      rows.push(h(Row, { key: `${sidx}-${i}`, style: { display: 'flex', alignItems: 'center', gap: 12, minHeight: 34 } },
        h(Text, { style: { width: 150, color: t.muted, fontSize: t.body - 1, margin: 0 } }, String(labels[i] ?? i + 1)),
        h(Section, { style: { flex: 1, height: 24, borderRadius: t.look === 'terminal' ? 0 : 10, border: `1px solid ${t.border}`, backgroundColor: t.panel2, overflow: 'hidden' } },
          h(Section, { style: { width: `${pct}%`, height: 24, backgroundColor: palette[sidx % palette.length], borderRadius: t.look === 'terminal' ? 0 : 9 } })
        ),
        h(Text, { style: { width: 100, color: t.fg, fontSize: t.body - 1, margin: 0, textAlign: 'right', fontFamily: t.mono } }, `${v}${chart.unit ? ` ${chart.unit}` : ''}`)
      ));
    }
  }
  return h(Section, { style: { display: 'flex', flexDirection: 'column', gap: 8 } }, ...rows);
}

function metrics(metrics, t) {
  return h(Row, { style: { display: 'flex', flexWrap: 'wrap', gap: t.gap } },
    ...metrics.slice(0, 12).map((m, i) => h(Section, { key: i, style: { width: t.density === 'roomy' ? 240 : 230, minHeight: t.density === 'compact' ? 104 : 122, backgroundColor: i % 2 === 0 ? 'transparent' : t.panel2, border: `1px solid ${t.border}`, borderRadius: t.panelRadius, padding: t.panelPad, display: 'flex', flexDirection: 'column', gap: 8 } },
      h(Text, { style: { color: t.muted, fontSize: t.small + 1, margin: 0, fontWeight: 700 } }, String(m.label ?? 'Metric')),
      h(Text, { style: { color: t.fg, fontSize: t.metric, fontWeight: 850, margin: 0, lineHeight: 1.05, fontFamily: t.look === 'terminal' ? t.mono : t.font } }, String(m.value ?? '')),
      m.status || m.note ? h(Text, { style: { color: metricNoteColor(m.status || m.note, t), fontSize: t.small + 1, margin: 0, fontFamily: t.mono, fontWeight: 700 } }, String(m.status || m.note)) : null,
    ))
  );
}

function metricNoteColor(value, t) {
  const text = String(value || '').toLowerCase();
  if (/pass|passed|ready|ok|success|green/.test(text)) return t.dark ? '#86efac' : '#166534';
  if (/pending|wait|warn|review|yellow|amber/.test(text)) return t.dark ? '#fde68a' : '#92400e';
  if (/fail|error|red|blocked|down/.test(text)) return t.dark ? '#fca5a5' : '#991b1b';
  return t.accent;
}


function timeline(spec, t) {
  const rows = spec.events || (spec.rows || []).map(r => ({ time: r[0], title: r[1], note: r[2] }));
  return h(Section, { style: { display: 'flex', flexDirection: 'column', gap: 0, borderTop: `1px solid ${t.border}` } },
    ...rows.slice(0, 18).map((e, i) => h(Row, { key: i, style: { display: 'flex', gap: 14, borderBottom: `1px solid ${t.border}`, padding: `${Math.max(9, t.row / 5)}px 0` } },
      h(Text, { style: { color: t.accent, width: 140, fontSize: t.small + 1, margin: 0, fontFamily: t.mono } }, String(e.time || e.when || '')),
      h(Column, { style: { display: 'flex', flexDirection: 'column', gap: 4, flex: 1 } },
        h(Text, { style: { color: t.fg, fontSize: t.body + 1, fontWeight: 750, margin: 0 } }, String(e.title || e.label || e.event || 'Event')),
        e.note || e.status ? h(Text, { style: { color: t.muted, fontSize: t.body - 1, lineHeight: 1.3, margin: 0 } }, String(e.note || e.status)) : null,
      )
    ))
  );
}

function receipt(spec, t) {
  const rawItems = spec.items || (spec.rows || []).map(r => ({ label: r?.[0], qty: r?.[1], amount: r?.[2] }));
  const items = rawItems.slice(0, 24);
  const rows = [];
  rows.push(h(Row, { key: 'head', style: { display: 'flex', borderBottom: `1px solid ${t.border}`, paddingBottom: 10, gap: 10 } },
    h(Text, { style: { flex: 1, color: t.muted, fontSize: t.small + 1, fontWeight: 800, margin: 0 } }, 'Item'),
    h(Text, { style: { width: 76, color: t.muted, fontSize: t.small + 1, fontWeight: 800, margin: 0, textAlign: 'right' } }, 'Qty'),
    h(Text, { style: { width: 136, color: t.muted, fontSize: t.small + 1, fontWeight: 800, margin: 0, textAlign: 'right' } }, 'Amount')
  ));
  for (let i = 0; i < items.length; i++) {
    const item = items[i] || {};
    const label = item.label ?? item.name ?? item.description ?? 'Item';
    const qty = item.qty ?? item.quantity ?? '';
    const amount = item.amount ?? item.price ?? item.total ?? '';
    rows.push(h(Row, { key: `item${i}`, style: { display: 'flex', alignItems: 'flex-start', gap: 10, borderBottom: `1px solid ${t.border}`, padding: '10px 0' } },
      h(Text, { style: { flex: 1, color: t.fg, fontSize: t.body, lineHeight: 1.25, margin: 0, overflowWrap: 'break-word' } }, String(label)),
      h(Text, { style: { width: 76, color: t.muted, fontSize: t.body - 1, margin: 0, textAlign: 'right', fontFamily: t.mono } }, String(qty)),
      h(Text, { style: { width: 136, color: t.fg, fontSize: t.body - 1, margin: 0, textAlign: 'right', fontFamily: t.mono } }, String(amount))
    ));
  }
  const totals = spec.data || {};
  const totalRows = ['subtotal', 'tax', 'tip', 'total'].filter(k => totals[k] !== undefined && totals[k] !== null && totals[k] !== '');
  for (const key of totalRows) {
    rows.push(h(Row, { key, style: { display: 'flex', justifyContent: 'flex-end', gap: 14, paddingTop: key === 'total' ? 12 : 5 } },
      h(Text, { style: { width: 150, color: key === 'total' ? t.fg : t.muted, fontSize: key === 'total' ? t.body + 3 : t.body - 1, fontWeight: key === 'total' ? 850 : 600, margin: 0, textAlign: 'right' } }, key[0].toUpperCase() + key.slice(1)),
      h(Text, { style: { width: 150, color: key === 'total' ? t.accent : t.fg, fontSize: key === 'total' ? t.body + 5 : t.body - 1, fontWeight: key === 'total' ? 900 : 650, margin: 0, textAlign: 'right', fontFamily: t.mono } }, String(totals[key]))
    ));
  }
  if (rawItems.length > items.length) rows.push(h(Text, { key: 'more', style: { color: t.muted, fontSize: t.body - 1 } }, `+ ${rawItems.length - items.length} more items`));
  return h(Section, { style: { display: 'flex', flexDirection: 'column' } }, ...rows);
}

function estimateHeight(spec, t) {
  const hasHeader = Boolean(spec.title || spec.subtitle);
  const base = t.outer * 2 + (hasHeader ? 142 : 42);
  if (spec.kind === 'status' && spec.metrics && !(spec.columns && spec.rows)) return Math.min(Number(spec?.style?.max_height) || 1500, base + Math.ceil((spec.metrics || []).length / 3) * (t.density === 'compact' ? 150 : 180));
  if (['table', 'comparison', 'status'].includes(spec.kind)) return Math.min(Number(spec?.style?.max_height) || 2200, base + 96 + estimateTableRowsHeight(spec, t));
  if (spec.kind === 'chart') return Math.min(Number(spec?.style?.max_height) || 2200, base + estimateChartHeight(spec.chart, t));
  if (spec.kind === 'metric_grid') return Math.min(Number(spec?.style?.max_height) || 1600, base + Math.ceil((spec.metrics || []).length / 3) * (t.density === 'compact' ? 150 : 180));
  if (spec.kind === 'timeline') return Math.min(Number(spec?.style?.max_height) || 2200, base + 50 + Math.max(3, (spec.events || spec.rows || []).length) * 108);
  if (spec.kind === 'receipt') return Math.min(Number(spec?.style?.max_height) || 2200, base + 72 + Math.max(3, (spec.items || spec.rows || []).length) * 58 + 120);
  return 900;
}

function estimateTableRowsHeight(spec, t) {
  const rows = (spec.rows || []).slice(0, 30);
  const columns = Math.max((spec.columns || []).length, 1);
  const charsPerLine = Math.max(16, Math.floor(54 / columns));
  let total = 0;
  for (const row of rows) {
    const maxLines = Math.max(1, ...(row || []).map(cell => estimateTextLines(cell, charsPerLine)));
    total += Math.max(t.row, 24 + (maxLines * t.body * 1.35));
  }
  if ((spec.rows || []).length > rows.length) total += t.row;
  return total;
}

function estimateChartHeight(chartSpec, t) {
  const series = chartSpec?.series || [];
  const valueRows = series.reduce((count, current) => count + Math.min((current?.values || []).length, 24), 0);
  const legendRows = Math.max(series.length, 1);
  return 40 + legendRows * (t.body + 18) + Math.max(4, valueRows) * 42;
}

function estimateTextLines(value, charsPerLine) {
  const text = String(value ?? '');
  if (!text) return 1;
  return Math.min(6, text.split(/\s+/).reduce((lines, word) => lines + Math.ceil(Math.max(word.length, 1) / charsPerLine), 0));
}
