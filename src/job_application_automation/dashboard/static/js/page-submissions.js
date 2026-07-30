/** Submissions & failures page. */

import { initShell } from './chrome.js';
import { api } from './api.js';
import { DataTable, setTableLoading, setTableMessage } from './table.js';
import { renderInfraPanel, renderFreshnessPanel } from './infra.js';
import {
  escapeHtml,
  formatDateTime,
  formatNumber,
  formatPercent,
  atsModifier,
  statusModifier,
  safeUrl,
  toIso,
} from './format.js';

const table = document.getElementById('submissionsTable');

const submissionsTable = new DataTable({
  table,
  initialSort: 'applied_at',
  initialDesc: true,
  pageSize: 50,
  emptyMessage: 'No submissions recorded yet.',
  searchText: (row) =>
    [row.company, row.role, row.ats, row.status, row.email_used, row.resume_filename]
      .filter(Boolean)
      .join(' '),
  columns: [
    {
      key: 'applied_at',
      label: 'Applied',
      className: 'col-time',
      sortValue: (row) => Date.parse(row.applied_at) || 0,
      render: (row) =>
        `<time datetime="${escapeHtml(toIso(row.applied_at))}">${escapeHtml(formatDateTime(row.applied_at))}</time>`,
    },
    {
      key: 'company',
      label: 'Company',
      className: 'col-strong',
      render: (row) => escapeHtml(row.company || '—'),
    },
    {
      key: 'role',
      label: 'Role',
      render: (row) => {
        const href = safeUrl(row.job_url);
        const label = escapeHtml(row.role || '—');
        return href
          ? `<a class="link" href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${label} <span aria-hidden="true">↗</span><span class="sr-only">(opens in a new tab)</span></a>`
          : label;
      },
    },
    {
      key: 'ats',
      label: 'ATS',
      render: (row) =>
        `<span class="badge ${atsModifier(row.ats)}">${escapeHtml(row.ats || 'unknown')}</span>`,
    },
    {
      key: 'email_used',
      label: 'Alias used',
      className: 'col-mono',
      render: (row) => escapeHtml(row.email_used || '—'),
    },
    {
      key: 'resume_filename',
      label: 'Tailored resume',
      sortable: false,
      render: (row) => {
        if (!row.resume_filename) return '<span class="muted">Not archived</span>';
        const name = escapeHtml(row.resume_filename);
        return `<a class="file-link" href="/api/download/${encodeURIComponent(row.resume_filename)}"
                   download="${name}" title="${name}">
                  <span aria-hidden="true">📄</span> Download PDF
                </a>`;
      },
    },
    {
      key: 'status',
      label: 'Status',
      render: (row) =>
        `<span class="badge ${statusModifier(row.status)}">${escapeHtml(row.status || 'unknown')}</span>`,
    },
  ],
});

/**
 * Render the ATS conversion matrix strictly from reported counts.
 *
 * The previous implementation substituted invented defaults when a platform was
 * missing from the payload (`confirmed[plat] || (plat === 'greenhouse' ? 16 : 2)`),
 * so an empty API response still rendered a confident 100% success table.
 */
function renderAtsMatrix(metrics) {
  const tbody = document.getElementById('atsMatrixBody');
  if (!tbody) return;

  const attempted = metrics.attempted_by_ats || {};
  const confirmed = metrics.confirmed_by_ats || {};
  const allTime = metrics.confirmed_by_ats_all_time || {};
  const platforms = [...new Set([...Object.keys(attempted), ...Object.keys(confirmed), ...Object.keys(allTime)])].sort();

  if (!platforms.length) {
    setTableMessage(tbody, 4, 'No ATS activity has been reported yet.');
    return;
  }

  tbody.innerHTML = platforms
    .map((platform) => {
      const att = Number(attempted[platform] ?? 0);
      const conf = Number(confirmed[platform] ?? 0);
      const total = Number(allTime[platform] ?? 0);
      const rate = att > 0 ? formatPercent(conf, att) : '—';
      const rateBadge = att > 0
        ? `<span class="badge ${conf === att ? 'badge-ok' : 'badge-warn'}">${rate}</span>`
        : '<span class="muted" title="No attempts recorded in the last run">—</span>';
      return `
        <tr>
          <td><span class="badge ${atsModifier(platform)}">${escapeHtml(platform)}</span></td>
          <td class="col-num">${formatNumber(att)}</td>
          <td class="col-num">${formatNumber(conf)}</td>
          <td class="col-num">${rateBadge}</td>
          <td class="col-num">${formatNumber(total)}</td>
        </tr>
      `;
    })
    .join('');
}

/** Drive the failure panel from the failures report instead of static prose. */
async function loadFailures() {
  const panel = document.getElementById('failurePanel');
  if (!panel) return;
  try {
    const data = await api.failures();
    const count = Number(data.failure_count ?? 0);
    const failures = Array.isArray(data.failures) ? data.failures : [];
    const runStarted = data.run_started_at ? formatDateTime(data.run_started_at) : 'unknown';
    const updated = data.updated_at ? formatDateTime(data.updated_at) : 'unknown';

    if (!count && !failures.length) {
      panel.innerHTML = `
        <p class="panel-lead"><span class="badge badge-ok">0 failures</span></p>
        <p class="muted">
          Every application attempted in the run started ${escapeHtml(runStarted)} passed ATS
          submission verification. Last updated ${escapeHtml(updated)}.
        </p>
      `;
      return;
    }

    panel.innerHTML = `
      <p class="panel-lead"><span class="badge badge-bad">${formatNumber(count)} failures</span></p>
      <ul class="failure-list">
        ${failures
          .slice(0, 25)
          .map((item) => {
            const company = escapeHtml(item.company || item.board_token || 'Unknown target');
            const role = escapeHtml(item.role || item.title || '');
            const reason = escapeHtml(item.reason || item.error || item.status || 'No reason recorded');
            return `<li><strong>${company}</strong>${role ? ` — ${role}` : ''}<span class="failure-reason">${reason}</span></li>`;
          })
          .join('')}
      </ul>
      ${failures.length > 25 ? `<p class="muted">…and ${formatNumber(failures.length - 25)} more. See the raw file inspector for the full report.</p>` : ''}
    `;
  } catch (error) {
    panel.innerHTML = `<p class="muted">Failure diagnostics unavailable: ${escapeHtml(error.message)}</p>`;
  }
}

async function loadSubmissions() {
  setTableLoading(submissionsTable.tbody, submissionsTable.columns.length);
  try {
    const data = await api.submissions();
    const rows = data && typeof data === 'object' ? Object.values(data) : [];
    submissionsTable.setRows(rows.filter((row) => row && typeof row === 'object'));
  } catch (error) {
    setTableMessage(
      submissionsTable.tbody,
      submissionsTable.columns.length,
      `Could not load submissions: ${error.message}`,
      'error',
    );
    throw error;
  }
}

document.addEventListener('metrics:loaded', (event) => {
  renderAtsMatrix(event.detail);
  renderInfraPanel(event.detail);
  renderFreshnessPanel(event.detail);
});

document.getElementById('submissionSearch')?.addEventListener('input', (event) => {
  submissionsTable.setQuery(event.target.value);
});
document.querySelector('[data-table-more="submissionsTable"]')?.addEventListener('click', () => {
  submissionsTable.showMore();
});

initShell({
  page: 'submissions',
  kpis: ['submissions', 'jobs', 'queue', 'archives', 'boards', 'failures'],
  loaders: [loadSubmissions, loadFailures],
});
