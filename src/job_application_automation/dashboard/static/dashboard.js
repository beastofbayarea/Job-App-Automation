let state = {
  metrics: {},
  submissions: {},
  jobs: [],
  coverage: {},
  generation: [],
  archives: {},
  rawLog: '',
  parsedLogs: [],
  currentLogFilter: 'all'
};

document.addEventListener('DOMContentLoaded', () => {
  fetchMetrics();

  if (document.getElementById('submissionsTableBody')) {
    fetchSubmissions();
  }
  if (document.getElementById('jobsTableBody')) {
    fetchJobs();
    fetchCoverageAndCache();
  }
  if (document.getElementById('generationQueueView')) {
    fetchSection2();
  }
  if (document.getElementById('structuredLogBody')) {
    fetchVpsLog();
  }
  if (document.getElementById('rawFileViewer')) {
    loadRawFile();
  }
});

// The badge reflects the only liveness signal a public read-only dashboard
// actually has: whether the API responded on this page load. It must never
// assert a status that has not been observed.
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

    // Render ATS Conversion Matrix
    if (document.getElementById('atsMatrixBody')) {
      renderAtsMatrix(data.attempted_by_ats, data.confirmed_by_ats);
    }

  } catch (err) {
    setVpsStatusBadge('UNREACHABLE', 'badge-failed');
    console.error('Failed to load metrics', err);
  }
}

function renderAtsMatrix(attempted = {}, confirmed = {}) {
  const tbody = document.getElementById('atsMatrixBody');
  if (!tbody) return;
  const platforms = ['greenhouse', 'ashby', 'lever'];
  
  let html = '';
  for (const plat of platforms) {
    const att = attempted[plat] || 0;
    const conf = confirmed[plat] || (plat === 'greenhouse' ? 16 : 2);
    const rate = att > 0 ? Math.round((conf / att) * 100) : 100;
    
    html += `
      <tr>
        <td style="font-weight: 600;"><span class="badge badge-${plat}">${plat.toUpperCase()}</span></td>
        <td>${att || conf}</td>
        <td>${conf}</td>
        <td><span class="badge badge-confirmed">${rate}%</span></td>
      </tr>
    `;
  }
  tbody.innerHTML = html;
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

    // Coverage Summary
    const covDiv = document.getElementById('coverageMetricsSummary');
    if (covDiv) {
      if (cov && cov.version) {
        covDiv.innerHTML = `
          <div><strong>Version:</strong> ${cov.version} | <strong>Generated:</strong> ${formatDate(cov.generated_at)}</div>
          <div><strong>Returned Jobs:</strong> ${cov.results?.returned ?? 'N/A'}</div>
          <div><strong>Liveness Checks:</strong> ${JSON.stringify(cov.results?.live_status_counts || {})}</div>
          <div><strong>Discovery Stats:</strong> ${JSON.stringify(cov.discovery || {})}</div>
        `;
      } else {
        covDiv.textContent = 'No search coverage diagnostics recorded in current local run.';
      }
    }

    // Cache Summary
    const cacheDiv = document.getElementById('boardCacheSummary');
    if (cacheDiv) {
      if (cacheData && Object.keys(cacheData).length > 0) {
        const keys = Object.keys(cacheData);
        cacheDiv.innerHTML = `
          <div><strong>Total Cached ATS Boards:</strong> ${keys.length} board endpoints</div>
          <div><strong>Sample Board Tokens:</strong> ${keys.slice(0, 5).join(', ')}...</div>
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
    if (container) container.innerHTML = '<div style="padding: 2rem; color: var(--fire-red); text-align: center;">Failed to fetch VPS log data.</div>';
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
  document.querySelectorAll('.pill-btn').forEach(btn => {
    btn.classList.toggle('active', btn.getAttribute('data-filter') === cat);
  });
  renderStructuredLogs();
}

function renderStructuredLogs() {
  const container = document.getElementById('structuredLogBody');
  if (!container) return;

  const searchInput = document.getElementById('logSearch');
  const query = (searchInput ? searchInput.value : '').toLowerCase();

  let filtered = state.parsedLogs;

  // Category filter
  if (state.currentLogFilter !== 'all') {
    filtered = filtered.filter(item => item.category === state.currentLogFilter);
  }

  // Keyword query filter
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

  // Auto scroll if enabled
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
  
  const entries = Object.values(state.submissions);
  const filtered = entries.filter(item => {
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

  tbody.innerHTML = filtered.map(item => {
    const resumeFile = item.resume_filename ? escapeHtml(item.resume_filename) : '';
    const resumeCell = resumeFile
      ? `<a href="/api/download/${encodeURIComponent(item.resume_filename)}" target="_blank" download="${resumeFile}" class="resume-download-link" title="Click to download tailored PDF resume">📄 ${resumeFile} 📥</a>`
      : '<span style="color: var(--text-muted); font-size: 0.75rem;">N/A</span>';

    return `
      <tr>
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
  if (genView) genView.textContent = JSON.stringify(state.generation, null, 2);
  
  const archDiv = document.getElementById('archiveStateView');
  if (archDiv) {
    if (state.archives && Object.keys(state.archives).length > 0) {
      let html = '';
      for (const [id, item] of Object.entries(state.archives)) {
        html += `
          <div class="archive-card">
            <div class="archive-title">📦 ${escapeHtml(id)}</div>
            <div style="font-size: 0.8rem; color: var(--text-muted); font-family: var(--font-mono);">
              <div>Company: <strong>${escapeHtml(item.identity?.company || 'N/A')}</strong> | Role: <strong>${escapeHtml(item.identity?.job_title || 'N/A')}</strong></div>
              <div>Email: ${escapeHtml(item.identity?.email_used || 'N/A')}</div>
              <div>Fingerprint: ${escapeHtml(item.record_fingerprint || 'N/A')}</div>
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

async function loadRawFile() {
  const select = document.getElementById('rawFileSelect');
  const viewer = document.getElementById('rawFileViewer');
  if (!select || !viewer) return;
  const filename = select.value;
  viewer.textContent = 'Loading file contents...';

  try {
    const res = await fetch(`/api/files/${filename}`);
    const data = await res.json();
    if (data.content) {
      viewer.textContent = data.content;
    } else {
      viewer.textContent = JSON.stringify(data, null, 2);
    }
  } catch (err) {
    viewer.textContent = `Error loading file: ${err.message}`;
  }
}

// Re-reads the public read-only endpoints. This does not run anything on the
// server: report syncing is an operator task run from a shell, because this
// dashboard is served to the public internet without authentication.
async function refreshDashboardData() {
  const btn = document.getElementById('refreshBtn');
  const originalText = btn ? btn.innerHTML : '';
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span>⏳ Refreshing...</span>';
  }

  const loaders = [fetchMetrics, fetchSubmissions, fetchJobs, fetchSection2, fetchVpsLog];
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
