/**
 * Four-pane layout: sidebar | splitter | PDF | splitter | Markdown | splitter | notes.
 *
 * Implemented with CSS Grid and pointer capture. `setPointerCapture` matters:
 * without it a fast drag leaves the splitter and the pointerup lands on another
 * element, leaving the pane stuck to the cursor.
 *
 * Only layout *geometry* is persisted here. Note text, selected text, annotation
 * bodies and paper titles must never go into localStorage — that store is not
 * covered by the endpoint's `Cache-Control: no-store` and would outlive the
 * session on disk.
 */

const STORAGE_KEY = 'personal-ai-workspace.paper-layout.v1';
const SCHEMA_VERSION = 1;

const DEFAULTS = {
  schema_version: SCHEMA_VERSION,
  left_width: 260,
  right_width: 340,
  pdf_ratio: 0.5,
  left_collapsed: false,
  right_collapsed: false,
};

const MIN_SIDE = 180;
const MAX_SIDE = 560;
const MIN_CENTER_RATIO = 0.2;
const MAX_CENTER_RATIO = 0.8;

export class PaperLayout {
  /**
   * @param {HTMLElement} root Grid container.
   */
  constructor(root) {
    if (!root) throw new Error('layout root element is required');
    this.root = root;
    this.state = this._load();
    this._dragging = null;
    this._apply();
    this._bindSplitters();
    this._observeResize();
  }

  /* ------------------------------------------------------------- persistence */

  _load() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return { ...DEFAULTS };
      const parsed = JSON.parse(raw);
      if (!parsed || parsed.schema_version !== SCHEMA_VERSION) return { ...DEFAULTS };
      return { ...DEFAULTS, ...parsed, schema_version: SCHEMA_VERSION };
    } catch {
      return { ...DEFAULTS };
    }
  }

  _save() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(this.state));
    } catch {
      /* storage may be full or blocked; layout is not worth failing over */
    }
  }

  /* ------------------------------------------------------------------ apply */

  _apply() {
    const { left_collapsed, right_collapsed, left_width, right_width, pdf_ratio } =
      this.state;

    // Collapsed panes keep their previous width in state so restoring is exact.
    const leftCol = left_collapsed ? '0px' : `${left_width}px`;
    const rightCol = right_collapsed ? '0px' : `${right_width}px`;

    this.root.style.gridTemplateColumns =
      `${leftCol} var(--splitter) ${1 - pdf_ratio}fr var(--splitter) ${pdf_ratio}fr var(--splitter) ${rightCol}`;

    this.root.dataset.leftCollapsed = String(left_collapsed);
    this.root.dataset.rightCollapsed = String(right_collapsed);
  }

  /* --------------------------------------------------------------- splitters */

  _bindSplitters() {
    this.root.querySelectorAll('[data-splitter]').forEach((handle) => {
      handle.addEventListener('pointerdown', (event) => this._onDown(event, handle));
      handle.addEventListener('keydown', (event) => this._onKey(event, handle));
    });
  }

  _onDown(event, handle) {
    const kind = handle.dataset.splitter;
    event.preventDefault();
    handle.setPointerCapture(event.pointerId);
    this._dragging = {
      kind,
      handle,
      startX: event.clientX,
      startState: { ...this.state },
      availableWidth: this.root.clientWidth,
    };
    this.root.dataset.dragging = 'true';

    const move = (e) => this._onMove(e);
    const up = (e) => {
      handle.releasePointerCapture?.(event.pointerId);
      handle.removeEventListener('pointermove', move);
      handle.removeEventListener('pointerup', up);
      handle.removeEventListener('pointercancel', up);
      this._dragging = null;
      delete this.root.dataset.dragging;
      this._save();
      this._emitResize();
    };

    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', up);
    handle.addEventListener('pointercancel', up);
  }

  _onMove(event) {
    const drag = this._dragging;
    if (!drag) return;
    const dx = event.clientX - drag.startX;

    if (drag.kind === 'left') {
      const next = clamp(drag.startState.left_width + dx, MIN_SIDE, MAX_SIDE);
      this.state.left_width = Math.round(next);
      this.state.left_collapsed = false;
    } else if (drag.kind === 'right') {
      const next = clamp(drag.startState.right_width - dx, MIN_SIDE, MAX_SIDE);
      this.state.right_width = Math.round(next);
      this.state.right_collapsed = false;
    } else if (drag.kind === 'center') {
      const centerWidth = Math.max(1, drag.availableWidth);
      const next = clamp(
        drag.startState.pdf_ratio + dx / centerWidth,
        MIN_CENTER_RATIO,
        MAX_CENTER_RATIO
      );
      this.state.pdf_ratio = Number(next.toFixed(4));
    }
    this._apply();
    this._emitResize();
  }

  _onKey(event, handle) {
    // Keyboard accessibility: arrows nudge, Home/End collapse.
    const kind = handle.dataset.splitter;
    const step = event.shiftKey ? 40 : 10;
    let handled = true;

    if (event.key === 'ArrowLeft') {
      this._nudge(kind, -step);
    } else if (event.key === 'ArrowRight') {
      this._nudge(kind, step);
    } else if (event.key === 'Home') {
      this.toggle(kind === 'left' ? 'left' : 'right');
    } else {
      handled = false;
    }
    if (handled) {
      event.preventDefault();
      this._save();
      this._emitResize();
    }
  }

  _nudge(kind, delta) {
    if (kind === 'left') {
      this.state.left_width = Math.round(
        clamp(this.state.left_width + delta, MIN_SIDE, MAX_SIDE)
      );
      this.state.left_collapsed = false;
    } else if (kind === 'right') {
      this.state.right_width = Math.round(
        clamp(this.state.right_width - delta, MIN_SIDE, MAX_SIDE)
      );
      this.state.right_collapsed = false;
    } else {
      this.state.pdf_ratio = Number(
        clamp(this.state.pdf_ratio + delta / 400, MIN_CENTER_RATIO, MAX_CENTER_RATIO).toFixed(4)
      );
    }
    this._apply();
  }

  /* ------------------------------------------------------------------- public */

  toggle(which) {
    if (which === 'left') {
      this.state.left_collapsed = !this.state.left_collapsed;
    } else if (which === 'right') {
      this.state.right_collapsed = !this.state.right_collapsed;
    }
    this._apply();
    this._save();
    this._emitResize();
    return this.state;
  }

  /* ------------------------------------------------------------------ resize */

  _observeResize() {
    if (typeof ResizeObserver === 'undefined') return;
    let queued = false;
    this._resizeObserver = new ResizeObserver(() => {
      // Reflowing a PDF on every pointer event is what makes a viewer feel
      // heavy; coalesce to one emit per frame.
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        this._emitResize();
      });
    });
    this._resizeObserver.observe(this.root);
  }

  _emitResize() {
    const width = this.root.clientWidth;
    this.root.dispatchEvent(
      new CustomEvent('layoutresize', {
        bubbles: true,
        detail: { width, height: this.root.clientHeight },
      })
    );
  }

  destroy() {
    this._resizeObserver?.disconnect();
  }
}

function clamp(value, min, max) {
  if (Number.isNaN(value)) return min;
  return Math.min(max, Math.max(min, value));
}

export { DEFAULTS as PAPER_LAYOUT_DEFAULTS, STORAGE_KEY as PAPER_LAYOUT_STORAGE_KEY };
