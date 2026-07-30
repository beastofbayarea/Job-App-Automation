/** Presentation helpers shared by every dashboard page. */

/**
 * Escape a value for interpolation into an HTML string.
 *
 * Accepts any type on purpose: the previous implementation short-circuited on
 * falsy input, which silently erased the number 0 and the boolean false from
 * every table cell that received one.
 */
export function escapeHtml(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/** Format an integer with locale grouping, or an em dash when absent. */
export function formatNumber(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return '—';
  return num.toLocaleString('en-US');
}

/** Render a ratio as a whole percentage, guarding against a zero denominator. */
export function formatPercent(numerator, denominator) {
  const n = Number(numerator);
  const d = Number(denominator);
  if (!Number.isFinite(n) || !Number.isFinite(d) || d <= 0) return '—';
  return `${Math.round((n / d) * 100)}%`;
}

function toDate(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** Absolute timestamp including the year, which the previous format omitted. */
export function formatDateTime(value) {
  const date = toDate(value);
  if (!date) return '—';
  return date.toLocaleString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/** Short relative age, e.g. "4m ago". Used for freshness indicators. */
export function formatRelative(value, now = Date.now()) {
  const date = toDate(value);
  if (!date) return '—';
  const seconds = Math.round((now - date.getTime()) / 1000);
  if (seconds < 0) return 'just now';
  if (seconds < 45) return 'just now';
  const units = [
    ['d', 86400],
    ['h', 3600],
    ['m', 60],
  ];
  for (const [suffix, size] of units) {
    if (seconds >= size) return `${Math.floor(seconds / size)}${suffix} ago`;
  }
  return `${seconds}s ago`;
}

/** Full ISO timestamp for `title` attributes and `<time datetime>`. */
export function toIso(value) {
  const date = toDate(value);
  return date ? date.toISOString() : '';
}

/** Collapse a long string for table display while keeping the full text in a title. */
export function truncate(value, limit = 160) {
  const text = String(value ?? '');
  if (text.length <= limit) return text;
  return `${text.slice(0, limit - 1).trimEnd()}…`;
}

/** Map a domain status string onto a badge modifier class. */
export function statusModifier(status) {
  const text = String(status ?? '').toLowerCase();
  if (!text) return 'badge-neutral';
  if (text.includes('confirm') || text === 'archived' || text === 'live' || text === 'ok') {
    return 'badge-ok';
  }
  if (text.includes('fail') || text.includes('error') || text === 'unavailable') {
    return 'badge-bad';
  }
  if (text.includes('pending') || text.includes('queue') || text.includes('retry')) {
    return 'badge-warn';
  }
  return 'badge-neutral';
}

/** Badge modifier for a known ATS platform, falling back to a neutral chip. */
export function atsModifier(platform) {
  const name = String(platform ?? '').toLowerCase();
  return ['greenhouse', 'ashby', 'lever'].includes(name) ? `badge-${name}` : 'badge-neutral';
}

/**
 * Only allow links the dashboard is willing to render as clickable.
 * Job URLs come from third-party feeds, so `javascript:` and `data:` payloads
 * must never reach an `href`.
 */
export function safeUrl(value) {
  const text = String(value ?? '').trim();
  if (!text) return '';
  try {
    const url = new URL(text, window.location.origin);
    return ['http:', 'https:'].includes(url.protocol) ? url.href : '';
  } catch {
    return '';
  }
}
