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
      btnLeft: this.root.querySelector('[data-role="toggle-left"]'),
      btnRight: this.root.querySelector('[data-role="toggle-right"]'),
      emptyState: this.root.querySelector('[data-role="empty-state"]'),
      viewerVersion: this.root.querySelector('[data-role="pdfjs-version"]'),
    };

    this.layout = new PaperLayout(this.root.querySelector('[data-role="grid"]'));
    this.pdf = new PdfBridge(this.el.pdfHost);

    this.el.viewerVersion.textContent = `PDF.js ${PDFJS_VERSION}`;
    this.el.btnLeft.addEventListener('click', () => this.layout.toggle('left'));
    this.el.btnRight.addEventListener('click', () => this.layout.toggle('right'));
    this.el.statusFilter.addEventListener('change', () => {
      this.statusFilter = this.el.statusFilter.value || null;
      this.loadPapers();
    });

    // Keep the viewer alive across pane resizes; only notify, never reload.
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
    try {
      const [paper, sources] = await Promise.all([
        api.getPaper(paperId),
        api.listSources(paperId),
      ]);
      this.selected = paper;
      this.sources = sources.sources || [];

      this.el.emptyState.hidden = true;
      this.el.title.textContent = paper.title || paper.display_title || '未命名';
      this.el.meta.textContent = [
        STATUS_LABELS[paper.status] || paper.status,
        paper.category_relpath,
        `${this.sources.length} 个来源`,
      ]
        .filter(Boolean)
        .join(' · ');

      this._renderPaperList();
      this._renderSources();

      const primary = this.sources.find((s) => s.media_kind === 'PDF');
      if (primary) await this.openSource(primary.source_id);
    } catch (error) {
      this.el.title.textContent = '加载失败';
      this.el.meta.textContent = error.message;
    }
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
      this.el.markdownHost.innerHTML = `<p class="paper-muted">Markdown 渲染将在后续步骤接入：${escapeHtml(source.rel_path)}</p>`;
    }
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
      <div class="paper-pane-body" data-role="note-host">
        <p class="paper-muted">笔记编辑器将在后续步骤接入</p>
      </div>
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
