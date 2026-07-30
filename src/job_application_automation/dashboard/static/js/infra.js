/**
 * Host infrastructure and data-freshness panels.
 *
 * Every value here is read from `/api/metrics`. The previous markup hardcoded a
 * host IP (`2.24.28.180`) that the API deliberately never returns — the address
 * is filtered out of the public config — so it could never be corrected and had
 * drifted away from the real host.
 */

import { escapeHtml, formatDateTime, formatNumber, formatRelative, toIso } from './format.js';

function definition(label, value, { mono = true, title = '' } = {}) {
  if (value === null || value === undefined || value === '') return '';
  return `
    <div class="def">
      <dt>${escapeHtml(label)}</dt>
      <dd class="${mono ? 'mono' : ''}"${title ? ` title="${escapeHtml(title)}"` : ''}>${escapeHtml(value)}</dd>
    </div>
  `;
}

export function renderInfraPanel(metrics) {
  const grid = document.getElementById('infraGrid');
  if (!grid) return;

  const vps = metrics.vps_info || {};
  const hostinger = metrics.hostinger_info || {};

  const specs = [];
  if (vps.cpu_cores) specs.push(`${vps.cpu_cores} vCPU`);
  if (vps.memory_gb) specs.push(`${vps.memory_gb} GB RAM`);
  if (vps.disk_gb) specs.push(`${vps.disk_gb} GB SSD`);
  if (vps.bandwidth_tb) specs.push(`${vps.bandwidth_tb} TB transfer`);

  const owner = hostinger.owner_name
    ? `${hostinger.company ? `${hostinger.company} — ` : ''}${hostinger.owner_name}`
    : 'Cent Capital';

  const html = [
    definition('Hostname', vps.hostname),
    definition('Operating system', vps.os),
    definition('Plan', vps.plan),
    definition('Resources', specs.join(' · ')),
    definition('Datacenter', vps.datacenter),
    definition('Backup schedule', vps.backup_schedule),
    definition('Plan renews', vps.plan_expiration_date),
    definition('Auto renewal', vps.auto_renewal === undefined ? '' : vps.auto_renewal ? 'Enabled' : 'Disabled'),
    definition('Account owner', owner, { mono: false }),
  ]
    .filter(Boolean)
    .join('');

  grid.innerHTML = html || '<p class="muted">Host metadata is not published by this deployment.</p>';
}

function freshnessRow(label, value, hint) {
  if (!value) {
    return `
      <div class="def">
        <dt>${escapeHtml(label)}</dt>
        <dd class="muted">Not recorded</dd>
      </div>
    `;
  }
  return `
    <div class="def">
      <dt>${escapeHtml(label)}</dt>
      <dd>
        <time datetime="${escapeHtml(toIso(value))}" title="${escapeHtml(formatDateTime(value))}">
          ${escapeHtml(formatRelative(value))}
        </time>
        ${hint ? `<span class="def-hint">${escapeHtml(hint)}</span>` : ''}
      </dd>
    </div>
  `;
}

/**
 * Show how old each underlying report is.
 *
 * The dashboard reads files that are synced from separate long-running
 * services, so "the page loaded" and "the data is current" are different
 * claims. Surfacing both stops the site from implying freshness it cannot know.
 */
export function renderFreshnessPanel(metrics) {
  const grid = document.getElementById('freshnessGrid');
  if (!grid) return;

  const coverage = metrics.coverage || {};
  grid.innerHTML = [
    freshnessRow('Latest submission', metrics.latest_submission_at, 'submission_log.json'),
    freshnessRow('Application run report', metrics.last_failure_update, 'vps_application_failures.json'),
    freshnessRow('Search coverage run', coverage.generated_at, 'job_search_coverage.json'),
    freshnessRow('Metrics computed', metrics.generated_at, 'server response time'),
  ].join('');
}

/** Compact "what the engine is looking for" summary from the search criteria. */
export function renderCriteriaPanel(metrics) {
  const target = document.getElementById('criteriaPanel');
  if (!target) return;

  const criteria = (metrics.coverage || {}).criteria || {};
  const chips = (label, values) => {
    if (!Array.isArray(values) || !values.length) return '';
    return `
      <div class="chip-row">
        <span class="chip-label">${escapeHtml(label)}</span>
        <span class="chips">${values.map((v) => `<span class="chip">${escapeHtml(v)}</span>`).join('')}</span>
      </div>
    `;
  };

  const html = [
    chips('Roles', criteria.role_terms),
    chips('Platforms', criteria.platforms),
    chips('Locations', criteria.location_terms),
    criteria.discovery_mode || criteria.match_mode
      ? `<div class="chip-row">
           <span class="chip-label">Modes</span>
           <span class="chips">
             ${criteria.discovery_mode ? `<span class="chip">discovery: ${escapeHtml(criteria.discovery_mode)}</span>` : ''}
             ${criteria.match_mode ? `<span class="chip">match: ${escapeHtml(criteria.match_mode)}</span>` : ''}
           </span>
         </div>`
      : '',
  ]
    .filter(Boolean)
    .join('');

  target.innerHTML = html || '<p class="muted">No search criteria recorded in the latest run.</p>';
}

/** Render a labelled stat with an optional proportion bar. */
export function statTile(label, value, { of = null, hint = '' } = {}) {
  const pct = of && Number(of) > 0 ? Math.min(100, Math.round((Number(value) / Number(of)) * 100)) : null;
  return `
    <div class="stat">
      <span class="stat-label">${escapeHtml(label)}</span>
      <span class="stat-value">${formatNumber(value)}</span>
      ${pct === null ? '' : `
        <span class="meter" role="img" aria-label="${pct}% of ${formatNumber(of)}">
          <span class="meter-fill" style="width:${pct}%"></span>
        </span>
        <span class="stat-hint">${pct}% of ${formatNumber(of)}</span>
      `}
      ${hint ? `<span class="stat-hint">${escapeHtml(hint)}</span>` : ''}
    </div>
  `;
}
