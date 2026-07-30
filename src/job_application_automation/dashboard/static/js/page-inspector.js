/** Raw report file inspector. */

import { initShell } from './chrome.js';
import { api } from './api.js';
import { formatNumber } from './format.js';

const state = { content: '', filename: '' };

function setStatus(message, tone = 'muted') {
  const el = document.getElementById('inspectorStatus');
  if (el) {
    el.textContent = message;
    el.dataset.tone = tone;
  }
}

function render() {
  const viewer = document.getElementById('fileViewer');
  if (!viewer) return;

  const query = document.getElementById('fileFilter')?.value.trim().toLowerCase() || '';
  if (!query) {
    viewer.textContent = state.content;
    return;
  }

  // Filtering a raw report keeps the inspector usable on the multi-megabyte
  // job feeds, where scrolling to a single record was otherwise impractical.
  const lines = state.content.split('\n');
  const matching = lines.filter((line) => line.toLowerCase().includes(query));
  viewer.textContent = matching.length
    ? matching.join('\n')
    : `No lines in ${state.filename} contain "${query}".`;
  setStatus(`${formatNumber(matching.length)} of ${formatNumber(lines.length)} lines match`, 'muted');
}

async function loadFile() {
  const select = document.getElementById('fileSelect');
  const viewer = document.getElementById('fileViewer');
  if (!select || !viewer) return;

  const filename = select.value;
  state.filename = filename;
  setStatus(`Loading ${filename}…`);
  viewer.textContent = '';
  viewer.setAttribute('aria-busy', 'true');

  try {
    const data = await api.rawFile(filename);
    if (data.error) throw new Error(data.error);
    state.content = typeof data.content === 'string' ? data.content : JSON.stringify(data, null, 2);
    const lines = state.content.split('\n').length;
    setStatus(`${formatNumber(state.content.length)} characters · ${formatNumber(lines)} lines`);
    render();
  } catch (error) {
    state.content = '';
    viewer.textContent = '';
    setStatus(`Could not load ${filename}: ${error.message}`, 'error');
    throw error;
  } finally {
    viewer.removeAttribute('aria-busy');
  }
}

function copyContents() {
  if (!state.content) return;
  navigator.clipboard?.writeText(state.content).then(
    () => setStatus(`Copied ${state.filename} to the clipboard`, 'ok'),
    () => setStatus('Clipboard access was denied by the browser', 'error'),
  );
}

document.getElementById('fileSelect')?.addEventListener('change', () => {
  loadFile().catch(() => {});
});
document.getElementById('fileFilter')?.addEventListener('input', render);
document.getElementById('copyFileBtn')?.addEventListener('click', copyContents);

const downloadLink = document.getElementById('downloadFileLink');
document.getElementById('fileSelect')?.addEventListener('change', (event) => {
  if (downloadLink) downloadLink.href = `/api/files/${encodeURIComponent(event.target.value)}`;
});

initShell({
  page: 'inspector',
  kpis: [],
  loaders: [loadFile],
});
