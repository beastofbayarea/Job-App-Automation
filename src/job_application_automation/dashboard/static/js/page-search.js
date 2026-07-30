/** Job search & discovery coverage page. */

import { initShell } from './chrome.js';
import { api } from './api.js';
import { DataTable, setTableLoading, setTableMessage } from './table.js';
import { renderCriteriaPanel, statTile } from './infra.js';
import { escapeHtml, formatDateTime, formatNumber, atsModifier, statusModifier, safeUrl, truncate } from './format.js';

const table = document.getElementById('jobsTable');

const jobsTable = new DataTable({
  table,
  initialSort: 'posted_at',
  initialDesc: true,
  pageSize: 50,
  emptyMessage: 'No jobs have been discovered in the current run.',
  searchText: (row) =>
    [row.company, row.title, row.platform, row.location, row.workplace_type, row.department, row.employment_type]
      .filter(Boolean)
      .join(' '),
  columns: [
    {
      key: 'platform',
      label: 'ATS',
      render: (row) => `<span class="badge ${atsModifier(row.platform)}">${escapeHtml(row.platform || '—')}</span>`,
    },
    { key: 'company', label: 'Company', className: 'col-strong', render: (row) => escapeHtml(row.company || '—') },
    {
      key: 'title',
      label: 'Role',
      render: (row) => {
        const href = safeUrl(row.job_url || row.apply_url);
        const label = escapeHtml(row.title || '—');
        return href
          ? `<a class="link" href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${label} <span aria-hidden="true">↗</span><span class="sr-only">(opens in a new tab)</span></a>`
          : label;
      },
    },
    { key: 'location', label: 'Location', render: (row) => escapeHtml(row.location || '—') },
    {
      key: 'workplace_type',
      label: 'Workplace',
      render: (row) => escapeHtml(row.workplace_type || '—'),
    },
    {
      key: 'posted_at',
      label: 'Posted',
      className: 'col-time',
      sortValue: (row) => Date.parse(row.posted_at) || 0,
      render: (row) => {
        const days = Number(row.days_old);
        const age = Number.isFinite(days) ? `${days}d old` : '';
        return row.posted_at
          ? `${escapeHtml(formatDateTime(row.posted_at))}${age ? `<span class="cell-sub">${escapeHtml(age)}</span>` : ''}`
          : '<span class="muted">Unknown</span>';
      },
    },
    {
      key: 'live_status',
      label: 'Liveness',
      render: (row) => {
        const status = row.live_status || 'unknown';
        const reason = row.live_check_reason || row.live_check_source || '';
        return `<span class="badge ${statusModifier(status)}" title="${escapeHtml(truncate(reason, 200))}">${escapeHtml(status)}</span>`;
      },
    },
    {
      key: 'match_reason',
      label: 'Why matched',
      sortable: false,
      render: (row) =>
        row.match_reason
          ? `<span class="cell-wrap" title="${escapeHtml(row.match_reason)}">${escapeHtml(truncate(row.match_reason, 90))}</span>`
          : '<span class="muted">—</span>',
    },
  ],
});

/**
 * Render the discovery funnel.
 *
 * The coverage report was previously dumped as `JSON.stringify` into a div,
 * which hid the fact that 103 of 396 planned queries failed and that 62 board
 * feeds could not be fetched.
 */
function renderCoverage(metrics) {
  const target = document.getElementById('coveragePanel');
  if (!target) return;

  const coverage = metrics.coverage || {};
  const discovery = coverage.discovery || {};
  const feed = coverage.feed_fetch || {};
  const fallback = coverage.fallback || {};
  const results = coverage.results || {};
  const cache = coverage.cache || {};

  if (!coverage.generated_at) {
    target.innerHTML = '<p class="muted">No search coverage diagnostics recorded for this deployment.</p>';
    return;
  }

  const group = (heading, note, tiles) => `
    <section class="funnel-stage">
      <h3 class="funnel-heading">${escapeHtml(heading)}</h3>
      <p class="funnel-note">${escapeHtml(note)}</p>
      <div class="stat-row">${tiles.join('')}</div>
    </section>
  `;

  target.innerHTML = [
    group('1 · Query discovery', 'Search-engine queries used to find ATS board endpoints.', [
      statTile('Queries planned', discovery.queries_planned),
      statTile('Attempted', discovery.queries_attempted, { of: discovery.queries_planned }),
      statTile('Query failures', discovery.query_failures, { of: discovery.queries_attempted }),
      statTile('Results seen', discovery.results_seen),
      statTile('Boards discovered', discovery.boards_discovered),
      statTile('Candidates found', discovery.candidates_discovered),
    ]),
    group('2 · Feed fetch', 'Direct ATS board API reads for every cached board endpoint.', [
      statTile('Boards checked', feed.boards_checked),
      statTile('Succeeded', feed.boards_succeeded, { of: feed.boards_checked }),
      statTile('Failed boards', feed.failed_board_count),
      statTile('Jobs from feeds', feed.jobs_from_feeds),
    ]),
    group('3 · Fallback scrape', 'Candidate URLs resolved when a board feed was unavailable.', [
      statTile('Attempted', fallback.attempted),
      statTile('Matched', fallback.matched, { of: fallback.attempted }),
      statTile('Failed', fallback.failed, { of: fallback.attempted }),
    ]),
    group('4 · Results', 'What survived deduplication and liveness checks.', [
      statTile('Collected', results.collected_before_deduplication),
      statTile('After dedup', results.deduplicated, { of: results.collected_before_deduplication }),
      statTile('Returned', results.returned),
    ]),
    group('Cache', 'Board and candidate registries persisted between runs.', [
      statTile('Boards loaded', cache.boards_loaded),
      statTile('Boards saved', cache.boards_saved),
      statTile('Candidate URLs loaded', cache.candidate_urls_loaded),
      statTile('Candidate URLs saved', cache.candidate_urls_saved),
    ]),
  ].join('');

  const stamp = document.getElementById('coverageStamp');
  if (stamp) stamp.textContent = `Run generated ${formatDateTime(coverage.generated_at)}`;
}

async function loadJobs() {
  setTableLoading(jobsTable.tbody, jobsTable.columns.length);
  try {
    const data = await api.jobs();
    jobsTable.setRows(Array.isArray(data) ? data : []);
  } catch (error) {
    setTableMessage(jobsTable.tbody, jobsTable.columns.length, `Could not load jobs: ${error.message}`, 'error');
    throw error;
  }
}

async function loadBoardCache() {
  const target = document.getElementById('boardCachePanel');
  if (!target) return;
  try {
    const data = await api.boardCache();
    const tokens = data && typeof data === 'object' ? Object.keys(data) : [];
    if (!tokens.length) {
      target.innerHTML = '<p class="muted">The board cache registry is empty.</p>';
      return;
    }
    const byPlatform = tokens.reduce((acc, token) => {
      const platform = token.includes(':') ? token.split(':')[0] : 'other';
      acc[platform] = (acc[platform] || 0) + 1;
      return acc;
    }, {});
    target.innerHTML = `
      <div class="stat-row">
        ${statTile('Cached endpoints', tokens.length)}
        ${Object.entries(byPlatform)
          .sort((a, b) => b[1] - a[1])
          .map(([platform, count]) => statTile(platform, count, { of: tokens.length }))
          .join('')}
      </div>
      <details class="disclosure">
        <summary>Show all ${formatNumber(tokens.length)} cached board tokens</summary>
        <ul class="token-list">${tokens.sort().map((t) => `<li>${escapeHtml(t)}</li>`).join('')}</ul>
      </details>
    `;
  } catch (error) {
    target.innerHTML = `<p class="muted">Board cache unavailable: ${escapeHtml(error.message)}</p>`;
    throw error;
  }
}

document.addEventListener('metrics:loaded', (event) => {
  renderCoverage(event.detail);
  renderCriteriaPanel(event.detail);
});

document.getElementById('jobSearch')?.addEventListener('input', (event) => {
  jobsTable.setQuery(event.target.value);
});
document.querySelector('[data-table-more="jobsTable"]')?.addEventListener('click', () => jobsTable.showMore());

initShell({
  page: 'search',
  kpis: ['jobs', 'boards', 'queue', 'submissions'],
  loaders: [loadJobs, loadBoardCache],
});
