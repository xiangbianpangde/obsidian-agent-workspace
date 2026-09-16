/**
 * Paper Workbench API client.
 *
 * Every resource is addressed by an opaque ASCII id — never by a Vault path.
 * The real vault is full of Chinese names, emoji, spaces and full-width colons,
 * and macOS stores them as NFD while other tools produce NFC, so a path-keyed
 * API has to survive double URL-decoding and normalisation mismatches. The
 * backend exposes `source_id` / `paper_id` precisely to avoid that (ADR-009).
 */

const BASE = '/api/paper';
const SOURCES = '/api/paper-sources';

async function request(url, options = {}) {
  const response = await fetch(url, {
    cache: 'no-store',
    ...options,
    headers: { Accept: 'application/json', ...(options.headers || {}) },
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body && body.detail) detail = body.detail;
    } catch {
      /* non-JSON error body: keep the status line */
    }
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  if (response.status === 204) return null;
  return response.json();
}

export const api = {
  /** Paper list, optionally filtered by status or category. */
  listPapers({ status = null, category = null } = {}) {
    const params = new URLSearchParams();
    if (status) params.set('status', status);
    if (category) params.set('category', category);
    const qs = params.toString();
    return request(`${BASE}/papers${qs ? `?${qs}` : ''}`);
  },

  getPaper(paperId) {
    return request(`${BASE}/papers/${encodeURIComponent(paperId)}`);
  },

  createPaper(payload) {
    return request(`${BASE}/papers`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  },

  /** Sources bound to a paper (PDFs, translations, extractions). */
  listSources(paperId) {
    return request(`${BASE}/papers/${encodeURIComponent(paperId)}/sources`);
  },

  /** Content URL for a source. `version` pins the revision so PDF.js cannot
   *  stitch bytes from two different files if the PDF is replaced mid-session. */
  sourceContentUrl(sourceId, version) {
    const base = `${SOURCES}/${encodeURIComponent(sourceId)}/content`;
    return version == null ? base : `${base}?version=${encodeURIComponent(version)}`;
  },

  setStatus(paperId, status, reason = null) {
    return request(`${BASE}/papers/${encodeURIComponent(paperId)}/status`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status, reason }),
    });
  },

  getWorkspaceState(paperId) {
    return request(`${BASE}/papers/${encodeURIComponent(paperId)}/workspace-state`);
  },

  saveWorkspaceState(paperId, state) {
    return request(`${BASE}/papers/${encodeURIComponent(paperId)}/workspace-state`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(state),
    });
  },

  getNote(paperId) {
    return request(`${BASE}/papers/${encodeURIComponent(paperId)}/note`);
  },

  saveNote(paperId, content, expectedHash) {
    return request(`${BASE}/papers/${encodeURIComponent(paperId)}/note`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, expected_hash: expectedHash }),
    });
  },

  createNote(paperId, content) {
    return request(`${BASE}/papers/${encodeURIComponent(paperId)}/note`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    });
  },

  /** Raw text of a markdown source (translations, MinerU extractions). */
  sourceText(sourceId) {
    return fetch(`${SOURCES}/${encodeURIComponent(sourceId)}/content`, {
      cache: 'no-store',
      headers: { Accept: 'text/markdown, text/plain, */*' },
    }).then(async (response) => {
      if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
      return response.text();
    });
  },

  listAnnotations(paperId) {
    return request(`${BASE}/papers/${encodeURIComponent(paperId)}/annotations`);
  },

  createAnnotation(paperId, payload) {
    return request(`${BASE}/papers/${encodeURIComponent(paperId)}/annotations`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  },

  /** Soft delete: the record keeps a `deleted_at` stamp (ADR-002). */
  deleteAnnotation(paperId, annotationId) {
    return request(
      `${BASE}/papers/${encodeURIComponent(paperId)}/annotations/${encodeURIComponent(annotationId)}`,
      { method: 'DELETE' }
    );
  },

  /** Update the launch config so the workspace can be re-opened as configured. */
  getConfig() {
    return request(`${BASE}/config`);
  },
};

/** Thrown when the local draft conflicts with a change made in Obsidian. */
export class ConflictError extends Error {
  constructor(message, remoteHash = null) {
    super(message);
    this.name = 'ConflictError';
    this.remoteHash = remoteHash;
  }
}

export { BASE as PAPER_API_BASE, SOURCES as PAPER_SOURCES_BASE };
