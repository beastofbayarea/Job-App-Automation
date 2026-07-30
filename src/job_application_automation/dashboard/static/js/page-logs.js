/** VPS sync log console. */

import { initShell } from './chrome.js';
import { api } from './api.js';
import { escapeHtml, formatDateTime, formatNumber } from './format.js';

/**
 * Log lines are emitted by Python's logging module as
 *   `2026-07-29 09:52:41,447 [INFO] ResumeAIUtilities: message`
 * with indented continuation lines belonging to the preceding entry.
 *
 * The previous parser ignored the `[LEVEL]` field entirely and guessed a
 * severity by substring-matching the message, which mislabelled every line
 * mentioning the word "attempt" as an AI call and stamped a literal "HTTP 200"
 * badge on requests whose status it had never read.
 */
const ENTRY_RE = /^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d{3})?)\s+\[([A-Z]+)\]\s+([^:]+):\s*(.*)$/;
const HTTP_STATUS_RE = /"HTTP\/[\d.]+\s+(\d{3})/;
const SCORE_RE = /Score:\s*(\d+)\s*\/\s*100/;

const LEVEL_TONE = {
  DEBUG: 'neutral',
  INFO: 'info',
  WARNING: 'warn',
  ERROR: 'bad',
  CRITICAL: 'bad',
};

const state = {
  entries: [],
  category: 'all',
  query: '',
};

function classify(entry) {
  const level = entry.level;
  const logger = entry.logger.toLowerCase();
  const text = `${entry.message} ${entry.detail}`.toLowerCase();

  if (level === 'ERROR' || level === 'CRITICAL' || level === 'WARNING' || text.includes('failed')) {
    return 'error';
  }
  if (logger.includes('httpx') || logger.includes('urllib') || entry.httpStatus) return 'http';
  if (logger.includes('resumeai') || logger.includes('genai') || entry.score !== null) return 'ai';
  if (text.includes('archive') || text.includes('submission') || text.includes('submitted')) return 'archive';
  return 'other';
}

function parseLog(raw) {
  const lines = String(raw || '').split('\n');
  const entries = [];

  for (const line of lines) {
    if (!line.trim()) continue;
    const match = ENTRY_RE.exec(line);

    if (!match) {
      // Indented continuations and bare messages extend the previous entry
      // rather than becoming phantom rows with a fabricated timestamp.
      if (entries.length && /^\s/.test(line)) {
        const previous = entries[entries.length - 1];
        previous.detail = previous.detail ? `${previous.detail}\n${line.trim()}` : line.trim();
        continue;
      }
      entries.push({
        timestamp: '',
        level: 'INFO',
        logger: '',
        message: line.trim(),
        detail: '',
        httpStatus: null,
        score: null,
      });
      continue;
    }

    const [, timestamp, level, logger, message] = match;
    const httpMatch = HTTP_STATUS_RE.exec(message);
    const scoreMatch = SCORE_RE.exec(message);
    entries.push({
      timestamp,
      level,
      logger: logger.trim(),
      message,
      detail: '',
      httpStatus: httpMatch ? Number(httpMatch[1]) : null,
      score: scoreMatch ? Number(scoreMatch[1]) : null,
    });
  }

  for (const entry of entries) {
    if (entry.score === null) {
      const detailScore = SCORE_RE.exec(entry.detail);
      if (detailScore) entry.score = Number(detailScore[1]);
    }
    entry.category = classify(entry);
  }

  return entries;
}

function counts(entries) {
  return entries.reduce(
    (acc, entry) => {
      acc.total += 1;
      acc[entry.category] = (acc[entry.category] || 0) + 1;
      return acc;
    },
    { total: 0 },
  );
}

function badgeFor(entry) {
  if (entry.httpStatus) {
    const tone = entry.httpStatus < 400 ? 'ok' : 'bad';
    return `<span class="badge badge-${tone}">HTTP ${entry.httpStatus}</span>`;
  }
  if (entry.score !== null) {
    const tone = entry.score >= 80 ? 'ok' : entry.score >= 60 ? 'warn' : 'bad';
    return `<span class="badge badge-${tone}">Score ${entry.score}</span>`;
  }
  const tone = LEVEL_TONE[entry.level] || 'neutral';
  return `<span class="badge badge-${tone}">${escapeHtml(entry.level)}</span>`;
}

function render() {
  const body = document.getElementById('logBody');
  if (!body) return;

  let visible = state.entries;
  if (state.category !== 'all') visible = visible.filter((e) => e.category === state.category);
  if (state.query) {
    const q = state.query.toLowerCase();
    visible = visible.filter((e) => `${e.timestamp} ${e.logger} ${e.message} ${e.detail}`.toLowerCase().includes(q));
  }

  if (!visible.length) {
    body.innerHTML = '<p class="log-empty">No log entries match the current filters.</p>';
    return;
  }

  body.innerHTML = visible
    .map((entry) => `
      <div class="log-row" data-level="${escapeHtml(entry.level)}">
        <span class="log-time">${entry.timestamp ? escapeHtml(formatDateTime(entry.timestamp.replace(',', '.'))) : '<span class="muted">—</span>'}</span>
        <span class="log-level">${badgeFor(entry)}</span>
        <span class="log-message">
          ${entry.logger ? `<span class="log-logger">${escapeHtml(entry.logger)}</span>` : ''}
          ${escapeHtml(entry.message)}
          ${entry.detail ? `<span class="log-detail">${escapeHtml(entry.detail)}</span>` : ''}
        </span>
      </div>
    `)
    .join('');

  if (document.getElementById('autoScrollToggle')?.checked) {
    body.scrollTop = body.scrollHeight;
  }
}

function updateCounters() {
  const c = counts(state.entries);
  const set = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.textContent = formatNumber(value || 0);
  };
  set('logCountTotal', c.total);
  set('logCountError', c.error);
  set('logCountAi', c.ai);
  set('logCountHttp', c.http);
  set('logCountArchive', c.archive);

  document.querySelectorAll('[data-log-filter]').forEach((btn) => {
    const key = btn.dataset.logFilter;
    const badge = btn.querySelector('[data-count]');
    if (badge) badge.textContent = formatNumber(key === 'all' ? c.total : c[key] || 0);
  });
}

async function loadLog() {
  const body = document.getElementById('logBody');
  try {
    const data = await api.vpsLog();
    state.entries = parseLog(data.log);
    updateCounters();
    render();
  } catch (error) {
    if (body) {
      body.innerHTML = `<p class="log-empty log-empty-error">Could not load the VPS log: ${escapeHtml(error.message)}</p>`;
    }
    throw error;
  }
}

document.querySelectorAll('[data-log-filter]').forEach((btn) => {
  btn.addEventListener('click', () => {
    state.category = btn.dataset.logFilter;
    document.querySelectorAll('[data-log-filter]').forEach((other) => {
      const active = other === btn;
      other.classList.toggle('is-active', active);
      other.setAttribute('aria-pressed', String(active));
    });
    render();
  });
});

document.getElementById('logSearch')?.addEventListener('input', (event) => {
  state.query = event.target.value.trim();
  render();
});

initShell({
  page: 'logs',
  kpis: [],
  loaders: [loadLog],
});
