/** Thin client for the dashboard's public read-only JSON API. */

const DEFAULT_TIMEOUT_MS = 20000;

export class ApiError extends Error {
  constructor(message, { status = 0, endpoint = '' } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.endpoint = endpoint;
  }
}

/**
 * Fetch JSON with a hard timeout.
 *
 * The log and job feeds can be multi-megabyte, and a stalled request used to
 * leave a page showing "Loading..." forever with no way for the user to tell
 * the difference between slow and broken.
 */
export async function getJson(endpoint, { timeout = DEFAULT_TIMEOUT_MS } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(endpoint, {
      signal: controller.signal,
      headers: { Accept: 'application/json' },
    });
    if (!response.ok) {
      throw new ApiError(`Request failed with HTTP ${response.status}`, {
        status: response.status,
        endpoint,
      });
    }
    return await response.json();
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error.name === 'AbortError') {
      throw new ApiError(`Request timed out after ${Math.round(timeout / 1000)}s`, { endpoint });
    }
    throw new ApiError(error.message || 'Network request failed', { endpoint });
  } finally {
    clearTimeout(timer);
  }
}

export const api = {
  metrics: () => getJson('/api/metrics'),
  jobs: () => getJson('/api/section1/jobs', { timeout: 45000 }),
  coverage: () => getJson('/api/section1/coverage', { timeout: 45000 }),
  boardCache: () => getJson('/api/section1/cache', { timeout: 45000 }),
  generation: () => getJson('/api/section2/generation', { timeout: 60000 }),
  archives: () => getJson('/api/section2/archives', { timeout: 45000 }),
  submissions: () => getJson('/api/section3/submissions'),
  failures: () => getJson('/api/section3/failures'),
  vpsLog: () => getJson('/api/vps/log', { timeout: 30000 }),
  rawFile: (filename) => getJson(`/api/files/${encodeURIComponent(filename)}`, { timeout: 60000 }),
};
