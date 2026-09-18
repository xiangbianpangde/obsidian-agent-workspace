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
  /** Paper list, optionally filtered by status, category, or binding state. */
  listPapers({ status = null, category = null, bindingState = null } = {}) {
    const params = new URLSearchParams();
    if (status) params.set('status', status);
    if (category) params.set('category', category);
    if (bindingState) params.set('binding_state', bindingState);
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

  /** Confirm source bindings for an AMBIGUOUS paper and adopt it (ADR-006). */
  resolvePaper(paperId, payload) {
    return request(`${BASE}/papers/${encodeURIComponent(paperId)}/resolve`, {
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

  /**
   * Persist reading state.
   *
   * `expectedVersion` is the state_version last read. Supplying it makes the
   * server refuse a write when another tab has advanced the state, instead of
   * silently letting the later write win.
   */
  saveWorkspaceState(paperId, state, expectedVersion = null) {
    const payload = { ...state };
    if (expectedVersion != null) payload.expected_state_version = expectedVersion;
    return request(`${BASE}/papers/${encodeURIComponent(paperId)}/workspace-state`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
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
    // Markdown is served by /text as text/plain; /content is PDF-only and
    // answers 415 for a rendered document.
    return fetch(`${SOURCES}/${encodeURIComponent(sourceId)}/text`, {
      cache: 'no-store',
      headers: { Accept: 'text/markdown, text/plain, */*' },
    }).then(async (response) => {
      if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
      return response.text();
    });
  },

  /**
   * URL for an image referenced by a paper's Markdown.
   *
   * Images resolve through this same-origin endpoint rather than the vault's
   * generic asset route, which scans the whole vault by basename and can
   * return another paper's figure.
   */
  sourceAssetUrl(sourceId, ref, version) {
    const base = `${SOURCES}/${encodeURIComponent(sourceId)}/asset?ref=${encodeURIComponent(ref)}`;
    return version == null ? base : `${base}&version=${encodeURIComponent(version)}`;
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
