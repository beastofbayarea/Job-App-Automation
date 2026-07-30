/**
 * Declarative KPI tiles.
 *
 * Every tile derives both its number and its subtext from the live metrics
 * payload. Nothing here may hardcode a value: the previous markup shipped
 * "0% Failure Rate" and a 16/2/2 ATS matrix as static HTML, which stayed on
 * screen as apparent fact even when the API said otherwise.
 */

import { escapeHtml, formatNumber, formatPercent } from './format.js';

function sumValues(record) {
  if (!record || typeof record !== 'object') return 0;
  return Object.values(record).reduce((total, value) => {
    const num = Number(value);
    return total + (Number.isFinite(num) ? num : 0);
  }, 0);
}

function breakdown(record, order = ['greenhouse', 'ashby', 'lever']) {
  if (!record || typeof record !== 'object') return '';
  const keys = [...new Set([...order.filter((k) => k in record), ...Object.keys(record)])];
  const parts = keys
    .filter((key) => Number(record[key]) > 0)
    .map((key) => `${key[0].toUpperCase()}${key.slice(1)} ${formatNumber(record[key])}`);
  return parts.join(' · ');
}

export const KPI_DEFS = {
  submissions: {
    accent: 'air',
    icon: '🌀',
    label: 'Confirmed Submissions',
    hint: 'Applications the engine submitted and verified, all time.',
    value: (m) => formatNumber(m.total_submissions),
    sub: (m) => breakdown(m.confirmed_by_ats_all_time ?? m.ats_submissions) || 'No submissions recorded yet',
  },
  jobs: {
    accent: 'water',
    icon: '🌊',
    label: 'Jobs Discovered',
    hint: 'Unique postings returned by the most recent search run.',
    value: (m) => formatNumber(m.total_jobs_found),
    sub: (m) => {
      const counts = m.live_status_counts || {};
      const parts = Object.entries(counts)
        .filter(([, value]) => Number(value) > 0)
        .map(([key, value]) => `${key} ${formatNumber(value)}`);
      return parts.length ? `Liveness: ${parts.join(' · ')}` : 'No liveness checks recorded';
    },
  },
  queue: {
    accent: 'earth',
    icon: '🪨',
    label: 'Generation Queue',
    hint: 'Postings staged for AI resume and cover-letter tailoring.',
    value: (m) => formatNumber(m.generation_queue_count),
    sub: () => 'Tailoring specs awaiting generation',
  },
  archives: {
    accent: 'fire',
    icon: '🔥',
    label: 'Document Archives',
    hint: 'Per-job archive records, including runs that did not complete.',
    value: (m) => formatNumber(m.archived_document_sets),
    sub: (m) => {
      const counts = m.archive_status_counts || {};
      const archived = Number(counts.archived || 0);
      const failed = Number(counts.failed || 0);
      if (!archived && !failed) return 'No archive records yet';
      return `${formatNumber(archived)} archived · ${formatNumber(failed)} failed`;
    },
  },
  boards: {
    accent: 'lotus',
    icon: '🪷',
    label: 'Board Registry',
    hint: 'ATS board endpoints held in the discovery cache.',
    value: (m) => formatNumber(m.cached_boards_count),
    sub: () => 'Cached board endpoints',
  },
  failures: {
    accent: 'error',
    icon: '⚡',
    label: 'Submission Failures',
    hint: 'Failures recorded by the most recent application run.',
    value: (m) => formatNumber(m.failure_count),
    sub: (m) => {
      const attempted = sumValues(m.attempted_by_ats);
      if (!attempted) return 'No attempts in the last run';
      const rate = formatPercent(m.failure_count, attempted);
      return `${rate} of ${formatNumber(attempted)} attempts in last run`;
    },
  },
};

/** Build the tile shells so they exist (in a loading state) before data lands. */
export function renderKpiShells(container, ids) {
  if (!container) return;
  container.innerHTML = ids
    .filter((id) => id in KPI_DEFS)
    .map((id) => {
      const def = KPI_DEFS[id];
      return `
        <article class="kpi-card accent-${def.accent}" data-kpi="${escapeHtml(id)}">
          <h3 class="kpi-label">
            <span aria-hidden="true">${def.icon}</span> ${escapeHtml(def.label)}
          </h3>
          <p class="kpi-value" data-kpi-value><span class="skeleton skeleton-value"></span></p>
          <p class="kpi-sub" data-kpi-sub><span class="skeleton skeleton-text"></span></p>
          <p class="kpi-hint">${escapeHtml(def.hint)}</p>
        </article>
      `;
    })
    .join('');
}

/** Fill the tiles from a metrics payload. */
export function updateKpis(container, metrics) {
  if (!container) return;
  container.querySelectorAll('[data-kpi]').forEach((card) => {
    const def = KPI_DEFS[card.dataset.kpi];
    if (!def) return;
    const valueEl = card.querySelector('[data-kpi-value]');
    const subEl = card.querySelector('[data-kpi-sub]');
    if (valueEl) valueEl.textContent = def.value(metrics);
    if (subEl) subEl.textContent = def.sub(metrics);
  });
}

/** Show that the numbers could not be refreshed, rather than leaving skeletons. */
export function markKpisUnavailable(container) {
  if (!container) return;
  container.querySelectorAll('[data-kpi]').forEach((card) => {
    const valueEl = card.querySelector('[data-kpi-value]');
    const subEl = card.querySelector('[data-kpi-sub]');
    if (valueEl) valueEl.textContent = '—';
    if (subEl) subEl.textContent = 'Metrics unavailable';
  });
}
