/**
 * PDF Bridge — the frozen interface from ADR-009.
 *
 * PDF.js runs inside a same-origin iframe as the official generic viewer.
 * The parent must never reach into that iframe's DOM and must never depend on
 * PDF.js private class names; everything goes through this adapter. The whole
 * point is that the implementation behind this boundary can be replaced later
 * (e.g. by a custom canvas renderer for P0.5 re-anchoring) without touching any
 * caller.
 *
 * Why the official viewer rather than a hand-built one: text layer, virtual
 * scrolling, rotation, selection and accessibility are each easy to make
 * half-finished, and a half-finished PDF reader is worse than none.
 *
 * Security: the viewer is vendored locally at a pinned version. viewer.mjs and
 * pdf.worker.mjs must always come from the same release — a mismatch typically
 * shows up as some PDFs silently failing to open rather than a startup error.
 */

const VIEWER_PATH = '/static/vendor/pdfjs/web/viewer.html';

/** Must match the vendored pdfjs-dist release. Checked at startup. */
export const PDFJS_VERSION = '6.3.289';

/** Bounded wait for the viewer to announce itself. */
const HANDSHAKE_TIMEOUT_MS = 15000;

export class PdfBridgeError extends Error {
  constructor(message, code = 'BRIDGE_ERROR') {
    super(message);
    this.name = 'PdfBridgeError';
    this.code = code;
  }
}

/**
 * @typedef {Object} SelectionSnapshot
 * @property {string} exact   Selected text.
 * @property {string} prefix  Text immediately before the selection.
 * @property {string} suffix  Text immediately after the selection.
 * @property {number} pageIndex  0-based page the selection sits on.
 */

export class PdfBridge extends EventTarget {
  /**
   * @param {HTMLElement} container Element that will host the iframe.
   * @param {{viewerPath?: string}} [options]
   */
  constructor(container, options = {}) {
    super();
    if (!container) throw new PdfBridgeError('container element is required');
    this.container = container;
    this.viewerPath = options.viewerPath || VIEWER_PATH;
    this.iframe = null;
    this.ready = false;
    this._readyPromise = null;
    this._readyResolve = null;
    this._readyReject = null;
    this._currentPage = 0;
    this._selection = null;
    this._sourceId = null;
    this._sourceVersion = null;
    this._disposed = false;
    this._wired = false;
    this._viewerApp = null;
    this._busHandlers = null;
    /** Incremented on every open(); stale probes and probes from a previous
     *  document must not settle the current promise. */
    this._generation = 0;
    this.pageCount = 0;
  }

  /* ------------------------------------------------------------ lifecycle */

  /**
   * Load a source into the viewer.
   * @param {string} contentUrl Absolute URL of the PDF content endpoint.
   * @param {{sourceId?: string, sourceVersion?: number, pageIndex?: number}} [opts]
   */
  async open(contentUrl, opts = {}) {
    if (this._disposed) throw new PdfBridgeError('bridge disposed');

    // Every open is a new generation. Without this the second open() reuses a
    // bridge whose `ready` flag is still true, so _markReady() returns early,
    // the new ready promise never settles, and the call times out after 15s
    // even though the document loaded fine.
    const generation = ++this._generation;
    this._detachViewerEvents();
    this.ready = false;
    this.pageCount = 0;

    this._sourceId = opts.sourceId ?? null;
    this._sourceVersion = opts.sourceVersion ?? null;
    this._currentPage = opts.pageIndex ?? 0;

    // Reuse one iframe: collapsing a panel must not reload the document and
    // lose the reading position.
    if (!this.iframe) {
      this._createIframe();
    }
    this.show();

    this._readyPromise = new Promise((resolve, reject) => {
      this._readyResolve = resolve;
      this._readyReject = reject;
    });

    const url = `${this.viewerPath}?file=${encodeURIComponent(contentUrl)}`;
    this.iframe.src = url;

    const timer = setTimeout(() => {
      if (generation !== this._generation) return;
      this._readyReject?.(
        new PdfBridgeError('viewer did not become ready in time', 'HANDSHAKE_TIMEOUT')
      );
    }, HANDSHAKE_TIMEOUT_MS);

    try {
      await this._readyPromise;
    } finally {
      clearTimeout(timer);
    }

    if (opts.pageIndex) this.goToPage(opts.pageIndex);
    return this;
  }

  /** Unbind the previous document's event handlers before loading another. */
  _detachViewerEvents() {
    try {
      const bus = this._viewerApp?.eventBus;
      if (bus && this._busHandlers) {
        for (const [name, handler] of this._busHandlers) bus.off?.(name, handler);
      }
    } catch {
      /* the frame is going away regardless */
    }
    this._busHandlers = null;
    this._viewerApp = null;
    this._wired = false;
  }

  _createIframe() {
    const iframe = document.createElement('iframe');
    iframe.className = 'paper-pdf-frame';
    iframe.setAttribute('title', '论文原文 PDF');
    // Same-origin is required: the bridge drives the viewer through
    // window.PDFViewerApplication, which the official viewer exposes for
    // exactly this kind of embedding. No third-party origin is involved.
    iframe.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-popups');
    iframe.style.width = '100%';
    iframe.style.height = '100%';
    iframe.style.border = '0';
    iframe.style.display = 'block';

    iframe.addEventListener('load', () => this._probeReady());

    this.container.replaceChildren(iframe);
    this.iframe = iframe;
  }

  /**
   * Wait for the viewer to finish loading the document.
   *
   * The official viewer does not postMessage to its parent, and it only writes
   * a location hash when navigation happens — so neither is a usable readiness
   * signal. What it does provide is `window.PDFViewerApplication`, exposed for
   * embedding. We poll that same-origin surface, which is the documented
   * integration point rather than a private class name.
   *
   * A slow, large PDF is surfaced through `loadingprogress` events rather than
   * being mistaken for a failure; the handshake timeout still applies as a
   * backstop for a viewer that never initialises at all.
   */
  _probeReady(attempt = 0) {
    if (this._disposed) return;
    const generation = this._generation;

    let app = null;
    try {
      app = this.iframe?.contentWindow?.PDFViewerApplication || null;
    } catch {
      app = null; // not same-origin yet
    }

    // A previous document may still be installed while the new one loads;
    // only the current generation may settle the promise.
    if (generation !== this._generation) return;

    if (app && app.initialized && app.pdfDocument) {
      this._wireViewerEvents(app);
      this._markReady(app.pagesCount || 0);
      return;
    }

    // Surface progress so a large PDF does not look like a hang.
    const loaded = app?.pdfDocument?.numPages || 0;
    if (loaded || (app && app.initialized)) {
      this._emit('loadingprogress', { pages: loaded });
    }

    if (attempt > 600) {
      this._readyReject?.(
        new PdfBridgeError('viewer never finished loading the document', 'VIEWER_NOT_READY')
      );
      return;
    }
    setTimeout(() => this._probeReady(attempt + 1), 100);
  }

  /** Bind to the viewer's own event bus so page changes reach the parent. */
  _wireViewerEvents(app) {
    if (this._wired) return;
    this._wired = true;
    try {
      this._viewerApp = app;
      const bus = app.eventBus;
      if (bus && typeof bus.on === 'function') {
        this._busHandlers = [];
        const on = (name, handler) => {
          bus.on(name, handler);
          this._busHandlers.push([name, handler]);
        };
        on('pagechanging', (evt) => {
          const idx = Number(evt.pageNumber) - 1;
          if (Number.isFinite(idx) && idx >= 0) {
            this._currentPage = idx;
            this._emit('pagechanged', { pageIndex: idx });
          }
        });
        on('scalechanging', (evt) => {
          this._emit('scalechange', { scale: evt.scale });
        });
      }
    } catch (error) {
      // Event wiring is a nicety; reading still works without it.
      this._emit('bridgewarning', { message: String(error) });
    }
  }

  _markReady(pageCount) {
    if (this.ready) return;
    this.ready = true;
    this.pageCount = pageCount;
    this._readyResolve?.({ version: PDFJS_VERSION, pageCount });
    this._emit('documentloaded', {
      sourceId: this._sourceId,
      sourceVersion: this._sourceVersion,
      pageCount,
    });
  }

  /* --------------------------------------------------------------- control */

  /** @param {number} pageIndex 0-based page index. */
  goToPage(pageIndex) {
    if (!this.iframe) return;
    const target = Math.max(0, Math.floor(pageIndex));
    this._currentPage = target;
    try {
      const app = this.iframe.contentWindow?.PDFViewerApplication;
      if (app && typeof app.page === 'number') {
        // 1-based on the viewer's own API; ours is 0-based everywhere else.
        app.page = target + 1;
        this._emit('pagechanged', { pageIndex: target });
        return;
      }
      // Fallback to the standard PDF.js hash navigation.
      this.iframe.contentWindow.location.hash = `page=${target + 1}`;
      this._emit('pagechanged', { pageIndex: target });
    } catch (error) {
      this._emit('bridgewarning', { message: String(error) });
    }
  }

  getCurrentPage() {
    try {
      const app = this.iframe?.contentWindow?.PDFViewerApplication;
      if (app && typeof app.page === 'number' && app.page > 0) {
        return app.page - 1;
      }
    } catch {
      /* fall through to the last commanded page */
    }
    return this._currentPage;
  }

  getPageCount() {
    try {
      return this.iframe?.contentWindow?.PDFViewerApplication?.pagesCount || this.pageCount || 0;
    } catch {
      return this.pageCount || 0;
    }
  }

  setScale(scale) {
    const clamped = Math.min(5, Math.max(0.25, Number(scale) || 1));
    try {
      const app = this.iframe?.contentWindow?.PDFViewerApplication;
      if (app) app.pdfViewer.scaleValue = clamped;
    } catch (error) {
      this._emit('bridgewarning', { message: String(error) });
    }
    this._emit('scalechange', { scale: clamped });
    return clamped;
  }

  /**
   * Current text selection.
   *
   * Selection lives inside the iframe's text layer. Reading it is the one place
   * we must cross the boundary, because the official viewer has no selection
   * event API. Access is same-origin and read-only.
   *
   * The selection's client rect and the page's rendered size are returned too:
   * an anchor needs normalised geometry to be re-resolvable, and raw CSS pixels
   * cannot be stored (they break on zoom, resize and DPI change).
   * @returns {SelectionSnapshot|null}
   */
  getSelection() {
    try {
      const win = this.iframe?.contentWindow;
      if (!win) return null;
      const sel = win.getSelection();
      const text = sel ? String(sel).trim() : '';
      if (!text) return null;

      // Derive a small amount of context so the annotation can be re-anchored
      // by text quote if the page is re-rendered at a different scale.
      let prefix = '';
      let suffix = '';
      let selectionRect = null;
      let pageWidth = 0;
      let pageHeight = 0;
      try {
        const range = sel.getRangeAt(0);
        const node = range.startContainer;
        const full = node.nodeValue || '';
        const start = Math.max(0, range.startOffset - 32);
        prefix = full.slice(start, range.startOffset);
        suffix = full.slice(range.endOffset, range.endOffset + 32);

        const rect = range.getBoundingClientRect();
        const pageNode = win.document.querySelector('.page[data-page-number]');
        const pageRect = pageNode?.getBoundingClientRect();
        if (rect && pageRect && pageRect.width > 0 && pageRect.height > 0) {
          // Coordinates relative to the page box, which is what the crop-box
          // normalisation expects.
          selectionRect = {
            left: rect.left - pageRect.left,
            top: rect.top - pageRect.top,
            right: rect.right - pageRect.left,
            bottom: rect.bottom - pageRect.top,
          };
          pageWidth = pageRect.width;
          pageHeight = pageRect.height;
        }
      } catch {
        /* context and geometry are best effort */
      }

      return {
        exact: text,
        prefix,
        suffix,
        pageIndex: this.getCurrentPage(),
        selectionRect,
        pageWidth,
        pageHeight,
      };
    } catch {
      return null;
    }
  }

  /* ---------------------------------------------------------------- events */

  _emit(type, detail) {
    this.dispatchEvent(new CustomEvent(type, { detail }));
  }

  onDocumentLoaded(cb) {
    return this._subscribe('documentloaded', cb);
  }

  onPageChanged(cb) {
    return this._subscribe('pagechanged', cb);
  }

  onSelectionChanged(cb) {
    return this._subscribe('selectionchanged', cb);
  }

  _subscribe(type, cb) {
    const handler = (event) => cb(event.detail);
    this.addEventListener(type, handler);
    return () => this.removeEventListener(type, handler);
  }

  /* --------------------------------------------------------------- dispose */

  /** Detach listeners and drop the iframe. */
  dispose() {
    this._disposed = true;
    try {
      const bus = this._viewerApp?.eventBus;
      if (bus && this._busHandlers) {
        for (const [name, handler] of this._busHandlers) bus.off?.(name, handler);
      }
    } catch {
      /* the frame is going away regardless */
    }
    this._busHandlers = null;
    this._viewerApp = null;
    this._wired = false;
    if (this.iframe) {
      this.iframe.remove();
      this.iframe = null;
    }
    this.ready = false;
  }

  /** Detach visually but keep the loaded document alive (panel collapse). */
  hide() {
    if (this.iframe) this.iframe.style.display = 'none';
  }

  /** Re-show a previously hidden viewer without reloading it. */
  show() {
    if (this.iframe) this.iframe.style.display = 'block';
  }
}
