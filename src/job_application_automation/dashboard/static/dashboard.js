let state = {
  metrics: {},
  submissions: {},
  jobs: [],
  backlog: [],
  coverage: {},
  generation: [],
  archives: {},
  operations: {},
  adminOverview: {},
  activeFunnelStage: 'all'
};

// Single source of truth for the site nav, shared by the desktop nav, the
// mobile drawer, and the mobile bottom bar across every page.
const NAV_PAGES = [
  { slug: 'index', href: 'index.html', icon: '📜', desktopLabel: 'Submissions', drawerLabel: 'Submissions & Failures', bottomLabel: 'Matrix' },
  { slug: 'search', href: 'search.html', icon: '🌊', desktopLabel: 'Job Search', drawerLabel: 'Job Search', bottomLabel: 'Search' },
  { slug: 'generation', href: 'generation.html', icon: '🪨', desktopLabel: 'Application Queue', drawerLabel: 'Application Queue', bottomLabel: 'Queue' },
  { slug: 'inspector', href: 'inspector.html', icon: '📁', desktopLabel: 'Inspector', drawerLabel: 'Raw File Inspector', bottomLabel: 'Files' },
  { slug: 'system-status', href: 'system-status.html', icon: '🛰️', desktopLabel: 'System Status', drawerLabel: 'System Status', bottomLabel: 'Status' },
  { slug: 'admin', href: 'admin.html', icon: '🔐', desktopLabel: 'Admin Vault', drawerLabel: 'Admin Vault', bottomLabel: 'Admin' }
];

// Each page keeps its own <nav>/<aside> chrome (hamburger button, drawer
// header, backdrops) exactly where it was; only the repeated <a> link lists
// inside them are generated here, driven by a `data-active` attribute set on
// each mount element to the current page's slug.
function renderNav() {
  const desktopMount = document.getElementById('desktopNav');
  if (desktopMount) {
    const activeSlug = desktopMount.dataset.active;
    desktopMount.innerHTML = NAV_PAGES.map((page) => {
      const activeClass = page.slug === activeSlug ? ' active' : '';
      return `<a href="${page.href}" class="nav-link${activeClass}">${page.icon} ${page.desktopLabel}</a>`;
    }).join('\n');
  }

  const drawerMount = document.getElementById('drawerLinks');
  if (drawerMount) {
    const activeSlug = drawerMount.dataset.active;
    drawerMount.innerHTML = NAV_PAGES.map((page) => {
      const activeClass = page.slug === activeSlug ? ' active' : '';
      return `<a href="${page.href}" class="nav-link${activeClass}" onclick="closeMobileDrawer()">${page.icon} ${page.drawerLabel}</a>`;
    }).join('\n');
  }

  const bottomMount = document.getElementById('bottomNav');
  if (bottomMount) {
    const activeSlug = bottomMount.dataset.active;
    bottomMount.innerHTML = NAV_PAGES.map((page) => {
      const activeClass = page.slug === activeSlug ? ' active' : '';
      return `<a href="${page.href}" class="bottom-nav-item${activeClass}">
        <span class="bottom-nav-icon">${page.icon}</span>
        <span class="bottom-nav-label">${page.bottomLabel}</span>
      </a>`;
    }).join('\n');
  }
}

document.addEventListener('DOMContentLoaded', () => {
  renderNav();
  setupNavigation();
  fetchMetrics();

  if (document.getElementById('submissionsTableBody')) {
    fetchSubmissions();
  }
  if (document.getElementById('jobsTableBody')) {
    fetchJobs();
  }
  if (document.getElementById('backlogTableBody')) {
    fetchBacklog();
  }
  if (document.getElementById('coverageMetricsSummary')) {
    fetchCoverageAndCache();
  }
  if (document.getElementById('generationQueueView') || document.getElementById('archiveStateView')) {
    fetchSection2();
  }
  if (document.getElementById('inspectorSections')) {
    renderInspectorSections();
  }
  if (document.getElementById('workerStatusBody')) {
    fetchOperations();
  }
  if (document.getElementById('adminFilesBody')) {
    fetchAdminOverview();
  }
});

/* --- Mobile Navigation Drawer & Bottom Bar Setup ----------------------- */

function setupNavigation() {
  const hamburgerBtn = document.getElementById('hamburgerBtn');
  const drawer = document.getElementById('mobileDrawer');
  const backdrop = document.getElementById('drawerBackdrop');
  const closeBtn = document.getElementById('drawerCloseBtn');

  if (hamburgerBtn && drawer && backdrop) {
    hamburgerBtn.addEventListener('click', () => {
      const isOpen = drawer.classList.toggle('is-open');
      backdrop.classList.toggle('is-visible', isOpen);
      hamburgerBtn.classList.toggle('is-open', isOpen);
      hamburgerBtn.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
    });

    if (closeBtn) {
      closeBtn.addEventListener('click', closeMobileDrawer);
    }
    backdrop.addEventListener('click', closeMobileDrawer);
  }

  // Job Detail Backdrop listener
  const detailBackdrop = document.getElementById('jobDetailBackdrop');
  if (detailBackdrop) {
    detailBackdrop.addEventListener('click', closeJobDetail);
  }
}

function closeMobileDrawer() {
  const hamburgerBtn = document.getElementById('hamburgerBtn');
  const drawer = document.getElementById('mobileDrawer');
  const backdrop = document.getElementById('drawerBackdrop');

  if (drawer) drawer.classList.remove('is-open');
  if (backdrop) backdrop.classList.remove('is-visible');
  if (hamburgerBtn) {
    hamburgerBtn.classList.remove('is-open');
    hamburgerBtn.setAttribute('aria-expanded', 'false');
  }
}

/* --- Job Detail Drawer Overlay ------------------------------------------ */

function openJobDetail(submissionKey) {
  const drawer = document.getElementById('jobDetailDrawer');
  const backdrop = document.getElementById('jobDetailBackdrop');
  const content = document.getElementById('jobDetailContent');
  if (!drawer || !content) return;

  const item = state.submissions[submissionKey];
  if (!item) return;

  content.innerHTML = `
    <div class="detail-field">
      <span class="detail-label">Company</span>
      <span class="detail-value" style="font-size: 1.1rem; font-weight: 700; color: var(--air);">${escapeHtml(item.company)}</span>
    </div>
    <div class="detail-field">
      <span class="detail-label">Role / Job Title</span>
      <span class="detail-value">${escapeHtml(item.role)}</span>
    </div>
    <div class="detail-field">
      <span class="detail-label">ATS Platform</span>
      <span class="detail-value"><span class="badge badge-${(item.ats || '').toLowerCase()}">${escapeHtml(item.ats)}</span></span>
    </div>
    <div class="detail-field">
      <span class="detail-label">Application Status</span>
      <span class="detail-value"><span class="badge badge-confirmed">${escapeHtml(item.status)}</span></span>
    </div>
    <div class="detail-field">
      <span class="detail-label">Applied Timestamp</span>
      <span class="detail-value">${formatDate(item.applied_at)}</span>
    </div>
    <div class="detail-field">
      <span class="detail-label">Complete Raw Record and Documents</span>
      <div style="margin-top: 0.3rem;"><a href="admin.html" class="resume-download-link">🔐 Open Admin Vault</a></div>
    </div>
  `;

  drawer.classList.add('is-open');
  if (backdrop) backdrop.classList.add('is-visible');
}

function closeJobDetail() {
  const drawer = document.getElementById('jobDetailDrawer');
  const backdrop = document.getElementById('jobDetailBackdrop');
  if (drawer) drawer.classList.remove('is-open');
  if (backdrop) backdrop.classList.remove('is-visible');
}

/* --- Pipeline Funnel Stage Filter --------------------------------------- */

function filterByFunnelStage(stage) {
  state.activeFunnelStage = stage;
  if (stage === 'water') {
    window.location.href = 'search.html';
  } else if (stage === 'earth') {
    window.location.href = 'generation.html';
  } else if (stage === 'fire') {
    window.location.href = 'generation.html#archives';
  } else {
    renderSubmissionsTable();
  }
}

/* --- VPS Status Badge & Metrics Fetching -------------------------------- */

function setVpsStatusBadge(text, modifier) {
  const badge = document.getElementById('vpsStatusBadge');
  if (!badge) return;
  badge.textContent = text;
  badge.className = `badge ${modifier}`;
}

async function fetchMetrics() {
  try {
    const res = await fetch('/api/metrics');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    setVpsStatusBadge('ACTIVE & ONLINE', 'badge-confirmed');
    state.metrics = data;
    
    if (document.getElementById('kpiSubmissions')) document.getElementById('kpiSubmissions').textContent = data.total_submissions ?? 0;
    if (document.getElementById('kpiJobsFound')) document.getElementById('kpiJobsFound').textContent = data.total_jobs_found ?? 0;
    if (document.getElementById('kpiGenQueue')) document.getElementById('kpiGenQueue').textContent = data.generation_queue_count ?? 0;
    if (document.getElementById('kpiArchives')) document.getElementById('kpiArchives').textContent = data.archived_document_sets ?? 0;
    if (document.getElementById('kpiCachedBoards')) document.getElementById('kpiCachedBoards').textContent = data.cached_boards_count ?? 0;
    if (document.getElementById('kpiFailures')) document.getElementById('kpiFailures').textContent = data.failure_count ?? 0;
    if (document.getElementById('snapshotJobCount')) document.getElementById('snapshotJobCount').textContent = `${data.total_jobs_found ?? 0} current`;
    if (document.getElementById('backlogJobCount')) document.getElementById('backlogJobCount').textContent = `${data.backlog_job_count ?? 0} tracked`;

    const failureSummary = document.getElementById('failureLogSummary');
    if (failureSummary) {
      const workers = Array.isArray(data.workers) ? data.workers : [];
      const workerRows = workers.map((worker) => {
        const failed = worker.status_counts?.failed || 0;
        const manual = worker.status_counts?.manual_review || 0;
        const confirmed = worker.status_counts?.confirmed || 0;
        return `<div><strong>${escapeHtml(worker.provider)}</strong>: ${confirmed} confirmed, ${failed} failed, ${manual} manual review</div>`;
      }).join('');
      failureSummary.innerHTML = `
        <div>Continuous non-confirmed outcomes: <strong>${data.continuous_nonconfirmed_count ?? 0}</strong></div>
        <div>Current bounded-run failures: <strong>${data.current_run_failure_count ?? 0}</strong></div>
        ${workerRows || '<div>No continuous worker state is available yet.</div>'}
      `;
    }

    // Render ATS Submissions Breakdown Subtext
    if (data.ats_submissions && document.getElementById('kpiAtsBreakdown')) {
      const gh = data.ats_submissions.greenhouse || 0;
      const ash = data.ats_submissions.ashby || 0;
      const lev = data.ats_submissions.lever || 0;
      document.getElementById('kpiAtsBreakdown').textContent = `Greenhouse: ${gh} | Ashby: ${ash} | Lever: ${lev}`;
    }

    // Render Liveness Subtext
    if (data.live_status_counts && document.getElementById('kpiLivenessSub')) {
      const live = data.live_status_counts.live || 0;
      const unavail = data.live_status_counts.unavailable || 0;
      document.getElementById('kpiLivenessSub').textContent = `Live: ${live} | Unavailable: ${unavail}`;
    }

    // Render Infrastructure Specs if available
    if (data.vps_info) {
      const v = data.vps_info;
      if (v.host && document.getElementById('vpsHost')) document.getElementById('vpsHost').textContent = v.host;
      if (v.hostname && document.getElementById('vpsHostname')) document.getElementById('vpsHostname').textContent = v.hostname;
      if (v.os && document.getElementById('vpsOs')) document.getElementById('vpsOs').textContent = v.os;
      if (v.plan && document.getElementById('vpsPlan')) document.getElementById('vpsPlan').textContent = `${v.plan} (${v.cpu_cores || 1} vCPU, ${v.memory_gb || 4}GB RAM, ${v.disk_gb || 50}GB SSD)`;
      if (v.datacenter && document.getElementById('vpsDatacenter')) document.getElementById('vpsDatacenter').textContent = v.datacenter;
      if (v.plan_expiration_date && document.getElementById('vpsExpiry')) document.getElementById('vpsExpiry').textContent = v.plan_expiration_date;
    }
    if (data.hostinger_info && data.hostinger_info.owner_name && document.getElementById('vpsOwner')) {
      document.getElementById('vpsOwner').textContent = `${data.hostinger_info.company || ''} (${data.hostinger_info.owner_name})`;
    }

    // Render live continuous-worker facts, pulled from the VPS's own
    // snapshot file rather than a live SSH check (the dashboard server never
    // shells into the VPS itself).
    if (data.vps_infra) {
      const infra = data.vps_infra;
      const servicesEl = document.getElementById('vpsActiveEngines');
      if (servicesEl) {
        const services = Array.isArray(infra.active_services) ? infra.active_services : [];
        servicesEl.textContent = services.length
          ? `${services.length} running: ${services.join(', ')}`
          : 'No snapshot yet';
      }
      const uptimeEl = document.getElementById('vpsUptime');
      if (uptimeEl) {
        uptimeEl.textContent = infra.uptime || '--';
      }
    }

  } catch (err) {
    setVpsStatusBadge('UNREACHABLE', 'badge-failed');
    console.error('Failed to load metrics', err);
  }
}

async function fetchSubmissions() {
  try {
    const res = await fetch('/api/section3/submissions');
    state.submissions = await res.json();
    renderSubmissionsTable();
  } catch (err) {
    console.error('Failed to load submissions', err);
  }
}

async function fetchJobs() {
  try {
    const res = await fetch('/api/section1/jobs');
    state.jobs = await res.json();
    renderJobsTable();
  } catch (err) {
    console.error('Failed to load search jobs', err);
  }
}

async function fetchBacklog() {
  try {
    const res = await fetch('/api/section1/backlog');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.backlog = await res.json();
    renderBacklogTable();
    const badge = document.getElementById('backlogJobCount');
    if (badge) badge.textContent = `${state.backlog.length} tracked`;
  } catch (err) {
    console.error('Failed to load persistent job database', err);
    const tbody = document.getElementById('backlogTableBody');
    if (tbody) tbody.innerHTML = '<tr><td colspan="10" class="table-empty">Failed to load the persistent job database.</td></tr>';
  }
}

async function fetchOperations() {
  try {
    const res = await fetch('/api/operations');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.operations = await res.json();
    renderOperations();
  } catch (err) {
    console.error('Failed to load VPS operations', err);
  }
}

async function fetchAdminOverview() {
  try {
    const res = await fetch('/api/admin/overview');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.adminOverview = await res.json();
    renderAdminOverview();
  } catch (err) {
    console.error('Failed to load admin inventory', err);
    const tbody = document.getElementById('adminFilesBody');
    if (tbody) tbody.innerHTML = '<tr><td colspan="6" class="table-empty">Admin inventory could not be loaded.</td></tr>';
  }
}

async function fetchCoverageAndCache() {
  try {
    const [covRes, cacheRes] = await Promise.all([
      fetch('/api/section1/coverage'),
      fetch('/api/section1/cache')
    ]);
    const cov = await covRes.json();
    const cacheData = await cacheRes.json();

    // Coverage Summary Rendering
    const covDiv = document.getElementById('coverageMetricsSummary');
    if (covDiv) {
      if (cov && (cov.version || cov.generated_at)) {
        covDiv.innerHTML = `
          <div style="display: flex; flex-direction: column; gap: 0.5rem;">
            <div><span style="color: var(--text-muted);">Version:</span> <strong>${escapeHtml(cov.version || '1.0.0')}</strong></div>
            <div><span style="color: var(--text-muted);">Generated At:</span> <span style="color: var(--air);">${formatDate(cov.generated_at)}</span></div>
            <div><span style="color: var(--text-muted);">Returned Jobs:</span> <span class="badge badge-info">${cov.results?.returned ?? 'N/A'} listings</span></div>
            <div><span style="color: var(--text-muted);">Live Status:</span> 
              <span class="badge badge-ok">Live: ${cov.results?.live_status_counts?.live ?? 0}</span>
              <span class="badge badge-warn">Unavail: ${cov.results?.live_status_counts?.unavailable ?? 0}</span>
            </div>
          </div>
        `;
      } else {
        covDiv.textContent = 'No search coverage diagnostics recorded in current local run.';
      }
    }

    // Board Cache Summary Rendering
    const cacheDiv = document.getElementById('boardCacheSummary');
    if (cacheDiv) {
      if (cacheData && Object.keys(cacheData).length > 0) {
        const keys = Object.keys(cacheData);
        const sampleTokens = keys.slice(0, 5).join(', ');
        cacheDiv.innerHTML = `
          <div style="display: flex; flex-direction: column; gap: 0.5rem;">
            <div><span style="color: var(--text-muted);">Cached Endpoints:</span> <strong style="color: var(--water-blue);">${keys.length} ATS Boards</strong></div>
            <div><span style="color: var(--text-muted);">Sample Board Tokens:</span> <code style="color: var(--lotus); font-size: 0.8rem;">${escapeHtml(sampleTokens)}...</code></div>
            <div><span class="badge badge-confirmed">⚡ Fast API Cache Active</span></div>
          </div>
        `;
      } else {
        cacheDiv.textContent = 'ATS Board cache registry active on VPS.';
      }
    }
  } catch (err) {
    console.error('Failed to load coverage/cache data', err);
  }
}

async function fetchSection2() {
  try {
    const [genRes, archRes] = await Promise.all([
      fetch('/api/section2/generation'),
      fetch('/api/section2/archives')
    ]);
    state.generation = await genRes.json();
    state.archives = await archRes.json();
    renderSection2View();
  } catch (err) {
    console.error('Failed to load section 2 data', err);
  }
}

function renderSubmissionsTable() {
  const tbody = document.getElementById('submissionsTableBody');
  if (!tbody) return;
  const searchInput = document.getElementById('sec3Search');
  const query = (searchInput ? searchInput.value : '').toLowerCase();
  
  if (!state.submissions || Object.keys(state.submissions).length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--text-muted);">No submissions recorded yet.</td></tr>';
    return;
  }
  
  const entries = Object.entries(state.submissions);
  const filtered = entries.filter(([key, item]) => {
    return (
      (item.company || '').toLowerCase().includes(query) ||
      (item.role || '').toLowerCase().includes(query) ||
      (item.ats || '').toLowerCase().includes(query) ||
      (item.status || '').toLowerCase().includes(query) ||
      key.toLowerCase().includes(query)
    );
  });

  if (filtered.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--text-muted);">No matching submissions found.</td></tr>';
    return;
  }

  tbody.innerHTML = filtered.map(([key, item]) => {
    const encodedKey = encodeURIComponent(key).replace(/'/g, '%27');
    return `
      <tr onclick="openJobDetail(decodeURIComponent('${encodedKey}'))">
        <td style="font-family: var(--font-mono); font-size: 0.8rem; color: var(--text-muted);">${formatDate(item.applied_at)}</td>
        <td style="font-weight: 600;">${escapeHtml(item.company)}</td>
        <td>${escapeHtml(item.role)}</td>
        <td><span class="badge badge-${(item.ats || '').toLowerCase()}">${escapeHtml(item.ats)}</span></td>
        <td><span class="badge badge-confirmed">${escapeHtml(item.status)}</span></td>
        <td><code>${escapeHtml(key)}</code></td>
        <td><a href="admin.html" class="raw-content-link" onclick="event.stopPropagation();">Admin Vault ↗</a></td>
      </tr>
    `;
  }).join('');
}

function renderJobsTable() {
  const tbody = document.getElementById('jobsTableBody');
  if (!tbody) return;
  const searchInput = document.getElementById('sec1Search');
  const query = (searchInput ? searchInput.value : '').toLowerCase();

  if (!state.jobs || state.jobs.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--text-muted);">No jobs loaded from ai_jobs.csv.</td></tr>';
    return;
  }

  const filtered = state.jobs.filter(item => {
    return (
      (item.company || '').toLowerCase().includes(query) ||
      (item.title || '').toLowerCase().includes(query) ||
      (item.platform || '').toLowerCase().includes(query) ||
      (item.location || '').toLowerCase().includes(query)
    );
  });

  if (filtered.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--text-muted);">No matching jobs found.</td></tr>';
    return;
  }

  tbody.innerHTML = filtered.slice(0, 100).map(item => `
    <tr>
      <td><span class="badge badge-${(item.platform || '').toLowerCase()}">${escapeHtml(item.platform)}</span></td>
      <td style="font-weight: 600;">${escapeHtml(item.company)}</td>
      <td>${escapeHtml(item.title)}</td>
      <td>${escapeHtml(item.location || 'N/A')}</td>
      <td>${escapeHtml(item.workplace_type || 'N/A')}</td>
      <td><span class="badge badge-confirmed">${escapeHtml(item.live_status || 'LIVE')}</span></td>
      <td><a href="${escapeHtml(safeHttpUrl(item.job_url || item.apply_url))}" target="_blank" rel="noopener" style="color: var(--air-cyan); text-decoration: none;">View Listing ↗</a></td>
    </tr>
  `).join('');
}

function renderBacklogTable() {
  const tbody = document.getElementById('backlogTableBody');
  if (!tbody) return;
  const searchInput = document.getElementById('backlogSearch');
  const query = (searchInput ? searchInput.value : '').toLowerCase();
  const rows = Array.isArray(state.backlog) ? state.backlog : [];
  const filtered = rows.filter((item) => JSON.stringify(item).toLowerCase().includes(query));
  if (!filtered.length) {
    tbody.innerHTML = '<tr><td colspan="10" class="table-empty">No persistent job records match this filter.</td></tr>';
    return;
  }
  tbody.innerHTML = filtered.map((item) => {
    const listingUrl = safeHttpUrl(item.job_url);
    const applyUrl = safeHttpUrl(item.apply_url || item.job_url);
    const providerId = item.platform_job_id || item.board_token || item.unique_id || 'N/A';
    return `
      <tr>
        <td><span class="badge badge-${escapeHtml((item.platform || '').toLowerCase())}">${escapeHtml(item.platform || 'unknown')}</span></td>
        <td style="font-weight: 600;">${escapeHtml(item.company || 'N/A')}</td>
        <td>${escapeHtml(item.title || 'N/A')}</td>
        <td>${escapeHtml(item.location || 'N/A')}</td>
        <td><span class="badge ${item.live_status === 'live' ? 'badge-confirmed' : 'badge-warn'}">${escapeHtml(item.live_status || 'unknown')}</span></td>
        <td>${escapeHtml(formatDate(item.first_seen_at))}</td>
        <td>${escapeHtml(formatDate(item.last_seen_at || item.live_checked_at))}</td>
        <td><code>${escapeHtml(providerId)}</code></td>
        <td><a class="raw-content-link" href="${escapeHtml(listingUrl)}" target="_blank" rel="noopener">Listing ↗</a></td>
        <td><a class="raw-content-link" href="${escapeHtml(applyUrl)}" target="_blank" rel="noopener">Apply ↗</a></td>
      </tr>
    `;
  }).join('');
}

function detailTiles(items) {
  return items.map(([label, value]) => `
    <div class="detail-tile">
      <span class="detail-tile-label">${escapeHtml(label)}</span>
      <span class="detail-tile-value">${escapeHtml(value === null || value === undefined || value === '' ? 'N/A' : value)}</span>
    </div>
  `).join('');
}

function renderOperations() {
  const data = state.operations || {};
  const workers = Array.isArray(data.workers) ? data.workers : [];
  const workerBody = document.getElementById('workerStatusBody');
  if (workerBody) {
    workerBody.innerHTML = workers.length ? workers.map((worker) => `
      <tr>
        <td><span class="badge badge-${escapeHtml(worker.provider)}">${escapeHtml(worker.provider)}</span></td>
        <td>${worker.record_count || 0}</td>
        <td>${worker.status_counts?.confirmed || 0}</td>
        <td>${worker.status_counts?.failed || 0}</td>
        <td>${worker.status_counts?.manual_review || 0}</td>
        <td>${worker.result_file_count || 0}</td>
        <td>${worker.document_file_count || 0}</td>
        <td>${escapeHtml(formatDate(worker.latest?.updated_at))}</td>
        <td>${escapeHtml(worker.latest?.stage || worker.latest?.result_status || 'N/A')}</td>
      </tr>
    `).join('') : '<tr><td colspan="9" class="table-empty">No continuous worker state files were found.</td></tr>';
  }

  const host = data.host || {};
  const hostGrid = document.getElementById('hostResourceGrid');
  if (hostGrid) {
    const memory = host.memory || {};
    const disk = host.disk || {};
    hostGrid.innerHTML = detailTiles([
      ['Hostname', host.hostname],
      ['CPU Cores', host.cpu_count],
      ['Load (1/5/15m)', (host.load_average || []).join(' / ')],
      ['Uptime', formatDuration(host.uptime_seconds)],
      ['Memory Total', formatBytes(memory.total_bytes)],
      ['Memory Available', formatBytes(memory.available_bytes)],
      ['Swap Total', formatBytes(memory.swap_total_bytes)],
      ['Disk Used', `${formatBytes(disk.used_bytes)} / ${formatBytes(disk.total_bytes)}`],
      ['Disk Free', formatBytes(disk.free_bytes)]
    ]);
  }

  const inventory = data.files || {};
  const outputGrid = document.getElementById('outputInventoryGrid');
  if (outputGrid) {
    outputGrid.innerHTML = detailTiles([
      ['Files', inventory.file_count || 0],
      ['Total Size', formatBytes(inventory.size_bytes)],
      ['Output Files', inventory.by_scope?.output?.file_count || 0],
      ['JSON', inventory.by_extension?.['.json'] || 0],
      ['PDF', inventory.by_extension?.['.pdf'] || 0],
      ['Screenshots', (inventory.by_extension?.['.png'] || 0) + (inventory.by_extension?.['.jpg'] || 0)],
      ['Other Extensions', Object.keys(inventory.by_extension || {}).length]
    ]);
  }

  const services = Array.isArray(data.infrastructure?.services) ? data.infrastructure.services : [];
  const serviceBody = document.getElementById('serviceStatusBody');
  if (serviceBody) {
    serviceBody.innerHTML = services.length ? services.map((service) => `
      <tr>
        <td><strong>${escapeHtml(service.name)}</strong><br><small>${escapeHtml(service.description)}</small></td>
        <td><span class="badge ${service.active_state === 'active' ? 'badge-confirmed' : 'badge-failed'}">${escapeHtml(service.active_state)} / ${escapeHtml(service.sub_state)}</span></td>
        <td>${escapeHtml(service.unit_file_state)}</td>
        <td>${service.main_pid || 0}</td>
        <td>${service.restart_count || 0}</td>
        <td>${formatBytes(service.memory_bytes)}</td>
        <td>${service.task_count || 0}</td>
      </tr>
    `).join('') : '<tr><td colspan="7" class="table-empty">The next search cycle will publish the detailed service snapshot.</td></tr>';
  }
  renderProcessTable();
}

function renderProcessRows(processes) {
  return processes.map((process) => `
    <tr>
      <td>${process.pid}</td>
      <td><strong>${escapeHtml(process.name)}</strong></td>
      <td>${escapeHtml(process.state)}</td>
      <td>${process.parent_pid}</td>
      <td>${process.threads}</td>
      <td>${formatBytes((process.memory_kb || 0) * 1024)}</td>
      <td>${escapeHtml(process.uid)}</td>
    </tr>
  `).join('');
}

function renderProcessTable() {
  const body = document.getElementById('processStatusBody');
  if (!body) return;
  const all = Array.isArray(state.operations?.processes?.processes) ? state.operations.processes.processes : [];
  const query = (document.getElementById('processSearch')?.value || '').toLowerCase();
  const filtered = all.filter((process) => JSON.stringify(process).toLowerCase().includes(query));
  body.innerHTML = filtered.length ? renderProcessRows(filtered) : '<tr><td colspan="7" class="table-empty">No matching processes.</td></tr>';
  const badge = document.getElementById('processCountBadge');
  if (badge) badge.textContent = `${filtered.length} / ${all.length} processes`;
}

function renderAdminOverview() {
  const data = state.adminOverview || {};
  const files = data.files || {};
  const scopes = files.by_scope || {};
  setText('adminOutputFiles', scopes.output?.file_count || 0);
  setText('adminPrivateFiles', scopes.private_archive?.file_count || 0);
  setText('adminRepositoryFiles', scopes.repository?.file_count || 0);
  setText('adminTotalBytes', formatBytes(files.size_bytes));
  setText('adminProcessCount', data.processes?.process_count || 0);
  setText('adminFileCountBadge', `${files.file_count || 0} files`);
  renderAdminFiles();

  const processBody = document.getElementById('adminProcessesBody');
  const processes = Array.isArray(data.processes?.processes) ? data.processes.processes : [];
  if (processBody) {
    processBody.innerHTML = processes.length ? renderProcessRows(processes) : '<tr><td colspan="7" class="table-empty">No process data available.</td></tr>';
  }

  const logsGrid = document.getElementById('adminLogsGrid');
  if (logsGrid) {
    logsGrid.innerHTML = Object.entries(data.logs || {}).map(([name, log]) => `
      <article class="admin-log-card">
        <h3>${escapeHtml(name)} · ${escapeHtml(log.path)}</h3>
        <pre>${escapeHtml(log.content)}</pre>
      </article>
    `).join('') || '<div class="table-empty">No readable VPS log files were found.</div>';
  }
}

function renderAdminFiles() {
  const body = document.getElementById('adminFilesBody');
  if (!body) return;
  const files = Array.isArray(state.adminOverview?.files?.files) ? state.adminOverview.files.files : [];
  const query = (document.getElementById('adminFileSearch')?.value || '').toLowerCase();
  const filtered = files.filter((file) => JSON.stringify(file).toLowerCase().includes(query));
  body.innerHTML = filtered.length ? filtered.map((file) => {
    const url = `/api/admin/file?scope=${encodeURIComponent(file.scope)}&path=${encodeURIComponent(file.path)}`;
    return `
      <tr>
        <td><span class="badge ${file.scope === 'private_archive' ? 'badge-warn' : 'badge-info'}">${escapeHtml(file.scope)}</span></td>
        <td><code>${escapeHtml(file.path)}</code></td>
        <td>${escapeHtml(file.content_type)}</td>
        <td>${formatBytes(file.size_bytes)}</td>
        <td>${escapeHtml(formatDate(file.modified_at))}</td>
        <td><a class="raw-content-link" href="${escapeHtml(url)}" target="_blank" rel="noopener">Open raw file ↗</a></td>
      </tr>
    `;
  }).join('') : '<tr><td colspan="6" class="table-empty">No matching files.</td></tr>';
  setText('adminFileCountBadge', `${filtered.length} / ${files.length} files`);
}

function renderSection2View() {
  const genView = document.getElementById('generationQueueView');
  if (genView) {
    if (Array.isArray(state.generation) && state.generation.length > 0) {
      let html = '';
      state.generation.forEach((job, idx) => {
        html += `
          <div class="queue-card">
            <div class="queue-title">📋 Generation Job #${idx + 1}</div>
            <div style="font-size: 0.82rem; color: var(--text-muted); font-family: var(--font-mono);">
              <div>Company: <strong style="color: var(--text);">${escapeHtml(job.company || job.target_company || 'N/A')}</strong></div>
              <div>Role: <strong>${escapeHtml(job.role || job.job_title || 'N/A')}</strong></div>
              <div>Status: <span class="badge badge-info">${escapeHtml(job.status || 'PENDING')}</span></div>
            </div>
          </div>
        `;
      });
      genView.innerHTML = html;
    } else {
      genView.textContent = JSON.stringify(state.generation, null, 2);
    }
  }
  
  const archDiv = document.getElementById('archiveStateView');
  if (archDiv) {
    if (state.archives && Object.keys(state.archives).length > 0) {
      let html = '';
      for (const [id, item] of Object.entries(state.archives)) {
        html += `
          <div class="archive-card">
            <div class="archive-title">📦 Archive ID: ${escapeHtml(id)}</div>
            <div style="font-size: 0.82rem; color: var(--text-muted); font-family: var(--font-mono);">
              <div>Company: <strong style="color: var(--text);">${escapeHtml(item.identity?.company || 'N/A')}</strong> | Role: <strong>${escapeHtml(item.identity?.job_title || 'N/A')}</strong></div>
              <div>Updated: <span style="color: var(--air);">${escapeHtml(formatDate(item.updated_at))}</span></div>
              <div style="margin-top: 0.3rem;"><a href="admin.html" class="raw-content-link">Open complete archived record in Admin Vault ↗</a></div>
            </div>
          </div>
        `;
      }
      archDiv.innerHTML = html;
    } else {
      archDiv.textContent = JSON.stringify(state.archives, null, 2);
    }
  }
}

// Static catalog of report files shown on the Inspector page, each rendered
// as its own section (rather than a single dropdown-driven viewer).
const RAW_FILES = [
  { key: 'ai_jobs', filename: 'ai_jobs.csv', label: 'ai_jobs.csv (Section 1)' },
  { key: 'job_search_coverage', filename: 'job_search_coverage.json', label: 'job_search_coverage.json (Section 1)' },
  { key: 'ats_boards_cache', filename: 'ats_boards_cache.json', label: 'ats_boards_cache.json (Section 1)' },
  { key: 'job_backlog', filename: 'job_backlog.json', label: 'job_backlog.json (Persistent Job Database)' },
  { key: 'vps_infra_status', filename: 'vps_infra_status.json', label: 'vps_infra_status.json (VPS Infrastructure)' }
];

function renderInspectorSections() {
  const mount = document.getElementById('inspectorSections');
  if (!mount) return;

  mount.innerHTML = RAW_FILES.map((f) => `
    <div class="card" style="margin-bottom: 1.25rem;">
      <h3 style="margin-bottom: 0.75rem; color: var(--air-cyan); font-family: var(--font-display); font-size: 1.05rem;">📄 ${escapeHtml(f.label)}</h3>
      <div id="inspectorMetaBar_${f.key}" class="inspector-meta-bar">
        <span>Loading metadata...</span>
      </div>
      <pre class="json-viewer" id="rawFileViewer_${f.key}">Loading file contents...</pre>
    </div>
  `).join('');

  RAW_FILES.forEach((f) => loadRawFile(f));
}

async function loadRawFile(fileEntry) {
  const viewer = document.getElementById(`rawFileViewer_${fileEntry.key}`);
  const metaBar = document.getElementById(`inspectorMetaBar_${fileEntry.key}`);
  if (!viewer) return;

  try {
    const res = await fetch(`/api/files/${fileEntry.filename}`);
    const data = await res.json();
    const contentStr = data.content ? data.content : JSON.stringify(data, null, 2);
    viewer.textContent = contentStr;

    if (metaBar) {
      const bytes = new Blob([contentStr]).size;
      const lineCount = contentStr.split('\n').length;
      metaBar.innerHTML = `
        <span>📄 File: <strong>${escapeHtml(fileEntry.filename)}</strong></span>
        <span>Size: <strong>${(bytes / 1024).toFixed(1)} KB</strong></span>
        <span>Lines: <strong>${lineCount}</strong></span>
        <button class="secondary-btn" style="min-height: 32px; padding: 0.25rem 0.6rem; font-size: 0.75rem;" onclick="copyViewerText('${fileEntry.key}')">📋 Copy File Text</button>
      `;
    }
  } catch (err) {
    viewer.textContent = `Error loading file: ${err.message}`;
  }
}

function copyViewerText(key) {
  const viewer = document.getElementById(`rawFileViewer_${key}`);
  if (!viewer) return;
  navigator.clipboard.writeText(viewer.textContent).then(() => {
    alert('File contents copied to clipboard!');
  }).catch(err => {
    console.error('Failed to copy text', err);
  });
}

async function refreshDashboardData() {
  const btn = document.getElementById('refreshBtn');
  const originalText = btn ? btn.innerHTML : '';
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span>⏳ Refreshing...</span>';
  }

  const loaders = [fetchMetrics, fetchSubmissions, fetchJobs, fetchBacklog, fetchCoverageAndCache, fetchSection2, fetchOperations, fetchAdminOverview];
  try {
    await Promise.allSettled(
      loaders.map((load) => (typeof load === 'function' ? load() : undefined)),
    );
    if (btn) btn.innerHTML = '<span>✅ Up to date</span>';
  } finally {
    if (btn) {
      setTimeout(() => {
        btn.innerHTML = originalText;
        btn.disabled = false;
      }, 2000);
    }
  }
}

function formatDate(isoString) {
  if (!isoString) return 'N/A';
  try {
    const date = new Date(isoString);
    return date.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  } catch (e) {
    return isoString;
  }
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / (1024 ** index)).toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function formatDuration(value) {
  let seconds = Number(value || 0);
  if (!Number.isFinite(seconds) || seconds <= 0) return 'N/A';
  const days = Math.floor(seconds / 86400);
  seconds %= 86400;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return `${days}d ${hours}h ${minutes}m`;
}

function setText(id, value) {
  const element = document.getElementById(id);
  if (element) element.textContent = value;
}

function safeHttpUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : '#';
  } catch (err) {
    return '#';
  }
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
