let state = {
  metrics: {},
  submissions: {},
  jobs: [],
  coverage: {},
  generation: [],
  archives: {},
  rawLog: '',
  parsedLogs: [],
  currentLogFilter: 'all',
  activeFunnelStage: 'all'
};

// Single source of truth for the site nav, shared by the desktop nav, the
// mobile drawer, and the mobile bottom bar across every page.
const NAV_PAGES = [
  { slug: 'index', href: 'index.html', icon: '📜', desktopLabel: 'Submissions', drawerLabel: 'Submissions & Failures', bottomLabel: 'Matrix' },
  { slug: 'search', href: 'search.html', icon: '🌊', desktopLabel: 'Job Search', drawerLabel: 'Job Search & Coverage', bottomLabel: 'Search' },
  { slug: 'generation', href: 'generation.html', icon: '🪨', desktopLabel: 'Application Queue', drawerLabel: 'Application Queue', bottomLabel: 'Queue' },
  { slug: 'logs', href: 'logs.html', icon: '🔥', desktopLabel: 'Sync Logs', drawerLabel: 'Real-Time VPS Logs', bottomLabel: 'Logs' },
  { slug: 'inspector', href: 'inspector.html', icon: '📁', desktopLabel: 'Inspector', drawerLabel: 'Raw File Inspector', bottomLabel: 'Files' },
  { slug: 'system-status', href: 'system-status.html', icon: '🛰️', desktopLabel: 'System Status', drawerLabel: 'VPS Config & Failures', bottomLabel: 'Status' }
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
  if (document.getElementById('coverageMetricsSummary')) {
    fetchCoverageAndCache();
  }
  if (document.getElementById('generationQueueView') || document.getElementById('archiveStateView')) {
    fetchSection2();
  }
  if (document.getElementById('structuredLogBody')) {
    fetchVpsLog();
  }
  if (document.getElementById('inspectorSections')) {
    renderInspectorSections();
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

  const resumeFile = item.resume_filename ? escapeHtml(item.resume_filename) : '';
  const resumeLink = resumeFile
    ? `<a href="/api/download/${encodeURIComponent(item.resume_filename)}" target="_blank" download="${resumeFile}" class="resume-download-link">📄 Download Tailored PDF Resume 📥</a>`
    : '<span style="color: var(--text-muted);">N/A</span>';

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
      <span class="detail-label">Candidate Email Used</span>
      <span class="detail-value" style="color: var(--earth);">${escapeHtml(item.email_used)}</span>
    </div>
    <div class="detail-field">
      <span class="detail-label">Tailored Resume PDF</span>
      <div style="margin-top: 0.3rem;">${resumeLink}</div>
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

async function fetchVpsLog() {
  const container = document.getElementById('structuredLogBody');
  if (!container) return;

  try {
    const res = await fetch('/api/vps/log');
    const data = await res.json();
    state.rawLog = data.log || '';
    parseLogData(state.rawLog);
    renderStructuredLogs();
  } catch (err) {
    if (container) container.innerHTML = '<div style="padding: 2rem; color: var(--rose); text-align: center;">Failed to fetch VPS log data.</div>';
  }
}

function parseLogData(rawText) {
  if (!rawText) {
    state.parsedLogs = [];
    return;
  }

  const lines = rawText.split('\n').filter(l => l.trim().length > 0);
  const parsed = [];

  let countError = 0;
  let countAi = 0;
  let countHttp = 0;
  let countSub = 0;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    let timestamp = '';
    let text = line;
    let level = 'info';
    let category = 'info';
    let badgeClass = 'log-badge-info';
    let badgeLabel = 'INFO';

    // Extract Timestamp if present
    const tsMatch = line.match(/^(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2}(?:,\d{3})?)/);
    if (tsMatch) {
      timestamp = tsMatch[1];
      text = line.slice(tsMatch[1].length).trim();
    } else {
      timestamp = 'LIVE-LOG';
    }

    const lower = text.toLowerCase();

    // Categorization
    if (lower.includes('error') || lower.includes('failed') || lower.includes('retry') || lower.includes('exhausted')) {
      level = 'error';
      category = 'error';
      badgeClass = 'log-badge-error';
      badgeLabel = lower.includes('failed') ? 'FAILED' : 'WARN/ERR';
      countError++;
    } else if (lower.includes('score:') || lower.includes('resumeai') || lower.includes('generating tailored') || lower.includes('attempt')) {
      level = 'ai';
      category = 'ai';
      badgeClass = lower.includes('score:') ? 'log-badge-score' : 'log-badge-ai';
      badgeLabel = lower.includes('score:') ? 'SCORE' : 'AI MODEL';
      countAi++;
    } else if (lower.includes('http request:') || lower.includes('httpx') || lower.includes('post https:')) {
      level = 'http';
      category = 'http';
      badgeClass = 'log-badge-http';
      badgeLabel = 'HTTP 200';
      countHttp++;
    } else if (lower.includes('document archive') || lower.includes('submission') || lower.includes('passthrough')) {
      level = 'sub';
      category = 'sub';
      badgeClass = 'log-badge-score';
      badgeLabel = 'ARCHIVE';
      countSub++;
    }

    parsed.push({
      id: i,
      timestamp,
      level,
      category,
      badgeClass,
      badgeLabel,
      raw: line,
      text
    });
  }

  state.parsedLogs = parsed;

  // Update KPI Stats
  if (document.getElementById('logKpiTotal')) document.getElementById('logKpiTotal').textContent = parsed.length;
  if (document.getElementById('logKpiAi')) document.getElementById('logKpiAi').textContent = countAi;
  if (document.getElementById('logKpiHttp')) document.getElementById('logKpiHttp').textContent = countHttp;
  if (document.getElementById('logKpiErrors')) document.getElementById('logKpiErrors').textContent = countError;

  if (document.getElementById('pillCountError')) document.getElementById('pillCountError').textContent = countError;
  if (document.getElementById('pillCountAi')) document.getElementById('pillCountAi').textContent = countAi;
  if (document.getElementById('pillCountHttp')) document.getElementById('pillCountHttp').textContent = countHttp;
  if (document.getElementById('pillCountSub')) document.getElementById('pillCountSub').textContent = countSub;
}

function setLogCategory(cat) {
  state.currentLogFilter = cat;
  document.querySelectorAll('.pill-btn, .pill').forEach(btn => {
    if (btn.getAttribute('data-filter')) {
      btn.classList.toggle('active', btn.getAttribute('data-filter') === cat);
    }
  });
  renderStructuredLogs();
}

function renderStructuredLogs() {
  const container = document.getElementById('structuredLogBody');
  if (!container) return;

  const searchInput = document.getElementById('logSearch');
  const query = (searchInput ? searchInput.value : '').toLowerCase();

  let filtered = state.parsedLogs;

  if (state.currentLogFilter !== 'all') {
    filtered = filtered.filter(item => item.category === state.currentLogFilter);
  }

  if (query) {
    filtered = filtered.filter(item => item.raw.toLowerCase().includes(query));
  }

  if (filtered.length === 0) {
    container.innerHTML = '<div style="padding: 2.5rem; text-align: center; color: var(--text-muted);">No log entries matching current filters.</div>';
    return;
  }

  const html = filtered.map(item => `
    <div class="log-entry-row">
      <span class="log-time">${escapeHtml(item.timestamp)}</span>
      <div class="log-badge-wrapper">
        <span class="log-badge ${item.badgeClass}">${escapeHtml(item.badgeLabel)}</span>
      </div>
      <span class="log-text">${escapeHtml(item.text)}</span>
    </div>
  `).join('');

  container.innerHTML = html;

  const autoCheck = document.getElementById('autoScrollCheck');
  if (autoCheck && autoCheck.checked) {
    container.scrollTop = container.scrollHeight;
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
      (item.email_used || '').toLowerCase().includes(query) ||
      (item.resume_filename || '').toLowerCase().includes(query)
    );
  });

  if (filtered.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--text-muted);">No matching submissions found.</td></tr>';
    return;
  }

  tbody.innerHTML = filtered.map(([key, item]) => {
    const resumeFile = item.resume_filename ? escapeHtml(item.resume_filename) : '';
    const resumeCell = resumeFile
      ? `<a href="/api/download/${encodeURIComponent(item.resume_filename)}" target="_blank" download="${resumeFile}" class="resume-download-link" title="Click to download tailored PDF resume" onclick="event.stopPropagation();">📄 ${resumeFile} 📥</a>`
      : '<span style="color: var(--text-muted); font-size: 0.75rem;">N/A</span>';

    return `
      <tr onclick="openJobDetail('${escapeHtml(key)}')">
        <td style="font-family: var(--font-mono); font-size: 0.8rem; color: var(--text-muted);">${formatDate(item.applied_at)}</td>
        <td style="font-weight: 600;">${escapeHtml(item.company)}</td>
        <td>${escapeHtml(item.role)}</td>
        <td><span class="badge badge-${(item.ats || '').toLowerCase()}">${escapeHtml(item.ats)}</span></td>
        <td style="font-family: var(--font-mono); font-size: 0.8rem;">${escapeHtml(item.email_used)}</td>
        <td>${resumeCell}</td>
        <td><span class="badge badge-confirmed">${escapeHtml(item.status)}</span></td>
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
      <td><a href="${escapeHtml(item.job_url || item.apply_url || '#')}" target="_blank" style="color: var(--air-cyan); text-decoration: none;">View Listing ↗</a></td>
    </tr>
  `).join('');
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
              <div>Candidate Email: <span style="color: var(--air);">${escapeHtml(item.identity?.email_used || 'N/A')}</span></div>
              <div style="margin-top: 0.3rem;">Fingerprint: <code style="color: var(--lotus);">${escapeHtml(item.record_fingerprint || 'N/A')}</code></div>
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
  { key: 'submission_log', filename: 'submission_log.json', label: 'submission_log.json (Section 3)' },
  { key: 'vps_application_failures', filename: 'vps_application_failures.json', label: 'vps_application_failures.json (Section 3)' },
  { key: 'vps_application_state', filename: 'vps_application_state.json', label: 'vps_application_state.json (Section 3)' },
  { key: 'ai_jobs', filename: 'ai_jobs.csv', label: 'ai_jobs.csv (Section 1)' },
  { key: 'job_search_coverage', filename: 'job_search_coverage.json', label: 'job_search_coverage.json (Section 1)' },
  { key: 'ats_boards_cache', filename: 'ats_boards_cache.json', label: 'ats_boards_cache.json (Section 1)' },
  { key: 'vps_generation_jobs', filename: 'vps_generation_jobs.json', label: 'vps_generation_jobs.json (Section 2)' },
  { key: 'vps_document_archive_state', filename: 'vps_document_archive_state.json', label: 'vps_document_archive_state.json (Section 2)' }
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

function copyLogsToClipboard() {
  if (!state.rawLog) {
    alert('No log text available to copy.');
    return;
  }
  navigator.clipboard.writeText(state.rawLog).then(() => {
    alert('Full VPS log copied to clipboard!');
  }).catch(err => {
    console.error('Failed to copy logs', err);
  });
}

async function refreshDashboardData() {
  const btn = document.getElementById('refreshBtn');
  const originalText = btn ? btn.innerHTML : '';
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span>⏳ Refreshing...</span>';
  }

  const loaders = [fetchMetrics, fetchSubmissions, fetchJobs, fetchCoverageAndCache, fetchSection2, fetchVpsLog];
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

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
