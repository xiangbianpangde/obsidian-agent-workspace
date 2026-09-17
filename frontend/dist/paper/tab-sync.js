/**
 * Cross-tab coordination.
 *
 * The backend already serialises writes (per-paper lock, optimistic hashes), so
 * two tabs cannot corrupt each other's data. What they *can* do is surprise the
 * user: tab A saves, tab B still holds the previous hash, and tab B only learns
 * about it when the user clicks save and gets a 409.
 *
 * This module closes that awareness gap. It is purely advisory — it never writes
 * and never decides conflicts. The server remains the authority on whether a
 * write is allowed; this only lets a tab find out sooner.
 *
 * Two mechanisms are used because neither is sufficient alone:
 *
 *   BroadcastChannel — instant, same-origin, but not universally available.
 *   storage events   — a fallback that works wherever localStorage does, and
 *                      reaches tabs that a BroadcastChannel would too.
 *
 * Both are best-effort: if neither is available the app behaves exactly as
 * before, with the server's 409 as the backstop.
 */

const CHANNEL_NAME = 'personal-ai-workspace.paper.v1';
const STORAGE_KEY = 'personal-ai-workspace.paper.activity';

/** Message kinds that other tabs care about. */
export const ACTIVITY = {
  NOTE_SAVED: 'note-saved',
  ANNOTATION_CHANGED: 'annotation-changed',
  WORKSPACE_STATE_SAVED: 'workspace-state-saved',
};

export class TabCoordinator extends EventTarget {
  /**
   * @param {{tabId?: string}} [options]
   */
  constructor(options = {}) {
    super();
    this.tabId = options.tabId || randomId();
    this._channel = null;
    this._storageHandler = null;
    this._closed = false;
    this._open();
  }

  _open() {
    // Preferred path: a real channel with structured messages.
    try {
      if (typeof BroadcastChannel !== 'undefined') {
        this._channel = new BroadcastChannel(CHANNEL_NAME);
        this._channel.onmessage = (event) => this._receive(event.data);
      }
    } catch {
      this._channel = null;
    }

    // Fallback path. BroadcastChannel is unavailable in some embedded or
    // privacy-restricted contexts, and reading the tab's own writes must not be
    // reported as another tab's activity.
    try {
      if (typeof window !== 'undefined' && window.addEventListener) {
        this._storageHandler = (event) => {
          if (event.key !== STORAGE_KEY || !event.newValue) return;
          let payload = null;
          try {
            payload = JSON.parse(event.newValue);
          } catch {
            return;
          }
          this._receive(payload);
        };
        window.addEventListener('storage', this._storageHandler);
      }
    } catch {
      this._storageHandler = null;
    }
  }

  /** Announce local activity to the other tabs. */
  announce(kind, detail = {}) {
    if (this._closed) return;
    const message = { kind, tabId: this.tabId, at: Date.now(), ...detail };
    try {
      this._channel?.postMessage(message);
    } catch {
      /* channel closed underneath us; the storage path may still work */
    }
    try {
      // Writing the same key twice with identical content fires no event, so a
      // counter is folded in to guarantee the value differs.
      if (typeof localStorage !== 'undefined') {
        localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...message, nonce: randomId() }));
      }
    } catch {
      /* storage may be full or blocked; coordination is optional */
    }
  }

  _receive(message) {
    if (!message || typeof message !== 'object') return;
    // Our own announcement arrives back through the storage path.
    if (message.tabId === this.tabId) return;
    this.dispatchEvent(new CustomEvent(message.kind, { detail: message }));
    this.dispatchEvent(new CustomEvent('activity', { detail: message }));
  }

  on(kind, callback) {
    const handler = (event) => callback(event.detail);
    this.addEventListener(kind, handler);
    return () => this.removeEventListener(kind, handler);
  }

  close() {
    this._closed = true;
    try {
      this._channel?.close();
    } catch {
      /* already closed */
    }
    this._channel = null;
    if (this._storageHandler && typeof window !== 'undefined') {
      window.removeEventListener('storage', this._storageHandler);
    }
    this._storageHandler = null;
  }
}

function randomId() {
  try {
    if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID();
    if (typeof crypto !== 'undefined' && crypto.getRandomValues) {
      const buffer = new Uint8Array(8);
      crypto.getRandomValues(buffer);
      return Array.from(buffer, (b) => b.toString(16).padStart(2, '0')).join('');
    }
  } catch {
    /* fall through */
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export { CHANNEL_NAME as TAB_CHANNEL_NAME, STORAGE_KEY as TAB_STORAGE_KEY };
