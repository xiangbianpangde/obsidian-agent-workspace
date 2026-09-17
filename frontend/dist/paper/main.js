/**
 * Paper Workbench entry point.
 *
 * Mounted by the host `index.html` via a single module script tag. Keeping this
 * subsystem in its own ES modules is deliberate: the host page is already a
 * 200 KB single file, and folding a PDF bridge, an annotation layer and a
 * four-pane layout into it would make any change a whole-application
 * regression risk (ADR-009).
 */

import { api } from './api.js';
import { PaperLayout } from './layout.js';
import { PdfBridge, PDFJS_VERSION } from './pdf-bridge.js';
import { MarkdownPane } from './markdown-pane.js';
import { NoteEditor } from './note-pane.js';
import {
  AnnotationList,
  makePdfAnchor,
  makeMarkdownAnchor,
} from './annotations.js';
import { assembleAiContext } from './ai-context.js';

const STATUS_LABELS = {
  UNREAD: '未看',
  READING: '正在看',
  COMPLETED: '看完',
};

const ROLE_LABELS = {
  ORIGINAL_PDF: '原文 PDF',
  SUPPLEMENTAL_PDF: '附加 PDF',
  TRANSLATION_FULL: '全文翻译',
  TRANSLATION_GUIDE: '翻译导读',
  EXTRACTED_MARKDOWN: '解析 Markdown',
  OTHER_MARKDOWN: '其他 Markdown',
};

export class PaperWorkbench {
  constructor(root) {
    this.root = root;
    this.layout = null;
    this.pdf = null;
    this.markdown = null;
    this.note = null;
    this.annotations = null;
    this.papers = [];
    this.selected = null;
    this.sources = [];
    this.activeSourceId = null;
    this.statusFilter = null;
  }

  async init() {
    this.root.innerHTML = SHELL_HTML;

    this.el = {
      paperList: this.root.querySelector('[data-role="paper-list"]'),
      paperCount: this.root.querySelector('[data-role="paper-count"]'),
      statusFilter: this.root.querySelector('[data-role="status-filter"]'),
      title: this.root.querySelector('[data-role="paper-title"]'),
      meta: this.root.querySelector('[data-role="paper-meta"]'),
      sourceTabs: this.root.querySelector('[data-role="source-tabs"]'),
      pdfHost: this.root.querySelector('[data-role="pdf-host"]'),
      markdownHost: this.root.querySelector('[data-role="markdown-host"]'),
      noteHost: this.root.querySelector('[data-role="note-host"]'),
      annHost: this.root.querySelector('[data-role="annotation-host"]'),
      btnLeft: this.root.querySelector('[data-role="toggle-left"]'),
      btnRight: this.root.querySelector('[data-role="toggle-right"]'),
      emptyState: this.root.querySelector('[data-role="empty-state"]'),
      viewerVersion: this.root.querySelector('[data-role="pdfjs-version"]'),
      statusButton: this.root.querySelector('[data-role="status-button"]'),
    };

    this.layout = new PaperLayout(this.root.querySelector('[data-role="grid"]'));
    this.pdf = new PdfBridge(this.el.pdfHost);
    this.markdown = new MarkdownPane(this.el.markdownHost);
    this.note = new NoteEditor(this.el.noteHost, {
      load: () => api.getNote(this.selected.paper_id),
      save: (content, hash) => api.saveNote(this.selected.paper_id, content, hash),
      create: (content) => api.createNote(this.selected.paper_id, content),
    });
    this.note.mount();
    this.annotations = new AnnotationList(this.el.annHost, {
      list: () => api.listAnnotations(this.selected.paper_id),
      onJump: (item) => this.jumpToAnnotation(item),
    });
    this.annotations.mount();

    this.el.viewerVersion.textContent = `PDF.js ${PDFJS_VERSION}`;
    this.el.btnLeft.addEventListener('click', () => this.layout.toggle('left'));
    this.el.btnRight.addEventListener('click', () => this.layout.toggle('right'));
    this.el.statusFilter.addEventListener('change', () => {
      this.statusFilter = this.el.statusFilter.value || null;
      this.loadPapers();
    });
    this.el.statusButton.addEventListener('click', () => this.advanceStatus());

    // Selection availability drives the annotation button; the panes do not
    // know about the list, so the workbench is the one place that joins them.
    this.annotations.addEventListener('addrequest', () => this.createAnnotationFromSelection());
    document.addEventListener('selectionchange', () => this._refreshSelectionState());
    this.el.markdownHost.addEventListener('mouseup', () => this._refreshSelectionState());

    this.root.addEventListener('layoutresize', () => {
      if (this.pdf?.iframe) this.pdf.iframe.style.height = '100%';
    });

    await this.loadPapers();
    this._selectFirst();
  }

  async loadPapers() {
    try {
      const data = await api.listPapers({ status: this.statusFilter });
      this.papers = data.papers || [];
      this._renderPaperList();
    } catch (error) {
      this.el.paperList.innerHTML = `<p class="paper-error">无法加载论文列表：${escapeHtml(error.message)}</p>`;
    }
  }

  _renderPaperList() {
    this.el.paperCount.textContent = String(this.papers.length);
    if (!this.papers.length) {
      this.el.paperList.innerHTML = '<p class="paper-muted">没有符合条件的论文</p>';
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const paper of this.papers) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'paper-item';
      button.dataset.paperId = paper.paper_id;
      if (this.selected && this.selected.paper_id === paper.paper_id) {
        button.classList.add('is-active');
      }
      button.innerHTML = `
        <span class="paper-item-title">${escapeHtml(paper.title || paper.display_title || '未命名')}</span>
        <span class="paper-item-meta">
          <span class="paper-status" data-status="${escapeHtml(paper.status)}">${STATUS_LABELS[paper.status] || paper.status}</span>
          ${paper.source_count ? `<span class="paper-badge">${paper.source_count} 来源</span>` : ''}
        </span>`;
      button.addEventListener('click', () => this.selectPaper(paper.paper_id));
      fragment.appendChild(button);
    }
    this.el.paperList.replaceChildren(fragment);
  }

  _selectFirst() {
    if (this.papers.length) this.selectPaper(this.papers[0].paper_id);
  }

  async selectPaper(paperId) {
    // Do not silently drop unsaved work when switching papers.
    if (this.note?.hasUnsavedWork()) {
      await this.note.flush();
    }
    try {
      const [paper, sources] = await Promise.all([
        api.getPaper(paperId),
        api.listSources(paperId),
      ]);
      this.selected = paper;
      this.sources = sources.sources || [];
      this.activeSourceId = null;

      this.el.emptyState.hidden = true;
      this.el.title.textContent = paper.title || paper.display_title || '未命名';
      this._renderMeta();
      this._renderPaperList();
      this._renderSources();
      this._renderStatusButton();

      // Mark READING only once a readable source actually loads, never on a
      // mere list click (ADR-007).
      const primary = this.sources.find((s) => s.media_kind === 'PDF');
      if (primary) {
        await this.openSource(primary.source_id);
        await this.markOpened();
      } else {
        const md = this.sources.find((s) => s.media_kind === 'MARKDOWN');
        if (md) await this.openSource(md.source_id);
      }

      await Promise.all([this.note.load(), this.annotations.load()]);
    } catch (error) {
      this.el.title.textContent = '加载失败';
      this.el.meta.textContent = error.message;
    }
  }

  _renderMeta() {
    const paper = this.selected;
    if (!paper) return;
    const parts = [
      STATUS_LABELS[paper.status] || paper.status,
      paper.category_relpath,
      `${this.sources.length} 个来源`,
    ].filter(Boolean);
    this.el.meta.textContent = parts.join(' · ');
  }

  _renderStatusButton() {
    const paper = this.selected;
    if (!paper || !this.el.statusButton) return;
    const next = { UNREAD: 'READING', READING: 'COMPLETED', COMPLETED: 'READING' }[paper.status];
    const nextLabel = { UNREAD: '开始阅读', READING: '标记看完', COMPLETED: '重新阅读' }[paper.status];
    this.el.statusButton.textContent = nextLabel;
    this.el.statusButton.dataset.next = next;
  }

  /** UNREAD -> READING fires on a successful source load, not on a list click. */
  async markOpened() {
    if (!this.selected) return;
    if (this.selected.status === 'COMPLETED') {
      // Opening a finished paper must not downgrade it.
      return;
    }
    if (this.selected.status !== 'UNREAD') return;
    try {
      await api.setStatus(this.selected.paper_id, 'READING', 'first_load');
      this.selected.status = 'READING';
      this._renderMeta();
      this._renderStatusButton();
      this._renderPaperList();
    } catch (error) {
      console.warn('状态更新失败:', error.message);
    }
  }

  /** COMPLETED only ever comes from an explicit user action. */
  async advanceStatus() {
    if (!this.selected) return;
    const next = this.el.statusButton.dataset.next;
    if (!next) return;
    try {
      const result = await api.setStatus(this.selected.paper_id, next, 'user_action');
      this.selected.status = result.status;
      this._renderMeta();
      this._renderStatusButton();
      this._renderPaperList();
    } catch (error) {
      this.el.meta.textContent = `状态更新失败：${error.message}`;
    }
  }

  _refreshSelectionState() {
    const available = !!(this.pdf?.getSelection() || this.markdown?.getSelection());
    this.annotations?.setSelectionAvailable(available);
  }

  /** Record an annotation from whatever the user currently has selected. */
  async createAnnotationFromSelection() {
    if (!this.selected) return;
    const source = this.sources.find((s) => s.source_id === this.activeSourceId);
    if (!source) return;

    const isPdf = source.media_kind === 'PDF';
    const selection = isPdf ? this.pdf.getSelection() : this.markdown.getSelection();
    if (!selection?.exact) {
      this.el.meta.textContent = '请先选中一段文本再添加标注';
      return;
    }

    const anchor = isPdf
      ? makePdfAnchor({
          pageIndex: selection.pageIndex,
          selectedText: selection.exact,
          prefix: selection.prefix,
          suffix: selection.suffix,
        })
      : makeMarkdownAnchor({
          headingPath: selection.headingPath,
          selectedText: selection.exact,
          prefix: selection.prefix,
          suffix: selection.suffix,
        });

    try {
      await api.createAnnotation(this.selected.paper_id, {
        source_id: source.source_id,
        kind: 'HIGHLIGHT',
        anchor,
        selected_text: selection.exact,
        source_version: source.source_version,
      });
      await this.annotations.load();
      this._refreshSelectionState();
    } catch (error) {
      this.el.meta.textContent = `标注保存失败：${error.message}`;
    }
  }

  /** Jump to an annotation. P0 supports this without repainting highlights. */
  jumpToAnnotation(item) {
    if (item.anchor_type === 'PDF_TEXT') {
      const page = Number(item.page_index);
      if (Number.isFinite(page)) this.pdf.goToPage(page);
      return;
    }
    if (item.anchor_type === 'MARKDOWN_TEXT') {
      const path = item.heading_path;
      const target = Array.isArray(path) && path.length ? path[path.length - 1] : null;
      if (target) this.markdown.scrollToHeading?.(target);
    }
  }

  /**
   * Build the P1 context envelope locally.
   *
   * Exposed on the instance so the contract can be exercised now; nothing here
   * calls a model or touches the network (ADR-009).
   */
  buildAiContext() {
    if (!this.selected) return null;
    const source = this.sources.find((s) => s.source_id === this.activeSourceId);
    const pdfSelection = this.pdf.getSelection();
    const mdSelection = this.markdown.getSelection();
    const selection = pdfSelection || mdSelection || null;

    return assembleAiContext({
      paper: this.selected,
      focus: source
        ? {
            pane: source.media_kind === 'PDF' ? 'PDF' : 'MARKDOWN',
            sourceId: source.source_id,
            sourceVersion: source.source_version,
            locator:
              source.media_kind === 'PDF'
                ? { page_index: this.pdf.getCurrentPage() }
                : { heading_path: this.markdown.currentHeadingPath() },
            selection,
          }
        : null,
      sourceRefs: this.sources.map((s) => ({
        sourceId: s.source_id,
        role: s.role,
        sha256: s.sha256,
        sourceVersion: s.source_version,
      })),
      note: this.selected.note_id
        ? { noteId: this.selected.note_id, sha256: null, content: this.note.getText() }
        : null,
    });
  }

  _renderSources() {
    const fragment = document.createDocumentFragment();
    for (const source of this.sources) {
      const tab = document.createElement('button');
      tab.type = 'button';
      tab.className = 'paper-source-tab';
      tab.dataset.sourceId = source.source_id;
      if (source.source_id === this.activeSourceId) tab.classList.add('is-active');
      tab.textContent = ROLE_LABELS[source.role] || source.role;
      tab.title = source.rel_path;
      tab.addEventListener('click', () => this.openSource(source.source_id));
      fragment.appendChild(tab);
    }
    this.el.sourceTabs.replaceChildren(fragment);
  }

  async openSource(sourceId) {
    const source = this.sources.find((s) => s.source_id === sourceId);
    if (!source) return;
    this.activeSourceId = sourceId;
    this._renderSources();

    if (source.media_kind === 'PDF') {
      const url = api.sourceContentUrl(sourceId, source.source_version);
      await this.pdf.open(url, {
        sourceId,
        sourceVersion: source.source_version,
      });
      this.el.markdownHost.innerHTML =
        '<p class="paper-muted">切换到翻译来源以查看对照阅读</p>';
    } else {
      try {
        const text = await api.sourceText(sourceId);
        this.markdown.setContent(text, { sourceId });
      } catch (error) {
        this.el.markdownHost.innerHTML = `<p class="paper-error">译文加载失败：${escapeHtml(error.message)}</p>`;
      }
    }
    this._refreshSelectionState();
  }
}

const SHELL_HTML = `
<div class="paper-workbench">
  <header class="paper-toolbar">
    <button type="button" class="paper-icon-btn" data-role="toggle-left" title="折叠/展开论文目录" aria-label="折叠论文目录">☰</button>
    <span class="paper-toolbar-title">论文工作台</span>
    <select class="paper-select" data-role="status-filter" aria-label="按阅读状态筛选">
      <option value="">全部状态</option>
      <option value="UNREAD">未看</option>
      <option value="READING">正在看</option>
      <option value="COMPLETED">看完</option>
    </select>
    <span class="paper-spacer"></span>
    <button type="button" class="paper-status-btn" data-role="status-button">开始阅读</button>
    <span class="paper-version" data-role="pdfjs-version"></span>
    <button type="button" class="paper-icon-btn" data-role="toggle-right" title="折叠/展开笔记栏" aria-label="折叠笔记栏">≡</button>
  </header>

  <div class="paper-grid" data-role="grid">
    <aside class="paper-pane paper-sidebar">
      <div class="paper-pane-head">
        <span>论文目录</span>
        <span class="paper-count" data-role="paper-count">0</span>
      </div>
      <div class="paper-pane-body" data-role="paper-list"></div>
    </aside>

    <div class="paper-splitter" data-splitter="left" role="separator" tabindex="0" aria-label="调整目录宽度"></div>

    <section class="paper-pane paper-reading">
      <div class="paper-pane-head">
        <span data-role="paper-title">未选择论文</span>
        <span class="paper-paper-meta" data-role="paper-meta"></span>
      </div>
      <div class="paper-tabs" data-role="source-tabs"></div>
      <div class="paper-pane-body paper-pdf-host" data-role="pdf-host"></div>
    </section>

    <div class="paper-splitter" data-splitter="center" role="separator" tabindex="0" aria-label="调整原文与翻译比例"></div>

    <section class="paper-pane paper-translation">
      <div class="paper-pane-head"><span>翻译对照</span></div>
      <div class="paper-pane-body" data-role="markdown-host">
        <p class="paper-muted">选择一篇论文以开始阅读</p>
      </div>
    </section>

    <div class="paper-splitter" data-splitter="right" role="separator" tabindex="0" aria-label="调整笔记栏宽度"></div>

    <aside class="paper-pane paper-notes">
      <div class="paper-pane-head"><span>阅读笔记</span></div>
      <div class="paper-pane-body paper-note-host" data-role="note-host"></div>
      <div class="paper-pane-head paper-ann-head"><span>标注</span></div>
      <div class="paper-pane-body paper-ann-host" data-role="annotation-host"></div>
    </aside>
  </div>

  <div class="paper-empty" data-role="empty-state">
    <p>从左侧选择一篇论文开始阅读</p>
  </div>
</div>`;

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  })[ch]);
}

/** Boot when the host page provides a mount point. */
export function mountPaperWorkbench(elementId = 'workspace-paper') {
  const root = document.getElementById(elementId);
  if (!root) return null;
  const workbench = new PaperWorkbench(root);
  workbench.init().catch((error) => {
    console.error('论文工作台初始化失败:', error);
    root.innerHTML = `<p class="paper-error">论文工作台初始化失败：${escapeHtml(error.message)}</p>`;
  });
  return workbench;
}

window.PaperWorkbench = { mountPaperWorkbench, PaperWorkbench, PdfBridge, PDFJS_VERSION };
