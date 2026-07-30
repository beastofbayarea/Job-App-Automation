/**
 * Shared page shell: header, primary navigation, footer, and the refresh
 * lifecycle every page uses.
 *
 * These three regions used to be copy-pasted into all five HTML files, so a
 * nav or footer change meant five edits and any missed file drifted silently.
 * They are defined once here and injected at runtime.
 */

import { formatRelative, escapeHtml } from './format.js';
import { renderKpiShells, updateKpis, markKpisUnavailable } from './kpis.js';
import { api } from './api.js';

const AUTO_REFRESH_MS = 60000;
const FRESHNESS_TICK_MS = 15000;

export const PAGES = [
  { id: 'submissions', href: 'index.html', icon: '📜', label: 'Submissions', title: 'Submissions & failures' },
  { id: 'search', href: 'search.html', icon: '🌊', label: 'Search', title: 'Job search & coverage' },
  { id: 'generation', href: 'generation.html', icon: '🪨', label: 'Generation', title: 'Generation queue & archives' },
  { id: 'logs', href: 'logs.html', icon: '🔥', label: 'Logs', title: 'VPS sync logs' },
  { id: 'inspector', href: 'inspector.html', icon: '📁', label: 'Inspector', title: 'Raw file inspector' },
];

const PROFILE_LINKS = [
  { href: 'https://www.linkedin.com/in/shivam-singh-58231522a/', label: 'LinkedIn' },
  { href: 'https://github.com/shivam040200', label: 'GitHub' },
  { href: 'https://shivam040200.github.io/', label: 'Portfolio' },
  { href: 'mailto:shivamsi@umich.edu', label: 'Email' },
];

const PLATFORM_LINKS = [
  { href: 'sitemap.xml', label: 'XML sitemap' },
  { href: 'robots.txt', label: 'Robots directives' },
  { href: 'site.webmanifest', label: 'Web app manifest' },
];

function headerHtml() {
  return `
    <a class="skip-link" href="#main">Skip to main content</a>
    <header class="app-header">
      <a class="brand" href="index.html">
        <span class="brand-mark">
          <img src="sky_bison_logo.jpg" alt="" width="44" height="44" loading="eager" decoding="async">
        </span>
        <span class="brand-text">
          <span class="brand-title">SkyBison Cloud</span>
          <span class="brand-subtitle">Autonomous job operations engine</span>
        </span>
      </a>
      <div class="header-actions">
        <span class="status-chip" id="serviceStatus" data-state="pending">
          <span class="status-dot" aria-hidden="true"></span>
          <span data-status-text>Connecting…</span>
        </span>
        <span class="freshness" id="freshness" title="Time since the last successful load"></span>
        <label class="auto-toggle">
          <input type="checkbox" id="autoRefreshToggle" checked>
          <span>Auto</span>
        </label>
        <button type="button" class="btn btn-primary" id="refreshBtn">
          <span aria-hidden="true">↻</span>
          <span data-refresh-label>Refresh</span>
        </button>
      </div>
    </header>
    <p class="live-region" role="status" aria-live="polite" id="liveRegion"></p>
  `;
}

function navHtml(activeId) {
  const items = PAGES.map((page) => {
    const isActive = page.id === activeId;
    return `
      <a href="${page.href}" class="tab${isActive ? ' is-active' : ''}"${isActive ? ' aria-current="page"' : ''}>
        <span aria-hidden="true">${page.icon}</span> ${escapeHtml(page.label)}
      </a>
    `;
  }).join('');
  return `<nav class="nav-tabs" aria-label="Dashboard sections">${items}</nav>`;
}

function linkList(links) {
  return links
    .map((link) => {
      const external = link.href.startsWith('http') || link.href.startsWith('mailto');
      const attrs = external ? ' target="_blank" rel="noopener noreferrer"' : '';
      return `<li><a href="${escapeHtml(link.href)}"${attrs}>${escapeHtml(link.label)}</a></li>`;
    })
    .join('');
}

function footerHtml() {
  const year = new Date().getFullYear();
  return `
    <footer class="app-footer">
      <div class="footer-inner">
        <div class="footer-col footer-col-brand">
          <div class="footer-brand">
            <img src="sky_bison_logo.jpg" alt="" width="32" height="32" loading="lazy" decoding="async">
            <span>SkyBison Cloud</span>
          </div>
          <p class="footer-desc">
            Autonomous job application engine. Served read-only over HTTPS from a
            Hostinger KVM VPS running Ubuntu 24.04 LTS.
          </p>
          <p class="footer-copy">© ${year} Cent Capital (Shivam Singh). All rights reserved.</p>
        </div>
        <div class="footer-col">
          <h2 class="footer-heading">Sections</h2>
          <ul class="footer-links">
            ${PAGES.map((p) => `<li><a href="${p.href}">${escapeHtml(p.title)}</a></li>`).join('')}
          </ul>
        </div>
        <div class="footer-col">
          <h2 class="footer-heading">Profile</h2>
          <ul class="footer-links">${linkList(PROFILE_LINKS)}</ul>
        </div>
        <div class="footer-col">
          <h2 class="footer-heading">Platform</h2>
          <ul class="footer-links">${linkList(PLATFORM_LINKS)}</ul>
        </div>
      </div>
    </footer>
  `;
}

/**
 * Reflect connectivity honestly.
 *
 * The badge may only report what this page load actually observed; it must
 * never assert that the backend is healthy on the strength of hardcoded markup.
 */
function setServiceStatus(state, text) {
  const chip = document.getElementById('serviceStatus');
  if (!chip) return;
  chip.dataset.state = state;
  const label = chip.querySelector('[data-status-text]');
  if (label) label.textContent = text;
}

function announce(message) {
  const region = document.getElementById('liveRegion');
  if (region) region.textContent = message;
}

/** Render a dismissible page-level error without destroying already-loaded content. */
export function showBanner(message, tone = 'error') {
  let banner = document.getElementById('pageBanner');
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'pageBanner';
    banner.className = 'banner';
    banner.setAttribute('role', 'alert');
    const main = document.getElementById('main');
    if (main) main.prepend(banner);
  }
  banner.dataset.tone = tone;
  banner.hidden = false;
  banner.textContent = message;
}

export function clearBanner() {
  const banner = document.getElementById('pageBanner');
  if (banner) banner.hidden = true;
}

/**
 * Boot a page.
 *
 * @param {object} config
 * @param {string} config.page      Active nav entry id.
 * @param {string[]} [config.kpis]  KPI tile ids to mount above the content.
 * @param {Array<() => Promise<void>>} [config.loaders] Data loaders re-run on refresh.
 */
export function initShell({ page, kpis = [], loaders = [] }) {
  document.body.insertAdjacentHTML('afterbegin', headerHtml());
  document.body.insertAdjacentHTML('beforeend', footerHtml());

  const navMount = document.getElementById('navMount');
  if (navMount) navMount.outerHTML = navHtml(page);

  const kpiMount = document.getElementById('kpiMount');
  if (kpiMount && kpis.length) {
    kpiMount.className = 'kpi-grid';
    renderKpiShells(kpiMount, kpis);
  }

  let lastLoadedAt = null;
  let refreshing = false;

  async function loadMetrics() {
    try {
      const metrics = await api.metrics();
      setServiceStatus('ok', 'Online');
      if (kpiMount && kpis.length) updateKpis(kpiMount, metrics);
      document.dispatchEvent(new CustomEvent('metrics:loaded', { detail: metrics }));
      return metrics;
    } catch (error) {
      setServiceStatus('down', 'Unreachable');
      if (kpiMount && kpis.length) markKpisUnavailable(kpiMount);
      throw error;
    }
  }

  function updateFreshness() {
    const el = document.getElementById('freshness');
    if (!el) return;
    if (!lastLoadedAt) {
      el.textContent = '';
      return;
    }
    el.textContent = `Updated ${formatRelative(lastLoadedAt)}`;
    el.dateTime = new Date(lastLoadedAt).toISOString();
  }

  async function refresh({ announceResult = true } = {}) {
    if (refreshing) return;
    refreshing = true;
    const btn = document.getElementById('refreshBtn');
    const label = btn ? btn.querySelector('[data-refresh-label]') : null;
    if (btn) btn.disabled = true;
    if (label) label.textContent = 'Refreshing…';

    const results = await Promise.allSettled([loadMetrics(), ...loaders.map((fn) => fn())]);
    const failures = results.filter((r) => r.status === 'rejected');

    if (failures.length) {
      const reason = failures[0].reason;
      showBanner(
        `Could not load all dashboard data: ${reason?.message || 'unknown error'}. ` +
          'Showing the most recent values that did load.',
      );
      if (announceResult) announce('Refresh completed with errors.');
    } else {
      clearBanner();
      if (announceResult) announce('Dashboard data refreshed.');
    }

    lastLoadedAt = Date.now();
    updateFreshness();
    if (btn) btn.disabled = false;
    if (label) label.textContent = 'Refresh';
    refreshing = false;
  }

  document.getElementById('refreshBtn')?.addEventListener('click', () => refresh());

  let timer = null;
  function startAuto() {
    stopAuto();
    timer = setInterval(() => {
      // Skip background polling for a hidden tab; it wastes VPS cycles on a
      // single-core host that is also running the browser workers.
      if (document.visibilityState === 'visible') refresh({ announceResult: false });
    }, AUTO_REFRESH_MS);
  }
  function stopAuto() {
    if (timer) clearInterval(timer);
    timer = null;
  }

  const toggle = document.getElementById('autoRefreshToggle');
  toggle?.addEventListener('change', () => (toggle.checked ? startAuto() : stopAuto()));
  if (toggle?.checked) startAuto();

  setInterval(updateFreshness, FRESHNESS_TICK_MS);
  refresh({ announceResult: false });

  return { refresh };
}
