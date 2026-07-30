/** Generation queue & document archives page. */

import { initShell } from './chrome.js';
import { api } from './api.js';
import { DataTable, setTableLoading, setTableMessage } from './table.js';
import { statTile } from './infra.js';
import {
  escapeHtml,
  formatDateTime,
  formatNumber,
  atsModifier,
  statusModifier,
  safeUrl,
  truncate,
  toIso,
} from './format.js';

/**
 * The generation queue holds the full job description for every staged posting.
 * Rendering it with `JSON.stringify(..., 2)` into a single element produced a
 * multi-megabyte blob of text — unsearchable, unreadable, and slow enough to
 * lock up the tab. Each posting now gets a row, with the description behind a
 * disclosure so it is available without being forced on the reader.
 */
const queueTable = new DataTable({
  table: document.getElementById('queueTable'),
  initialSort: 'posted_at',
  initialDesc: true,
  pageSize: 25,
  emptyMessage: 'The generation queue is empty.',
  searchText: (row) =>
    [row.company, row.title, row.platform, row.location, row.department, row.team, row.match_reason]
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
        const link = href
          ? `<a class="link" href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${label} <span aria-hidden="true">↗</span><span class="sr-only">(opens in a new tab)</span></a>`
          : label;
        const meta = [row.department, row.team, row.employment_type].filter(Boolean).join(' · ');
        return `${link}${meta ? `<span class="cell-sub">${escapeHtml(meta)}</span>` : ''}`;
      },
    },
    {
      key: 'location',
      label: 'Location',
      render: (row) => {
        const workplace = row.workplace_type ? `<span class="cell-sub">${escapeHtml(row.workplace_type)}</span>` : '';
        return `${escapeHtml(row.location || '—')}${workplace}`;
      },
    },
    {
      key: 'salary',
      label: 'Salary',
      render: (row) => (row.salary ? escapeHtml(row.salary) : '<span class="muted">Not stated</span>'),
    },
    {
      key: 'posted_at',
      label: 'Posted',
      className: 'col-time',
      sortValue: (row) => Date.parse(row.posted_at) || 0,
      render: (row) =>
        row.posted_at
          ? `<time datetime="${escapeHtml(toIso(row.posted_at))}">${escapeHtml(formatDateTime(row.posted_at))}</time>`
          : '<span class="muted">Unknown</span>',
    },
    {
      key: 'description',
      label: 'Spec',
      sortable: false,
      render: (row) => {
        const reason = row.match_reason
          ? `<p class="spec-reason"><strong>Matched on:</strong> ${escapeHtml(row.match_reason)}</p>`
          : '';
        if (!row.description) return reason || '<span class="muted">—</span>';
        return `
          <details class="disclosure disclosure-inline">
            <summary>Description (${formatNumber(String(row.description).length)} chars)</summary>
            ${reason}
            <p class="spec-body">${escapeHtml(row.description)}</p>
          </details>
        `;
      },
    },
  ],
});

const archiveTable = new DataTable({
  table: document.getElementById('archiveTable'),
  initialSort: 'updated_at',
  initialDesc: true,
  pageSize: 50,
  emptyMessage: 'No document archive records yet.',
  searchText: (row) => [row.company, row.title, row.status, row.url].filter(Boolean).join(' '),
  columns: [
    { key: 'company', label: 'Company', className: 'col-strong', render: (row) => escapeHtml(row.company || '—') },
    {
      key: 'title',
      label: 'Role',
      render: (row) => {
        const href = safeUrl(row.url);
        const label = escapeHtml(truncate(row.title || '—', 90));
        return href
          ? `<a class="link" href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${label} <span aria-hidden="true">↗</span><span class="sr-only">(opens in a new tab)</span></a>`
          : label;
      },
    },
    {
      key: 'status',
      label: 'Status',
      render: (row) => `<span class="badge ${statusModifier(row.status)}">${escapeHtml(row.status || 'unknown')}</span>`,
    },
    {
      key: 'exit_code',
      label: 'Exit code',
      className: 'col-num',
      sortValue: (row) => Number(row.exit_code ?? -1),
      render: (row) => (row.exit_code === undefined ? '<span class="muted">—</span>' : escapeHtml(row.exit_code)),
    },
    {
      key: 'updated_at',
      label: 'Updated',
      className: 'col-time',
      sortValue: (row) => Date.parse(row.updated_at) || 0,
      render: (row) =>
        `<time datetime="${escapeHtml(toIso(row.updated_at))}">${escapeHtml(formatDateTime(row.updated_at))}</time>`,
    },
  ],
});

async function loadQueue() {
  setTableLoading(queueTable.tbody, queueTable.columns.length);
  try {
    const data = await api.generation();
    queueTable.setRows(Array.isArray(data) ? data : []);
  } catch (error) {
    setTableMessage(queueTable.tbody, queueTable.columns.length, `Could not load the queue: ${error.message}`, 'error');
    throw error;
  }
}

/**
 * `vps_document_archive_state.json` nests its records under a `jobs` key and
 * stores the job URL as the record key, so the record set has to be unwrapped
 * before it can be listed.
 */
function flattenArchives(payload) {
  if (!payload || typeof payload !== 'object') return [];
  const jobs = payload.jobs && typeof payload.jobs === 'object' ? payload.jobs : payload;
  return Object.entries(jobs)
    .filter(([, record]) => record && typeof record === 'object')
    .map(([url, record]) => ({ url, ...record }));
}

function renderArchiveSummary(rows) {
  const target = document.getElementById('archiveSummary');
  if (!target) return;
  const counts = rows.reduce((acc, row) => {
    const status = String(row.status || 'unknown').toLowerCase();
    acc[status] = (acc[status] || 0) + 1;
    return acc;
  }, {});
  target.innerHTML = `
    <div class="stat-row">
      ${statTile('Archive records', rows.length)}
      ${Object.entries(counts)
        .sort((a, b) => b[1] - a[1])
        .map(([status, count]) => statTile(status, count, { of: rows.length }))
        .join('')}
    </div>
  `;
}

async function loadArchives() {
  setTableLoading(archiveTable.tbody, archiveTable.columns.length);
  try {
    const rows = flattenArchives(await api.archives());
    archiveTable.setRows(rows);
    renderArchiveSummary(rows);
  } catch (error) {
    setTableMessage(
      archiveTable.tbody,
      archiveTable.columns.length,
      `Could not load archives: ${error.message}`,
      'error',
    );
    throw error;
  }
}

document.getElementById('queueSearch')?.addEventListener('input', (e) => queueTable.setQuery(e.target.value));
document.getElementById('archiveSearch')?.addEventListener('input', (e) => archiveTable.setQuery(e.target.value));
document.querySelector('[data-table-more="queueTable"]')?.addEventListener('click', () => queueTable.showMore());
document.querySelector('[data-table-more="archiveTable"]')?.addEventListener('click', () => archiveTable.showMore());

initShell({
  page: 'generation',
  kpis: ['queue', 'archives', 'submissions', 'jobs'],
  loaders: [loadQueue, loadArchives],
});
