/**
 * Jevora IDE — application logic wired to the local backend.
 *
 * Every surface talks to the FastAPI backend served from the same origin:
 * runs (WebSocket streaming with polling fallback), providers & models, agents,
 * workspace files, sandbox terminal (xterm.js), MCP servers, plugins, history,
 * sandbox VMs, attachments, slash commands and authentication.
 */

document.addEventListener('DOMContentLoaded', () => {
  // =========================================================================
  // 0. Utilities & API client
  // =========================================================================
  const $ = (id) => document.getElementById(id);
  const TERMINAL = ['done', 'failed', 'escalated'];
  const TOKEN_KEY = 'jevora_access_token';

  function escapeHtml(value) {
    const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
    return String(value ?? '').replace(/[&<>"']/g, (m) => map[m]);
  }

  function showToast(message) {
    const container = $('toastContainer');
    if (!container) return;
    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transform = 'translateY(10px)';
      toast.style.transition = 'all 0.2s ease';
      setTimeout(() => toast.remove(), 200);
    }, 2800);
  }

  function formatTime(iso) {
    if (!iso) return '';
    const date = new Date(iso);
    const now = new Date();
    const sameDay = date.toDateString() === now.toDateString();
    const time = date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    return sameDay ? `Today, ${time}` : `${date.toLocaleDateString()} ${time}`;
  }

  function inlineMarkdown(text) {
    return text
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>')
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  }

  /** Small, safe markdown renderer (input is escaped first). */
  function renderMarkdown(source) {
    const lines = escapeHtml(source || '').split('\n');
    const out = [];
    let inCode = false;
    let code = [];
    let list = null;
    const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
    for (const line of lines) {
      if (line.trim().startsWith('```')) {
        if (inCode) { out.push(`<pre><code>${code.join('\n')}</code></pre>`); code = []; inCode = false; }
        else { closeList(); inCode = true; }
        continue;
      }
      if (inCode) { code.push(line); continue; }
      const heading = line.match(/^(#{1,3})\s+(.*)$/);
      const bullet = line.match(/^\s*[-*]\s+(.*)$/);
      const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (heading) { closeList(); out.push(`<h${heading[1].length + 1}>${inlineMarkdown(heading[2])}</h${heading[1].length + 1}>`); }
      else if (bullet) { if (list !== 'ul') { closeList(); out.push('<ul>'); list = 'ul'; } out.push(`<li>${inlineMarkdown(bullet[1])}</li>`); }
      else if (numbered) { if (list !== 'ol') { closeList(); out.push('<ol>'); list = 'ol'; } out.push(`<li>${inlineMarkdown(numbered[1])}</li>`); }
      else if (/^-{3,}$/.test(line.trim())) { closeList(); out.push('<hr>'); }
      else if (!line.trim()) { closeList(); }
      else { closeList(); out.push(`<p>${inlineMarkdown(line)}</p>`); }
    }
    if (inCode) out.push(`<pre><code>${code.join('\n')}</code></pre>`);
    closeList();
    return out.join('');
  }

  class ApiError extends Error {
    constructor(status, detail, body) { super(detail || `HTTP ${status}`); this.status = status; this.body = body || {}; }
  }

  const api = {
    token: localStorage.getItem(TOKEN_KEY),
    setToken(token) {
      this.token = token || null;
      if (token) localStorage.setItem(TOKEN_KEY, token); else localStorage.removeItem(TOKEN_KEY);
    },
    async refresh() {
      try {
        const response = await fetch('/api/auth/refresh', { method: 'POST', credentials: 'same-origin' });
        if (!response.ok) { this.setToken(null); return false; }
        const data = await response.json();
        this.setToken(data.access_token);
        return true;
      } catch (_) { return false; }
    },
    async request(method, path, body, options = {}) {
      const headers = {};
      if (this.token) headers.Authorization = `Bearer ${this.token}`;
      let payload = body;
      if (body !== undefined && !(body instanceof FormData)) {
        headers['Content-Type'] = 'application/json';
        payload = JSON.stringify(body);
      }
      const response = await fetch(path, { method, headers, body: payload, credentials: 'same-origin' });
      if (response.status === 401 && !options.retried && !path.startsWith('/api/auth/')) {
        const refreshed = await this.refresh();
        if (!refreshed) this.setToken(null);
        if (refreshed || this.token === null) return this.request(method, path, body, { ...options, retried: true });
      }
      const text = await response.text();
      let data = null;
      try { data = text ? JSON.parse(text) : null; } catch (_) { data = text; }
      if (!response.ok) {
        if (response.status === 401 && data && data.code === 'auth_required') window.location.href = 'auth.html';
        throw new ApiError(response.status, data && data.detail ? data.detail : `HTTP ${response.status}`, data);
      }
      return data;
    },
    get(path) { return this.request('GET', path); },
    post(path, body) { return this.request('POST', path, body ?? {}); },
    put(path, body) { return this.request('PUT', path, body ?? {}); },
    patch(path, body) { return this.request('PATCH', path, body ?? {}); },
    del(path) { return this.request('DELETE', path); },
  };

  function wsUrl(path, params = {}) {
    const url = new URL(path, window.location.href);
    url.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    Object.entries(params).forEach(([k, v]) => { if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v); });
    if (api.token) url.searchParams.set('token', api.token);
    return url.toString();
  }

  // =========================================================================
  // 1. DOM references & state
  // =========================================================================
  const modeDropdownBtn = $('modeDropdownBtn');
  const modeDropdownMenu = $('modeDropdownMenu');
  const modeDropdownLabel = $('modeDropdownLabel');
  const modeDropdownItems = document.querySelectorAll('.mode-dropdown-item');
  const attachToggleBtn = $('attachToggleBtn');
  const attachMenu = $('attachMenu');
  const agentInput = $('agentInput');
  const sendBtn = $('sendBtn');
  const streamContainer = $('streamContainer');
  const activeAttachmentsContainer = $('activeAttachments');
  const chatStageWrapper = $('chatStageWrapper');
  const greetingHeading = $('greetingHeading');
  const greetingSubtitle = $('greetingSubtitle');
  const panelScrollArea = $('panelScrollArea');
  const sidebarToggleBtn = $('sidebarToggleBtn');
  const sidebarColumn = document.querySelector('.sidebar-column');
  const toolRows = document.querySelectorAll('.tool-row');
  const drawerOverlay = $('drawerOverlay');
  const toolDrawer = $('toolDrawer');
  const drawerTitle = $('drawerTitle');
  const drawerBody = $('drawerBody');
  const drawerCloseBtn = $('drawerCloseBtn');
  const commandPopover = $('commandPopover');

  let currentMode = 'build';
  let activeTab = 'build';
  let activeAttachments = []; // { id, filename, kind, uploading }
  let isSidebarOpen = true;
  let conversationId = null;
  let selectedAgent = null; // { id, name }
  let engineName = 'Decision';
  let features = {};
  let currentUser = null;
  let currentDrawer = null;
  let drawerCleanup = null;

  // =========================================================================
  // 2. Auth: profile, sign in / out
  // =========================================================================
  const signInBtn = $('signInBtn');
  const signInWrap = document.querySelector('.sidebar-signin-wrap');
  const profileChip = $('profileChip');
  const profileAvatar = $('profileAvatar');

  if (signInBtn) signInBtn.addEventListener('click', () => { window.location.href = 'auth.html'; });

  function initials(user) {
    const source = (user.name || user.email || 'J').trim();
    return source.split(/\s+/).map((p) => p[0]).join('').slice(0, 2).toUpperCase();
  }

  async function signOut() {
    try { await api.post('/api/auth/logout'); } catch (_) { /* already signed out */ }
    api.setToken(null);
    window.location.href = 'auth.html';
  }

  function renderProfile() {
    if (!currentUser || currentUser.is_local) {
      if (profileChip) profileChip.hidden = true;
      return;
    }
    if (profileChip && profileAvatar) {
      profileChip.hidden = false;
      profileAvatar.textContent = initials(currentUser);
      profileChip.title = `${currentUser.name || ''} ${currentUser.email ? `<${currentUser.email}>` : ''}`.trim();
    }
    if (signInWrap) {
      signInWrap.innerHTML = `
        <div class="sidebar-user">
          <span class="su-avatar">${escapeHtml(initials(currentUser))}</span>
          <div class="su-text"><div class="su-name">${escapeHtml(currentUser.name || 'Signed in')}</div>
          <div class="su-email">${escapeHtml(currentUser.email || '')}</div></div>
          <button class="su-signout" type="button" id="signOutBtn">Sign out</button>
        </div>`;
      $('signOutBtn').addEventListener('click', signOut);
    }
  }

  if (profileChip) {
    profileChip.addEventListener('click', (e) => {
      e.stopPropagation();
      const existing = document.querySelector('.profile-menu');
      if (existing) { existing.remove(); return; }
      const rect = profileChip.getBoundingClientRect();
      const menu = document.createElement('div');
      menu.className = 'profile-menu';
      menu.style.top = `${rect.bottom + 8}px`;
      menu.style.right = `${window.innerWidth - rect.right}px`;
      menu.innerHTML = `<div class="pm-name">${escapeHtml(currentUser?.name || '')}</div>
        <div class="pm-email">${escapeHtml(currentUser?.email || '')}</div>
        <button class="chip-btn" type="button">Sign out</button>`;
      menu.querySelector('button').addEventListener('click', signOut);
      document.body.appendChild(menu);
      setTimeout(() => document.addEventListener('click', () => menu.remove(), { once: true }), 0);
    });
  }

  // =========================================================================
  // 3. Sidebar, tabs, mode dropdown
  // =========================================================================
  if (sidebarToggleBtn) sidebarToggleBtn.classList.add('active');
  if (sidebarToggleBtn && sidebarColumn) {
    sidebarToggleBtn.addEventListener('click', () => {
      isSidebarOpen = !isSidebarOpen;
      sidebarColumn.classList.toggle('collapsed', !isSidebarOpen);
      sidebarToggleBtn.classList.toggle('active', isSidebarOpen);
      showToast(isSidebarOpen ? 'Context & Tools panel opened' : 'Context & Tools panel hidden');
    });
  }

  const leftTabs = document.querySelectorAll('.left-tab');
  const leftTabBuild = $('leftTabBuild');
  const leftTabAgent = $('leftTabAgent');
  const createAgentBtn = $('createAgentBtn');
  const modeDropdownWrapperEl = $('modeDropdownWrapper');

  leftTabs.forEach((tab) => {
    tab.addEventListener('click', () => {
      leftTabs.forEach((t) => t.classList.remove('active'));
      tab.classList.add('active');
      activeTab = tab.dataset.leftTab;
      if (activeTab === 'build') {
        leftTabBuild.classList.add('active');
        leftTabAgent.classList.remove('active');
        if (modeDropdownWrapperEl) modeDropdownWrapperEl.style.display = '';
        if (greetingHeading) greetingHeading.textContent = 'What can I build for you?';
        if (greetingSubtitle) greetingSubtitle.textContent = 'Interact with Jevora IDE and explore the boundless creative world';
        if (agentInput) agentInput.placeholder = selectedAgent ? `Message ${selectedAgent.name}...` : 'How can I help you today?';
      } else {
        leftTabAgent.classList.add('active');
        leftTabBuild.classList.remove('active');
        if (modeDropdownWrapperEl) modeDropdownWrapperEl.style.display = 'none';
        modeDropdownMenu?.classList.remove('open');
        modeDropdownBtn?.classList.remove('open');
        if (greetingHeading) greetingHeading.textContent = 'Create anything you can imagine';
        if (greetingSubtitle) greetingSubtitle.textContent = 'Design custom autonomous agents with specialized tools and unique workflows';
        if (agentInput) agentInput.placeholder = 'Describe the agent you want to create...';
        loadAgents();
      }
    });
  });

  function resetConversation(message) {
    chatStageWrapper?.classList.remove('chat-active');
    if (streamContainer) { streamContainer.innerHTML = ''; streamContainer.style.display = 'none'; }
    if (agentInput) { agentInput.value = ''; agentInput.style.height = 'auto'; agentInput.focus(); }
    conversationId = null;
    if (message) showToast(message);
  }

  document.querySelector('.left-nav-highlight')?.addEventListener('click', () => resetConversation('Started new task'));

  createAgentBtn?.addEventListener('click', () => {
    document.querySelector('.left-tab[data-left-tab="agent"]')?.click();
    agentInput?.focus();
  });

  if (modeDropdownBtn && modeDropdownMenu) {
    modeDropdownBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const isOpen = modeDropdownMenu.classList.toggle('open');
      modeDropdownBtn.classList.toggle('open', isOpen);
      modeDropdownBtn.setAttribute('aria-expanded', String(isOpen));
      attachMenu?.classList.remove('open');
    });
    modeDropdownItems.forEach((item) => {
      item.addEventListener('click', (e) => {
        e.stopPropagation();
        modeDropdownItems.forEach((i) => i.classList.remove('active'));
        item.classList.add('active');
        const selectedMode = item.dataset.mode;
        if (selectedMode !== currentMode) conversationId = null;
        currentMode = selectedMode;
        const modeTitle = item.querySelector('.mode-item-title')?.textContent || selectedMode;
        if (modeDropdownLabel) modeDropdownLabel.textContent = modeTitle;
        modeDropdownMenu.classList.remove('open');
        modeDropdownBtn.classList.remove('open');
        modeDropdownBtn.setAttribute('aria-expanded', 'false');
        showToast(`Switched to ${modeTitle} mode`);
        const placeholders = {
          build: 'Message your agent (e.g. Build a full-stack dashboard)...',
          chat: 'Chat with agent...',
          research: 'Search, summarize and research any topic...',
          analysis: 'Provide files or repository for deep analysis...',
        };
        if (agentInput) agentInput.placeholder = placeholders[currentMode] || agentInput.placeholder;
      });
    });
  }

  // =========================================================================
  // 4. Attach menu: files, photos, web search flag, slash commands
  // =========================================================================
  attachToggleBtn?.addEventListener('click', (e) => {
    e.stopPropagation();
    attachMenu.classList.toggle('open');
    modeDropdownMenu?.classList.remove('open');
    modeDropdownBtn?.classList.remove('open');
  });

  document.addEventListener('click', (e) => {
    if (modeDropdownMenu && !modeDropdownMenu.contains(e.target) && !modeDropdownBtn?.contains(e.target)) {
      modeDropdownMenu.classList.remove('open');
      modeDropdownBtn?.classList.remove('open');
      modeDropdownBtn?.setAttribute('aria-expanded', 'false');
    }
    if (attachMenu && !attachMenu.contains(e.target) && !attachToggleBtn?.contains(e.target)) attachMenu.classList.remove('open');
    if (commandPopover && !commandPopover.hidden && !commandPopover.contains(e.target)) commandPopover.hidden = true;
  });

  function pickFiles(accept, multiple) {
    const input = document.createElement('input');
    input.type = 'file';
    input.multiple = multiple;
    if (accept) input.accept = accept;
    input.onchange = (ev) => { if (ev.target.files?.length) uploadFiles(Array.from(ev.target.files)); };
    input.click();
  }

  async function uploadFiles(files) {
    const placeholders = files.map((f) => ({ id: null, filename: f.name, kind: f.type.startsWith('image/') ? 'image' : 'file', uploading: true }));
    activeAttachments.push(...placeholders);
    renderAttachments();
    const form = new FormData();
    files.forEach((f) => form.append('files', f));
    try {
      const result = await api.request('POST', '/api/attachments', form);
      activeAttachments = activeAttachments.filter((a) => !placeholders.includes(a));
      result.attachments.forEach((att) => activeAttachments.push({ id: att.id, filename: att.filename, kind: att.kind }));
      showToast(`Attached ${result.attachments.map((a) => a.filename).join(', ')}`);
    } catch (error) {
      activeAttachments = activeAttachments.filter((a) => !placeholders.includes(a));
      showToast(`Upload failed: ${error.message}`);
    }
    renderAttachments();
  }

  attachMenu?.querySelectorAll('.attach-menu-item').forEach((item) => {
    item.addEventListener('click', () => {
      const action = item.dataset.action;
      if (item.classList.contains('disabled')) { showToast('Web search is not available yet'); return; }
      attachMenu.classList.remove('open');
      if (action === 'files') pickFiles('', true);
      else if (action === 'photos') pickFiles('image/*', true);
      else if (action === 'commands') openCommandPopover();
      else if (action === 'websearch') {
        if (currentMode !== 'research') document.querySelector('.mode-dropdown-item[data-mode="research"]')?.click();
        showToast('Web search is enabled for Research mode');
      }
    });
  });

  $('webSearchToggleBtn')?.addEventListener('click', () => {
    if (!features.web_search) { showToast('Web search is not available yet'); return; }
    document.querySelector('.mode-dropdown-item[data-mode="research"]')?.click();
  });

  let commandsCache = null;
  async function openCommandPopover() {
    if (!commandPopover) return;
    try { commandsCache = commandsCache || await api.get('/api/commands'); } catch (_) { commandsCache = []; }
    commandPopover.innerHTML = commandsCache.map((c) => `
      <button type="button" data-name="${escapeHtml(c.name)}"><span class="cp-name">${escapeHtml(c.name)} <span style="font-weight:400;color:var(--text-muted)">${escapeHtml(c.args_hint || '')}</span></span>
      <span class="cp-desc">${escapeHtml(c.description)}</span></button>`).join('') || '<div class="drawer-muted" style="padding:8px">No commands available</div>';
    commandPopover.querySelectorAll('button').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        agentInput.value = `${btn.dataset.name} ${agentInput.value.replace(/^\/\S+\s*/, '')}`;
        commandPopover.hidden = true;
        agentInput.focus();
      });
    });
    commandPopover.hidden = false;
  }

  function renderAttachments() {
    activeAttachmentsContainer.innerHTML = '';
    if (selectedAgent && activeTab === 'build') {
      const tag = document.createElement('span');
      tag.className = 'attachment-tag selected-agent-tag';
      tag.innerHTML = `Agent: ${escapeHtml(selectedAgent.name)} <span class="attachment-remove">&times;</span>`;
      tag.querySelector('.attachment-remove').addEventListener('click', (e) => { e.stopPropagation(); selectAgent(null); });
      activeAttachmentsContainer.appendChild(tag);
    }
    activeAttachments.forEach((att, index) => {
      const tag = document.createElement('span');
      tag.className = 'attachment-tag';
      const icon = att.kind === 'image' ? '🖼️' : '📄';
      tag.innerHTML = `${icon} ${escapeHtml(att.filename)}${att.uploading ? ' …' : ''} <span class="attachment-remove" data-index="${index}">&times;</span>`;
      tag.querySelector('.attachment-remove').addEventListener('click', (e) => {
        e.stopPropagation();
        activeAttachments.splice(index, 1);
        renderAttachments();
      });
      activeAttachmentsContainer.appendChild(tag);
    });
  }

  // =========================================================================
  // 5. Sending messages & live run rendering
  // =========================================================================
  function scrollMessagesToBottom() {
    if (panelScrollArea) requestAnimationFrame(() => { panelScrollArea.scrollTop = panelScrollArea.scrollHeight; });
  }

  function ensureChatStage() {
    chatStageWrapper?.classList.add('chat-active');
    if (streamContainer) streamContainer.style.display = 'flex';
  }

  function appendUserBubble(text, attachments) {
    ensureChatStage();
    const bubble = document.createElement('div');
    bubble.className = 'message-bubble user';
    const attachmentMarkup = attachments.length
      ? `<div style="font-size:12px;opacity:0.9;margin-bottom:4px;">Attachments: ${attachments.map((a) => escapeHtml(a.filename)).join(', ')}</div>` : '';
    bubble.innerHTML = `<div class="bubble-content">${attachmentMarkup}<div>${escapeHtml(text || 'Attached context')}</div></div>`;
    streamContainer.appendChild(bubble);
    scrollMessagesToBottom();
  }

  function handleSendMessage() {
    const text = agentInput.value.trim();
    if (activeAttachments.some((a) => a.uploading)) { showToast('Wait for uploads to finish'); return; }
    if (!text && activeAttachments.length === 0) return;
    const attachments = activeAttachments.slice();
    appendUserBubble(text, attachments);
    agentInput.value = '';
    agentInput.style.height = 'auto';
    activeAttachments = [];
    renderAttachments();
    if (commandPopover) commandPopover.hidden = true;
    if (activeTab === 'agent') generateAgent(text);
    else runAgentTask(text || 'Review the attached context', attachments.map((a) => a.id));
  }

  sendBtn.addEventListener('click', handleSendMessage);
  agentInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSendMessage(); }
    if (e.key === 'Escape' && commandPopover) commandPopover.hidden = true;
  });
  agentInput.addEventListener('input', () => {
    agentInput.style.height = 'auto';
    agentInput.style.height = `${Math.min(agentInput.scrollHeight, 150)}px`;
    if (agentInput.value === '/') openCommandPopover();
  });

  document.querySelectorAll('.quick-chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      if (chip.dataset.prompt && agentInput) { agentInput.value = chip.dataset.prompt; handleSendMessage(); }
    });
  });

  class RunView {
    constructor(runId, label) {
      this.runId = runId;
      this.nodes = new Map();
      this.lastSeq = 0;
      this.finished = false;
      this.streamed = '';
      this.el = document.createElement('div');
      this.el.className = 'message-bubble agent';
      this.el.innerHTML = `
        <div class="bubble-header"><span class="tool-dot"></span><span>${escapeHtml(label)}</span></div>
        <div class="bubble-content">
          <div class="run-meta-row">
            <span class="run-status-chip"><span class="pulse"></span><span class="rs-text">queued</span></span>
            <span class="drawer-muted rs-models"></span>
            <button class="run-cancel-btn" type="button">Cancel</button>
          </div>
          <details class="run-tree" open><summary>Agents</summary><div class="tree-root"></div></details>
          <div class="stream-text md-content"></div>
          <div class="final-output md-content"></div>
          <div class="decision-line"></div>
          <div class="escalation-slot"></div>
        </div>`;
      this.statusChip = this.el.querySelector('.run-status-chip');
      this.statusText = this.el.querySelector('.rs-text');
      this.models = this.el.querySelector('.rs-models');
      this.tree = this.el.querySelector('.tree-root');
      this.treeBox = this.el.querySelector('.run-tree');
      this.streamEl = this.el.querySelector('.stream-text');
      this.finalEl = this.el.querySelector('.final-output');
      this.decisionEl = this.el.querySelector('.decision-line');
      this.escalationSlot = this.el.querySelector('.escalation-slot');
      this.cancelBtn = this.el.querySelector('.run-cancel-btn');
      this.cancelBtn.addEventListener('click', () => this.cancel());
      this.treeBox.style.display = 'none';
    }

    setStatus(status) {
      this.statusChip.className = `run-status-chip ${status}`;
      this.statusText.textContent = status.replace('_', ' ');
      const pulse = this.statusChip.querySelector('.pulse');
      if (pulse) pulse.style.display = TERMINAL.includes(status) ? 'none' : '';
      this.cancelBtn.style.display = ['done', 'failed'].includes(status) ? 'none' : '';
    }

    node(callId, parentId, data = {}) {
      if (this.nodes.has(callId)) return this.nodes.get(callId);
      const el = document.createElement('div');
      el.className = 'tree-node';
      el.innerHTML = `<div class="tn-line"><span class="tn-role"></span><span class="tn-model"></span><span class="tn-status running">running</span><span class="tn-decision"></span></div>
        <div class="tn-events"></div><div class="tn-children"></div>`;
      el.querySelector('.tn-role').textContent = data.role || data.agent_name || 'agent';
      const parent = parentId && this.nodes.get(parentId);
      (parent ? parent.querySelector(':scope > .tn-children') : this.tree).appendChild(el);
      this.nodes.set(callId, el);
      this.treeBox.style.display = '';
      return el;
    }

    addEvent(callId, text) {
      const node = this.nodes.get(callId);
      if (!node) return;
      const events = node.querySelector(':scope > .tn-events');
      const line = document.createElement('div');
      line.textContent = text;
      events.appendChild(line);
      while (events.children.length > 6) events.removeChild(events.firstChild);
    }

    handle(event) {
      if (event.seq) this.lastSeq = Math.max(this.lastSeq, event.seq);
      const data = event.data || {};
      switch (event.type) {
        case 'run.status':
          this.setStatus(data.status);
          if (TERMINAL.includes(data.status)) return 'terminal';
          break;
        case 'agent.started':
          this.node(event.call_id, event.parent_call_id, data);
          break;
        case 'agent.finished':
        case 'agent.failed': {
          const node = this.node(event.call_id, event.parent_call_id, data);
          const status = node.querySelector('.tn-status');
          status.textContent = data.status;
          status.className = `tn-status ${data.status}`;
          if (data.model) node.querySelector('.tn-model').textContent = data.model;
          break;
        }
        case 'tool.call':
          if (data.tool === 'terminal') this.addEvent(event.call_id, `$ ${data.command}`);
          else if (data.tool === 'screen') this.addEvent(event.call_id, `screen ${data.action?.action || ''}`);
          else this.addEvent(event.call_id, `→ ${data.tool} ${data.arguments?.path || ''}`.trim());
          break;
        case 'tool.result':
          if (data.ok === false) this.addEvent(event.call_id, `✗ ${data.tool}: ${(data.preview || '').slice(0, 80)}`);
          break;
        case 'test.result':
          this.addEvent(event.call_id, `tests: ${data.passed} passed, ${data.failed} failed${data.errors ? `, ${data.errors} errors` : ''}`);
          break;
        case 'decision.made':
          if (data.acted !== false && event.call_id) {
            const node = this.nodes.get(event.call_id);
            if (node) node.querySelector('.tn-decision').textContent = `⟶ ${data.choice} (${Math.round(data.confidence * 100)}%)`;
          }
          break;
        case 'token.delta':
          this.streamed += data.text || '';
          this.streamEl.textContent = this.streamed;
          scrollMessagesToBottom();
          break;
        case 'token.reset':
          this.streamed = '';
          this.streamEl.textContent = '';
          break;
        case 'escalation.requested':
          this.showEscalation({ id: data.id, kind: data.kind, question: data.question, options: data.options });
          break;
        case 'escalation.resolved':
          this.escalationSlot.innerHTML = '';
          break;
        case 'file.changed':
          if (currentDrawer === 'files') refreshFilesDrawer();
          break;
        default:
          break;
      }
      return null;
    }

    showEscalation(escalation) {
      if (!escalation || !escalation.id) return;
      const labels = { accept: 'Accept result', retry: 'Retry', reassign: 'Reassign (stronger model)', abort: 'Abort',
        approve: 'Approve', deny: 'Deny' };
      this.escalationSlot.innerHTML = `
        <div class="escalation-card">
          <strong>${escalation.kind === 'approval' ? 'Approval needed' : 'Your decision is needed'}</strong>
          <div style="margin-top:4px">${escapeHtml(escalation.question)}</div>
          ${escalation.kind === 'approval' ? '' : '<textarea rows="2" placeholder="Optional guidance for the agent"></textarea>'}
          <div class="esc-actions">${(escalation.options || []).map((o, i) =>
            `<button type="button" class="chip-btn ${i === 0 ? 'primary' : ''} ${o === 'abort' || o === 'deny' ? 'danger' : ''}" data-choice="${escapeHtml(o)}">${escapeHtml(labels[o] || o)}</button>`).join('')}</div>
        </div>`;
      this.escalationSlot.querySelectorAll('button[data-choice]').forEach((btn) => {
        btn.addEventListener('click', async () => {
          const message = this.escalationSlot.querySelector('textarea')?.value || null;
          this.escalationSlot.querySelectorAll('button').forEach((b) => { b.disabled = true; });
          try {
            await api.post(`/api/runs/${encodeURIComponent(this.runId)}/escalation`,
              { choice: btn.dataset.choice, message, escalation_id: escalation.id });
            this.escalationSlot.innerHTML = '';
            showToast(`Answer sent: ${btn.dataset.choice}`);
            if (this.finished) { this.finished = false; this.finalEl.innerHTML = ''; this.decisionEl.textContent = ''; streamRun(this); }
          } catch (error) {
            showToast(`Could not send answer: ${error.message}`);
            this.escalationSlot.querySelectorAll('button').forEach((b) => { b.disabled = false; });
          }
        });
      });
      scrollMessagesToBottom();
    }

    async cancel() {
      try {
        await api.post(`/api/runs/${encodeURIComponent(this.runId)}/cancel`);
        showToast('Run cancelled');
      } catch (error) { showToast(`Cancel failed: ${error.message}`); }
    }

    finalize(run) {
      this.finished = true;
      this.setStatus(run.status);
      this.streamEl.textContent = '';
      this.finalEl.innerHTML = renderMarkdown(run.final_output || 'The run completed without an output.');
      const decision = run.decisions?.at(-1);
      const engine = decision?.engine ? ` · ${decision.engine}` : '';
      this.decisionEl.textContent = `Decision engine: ${decision?.choice || 'pending'} (${Math.round((decision?.confidence || 0) * 100)}% confidence) · ${run.status}${engine}`;
      if (run.models_used?.length) this.models.textContent = run.models_used.join(' · ');
      if (run.status === 'escalated' && run.escalation) this.showEscalation(run.escalation);
      if (!this.nodes.size) this.treeBox.style.display = 'none';
      if (this.nodes.size > 4) this.treeBox.open = false;
      if (run.conversation_id && run.mode === 'chat') conversationId = run.conversation_id;
      scrollMessagesToBottom();
      refreshStatus();
      if (currentDrawer === 'history') renderHistoryDrawer();
    }
  }

  async function waitForRun(runId) {
    for (let attempt = 0; attempt < 3000; attempt++) {
      const run = await api.get(`/api/runs/${encodeURIComponent(runId)}`);
      if (TERMINAL.includes(run.status)) return run;
      await new Promise((resolve) => setTimeout(resolve, 600));
    }
    throw new Error('Run timed out while waiting for the orchestrator');
  }

  function streamRun(view) {
    let retries = 0;
    let closedByUs = false;
    const finish = async () => {
      closedByUs = true;
      try { view.finalize(await api.get(`/api/runs/${encodeURIComponent(view.runId)}`)); }
      catch (error) { view.finalEl.textContent = `Could not load the run: ${error.message}`; }
    };
    const fallback = async () => {
      if (view.finished) return;
      try {
        let run = await api.get(`/api/runs/${encodeURIComponent(view.runId)}`);
        while (!TERMINAL.includes(run.status)) {
          view.setStatus(run.status);
          if (run.status === 'awaiting_user' && run.escalation) view.showEscalation(run.escalation);
          await new Promise((resolve) => setTimeout(resolve, 600));
          run = await api.get(`/api/runs/${encodeURIComponent(view.runId)}`);
        }
        view.finalize(run);
      } catch (error) { view.finalEl.textContent = `Unable to reach the backend: ${error.message}`; }
    };
    const open = () => {
      let ws;
      try { ws = new WebSocket(wsUrl(`/api/runs/${encodeURIComponent(view.runId)}/stream`, { last_seq: view.lastSeq || '' })); }
      catch (_) { fallback(); return; }
      const timer = setTimeout(() => { if (ws.readyState !== WebSocket.OPEN) { closedByUs = true; ws.close(); fallback(); } }, 4000);
      ws.onopen = () => clearTimeout(timer);
      ws.onmessage = (message) => {
        let event;
        try { event = JSON.parse(message.data); } catch (_) { return; }
        if (view.handle(event) === 'terminal') { ws.close(); finish(); }
      };
      ws.onerror = () => {};
      ws.onclose = () => {
        clearTimeout(timer);
        if (closedByUs || view.finished) return;
        retries += 1;
        if (retries <= 3) setTimeout(open, 800 * retries); else fallback();
      };
    };
    open();
  }

  async function runAgentTask(prompt, attachmentIds) {
    const label = selectedAgent ? `${selectedAgent.name} · custom agent` : `IDE Agent (${currentMode.toUpperCase()}) · LangGraph Orchestrator`;
    const placeholder = document.createElement('div');
    placeholder.className = 'message-bubble agent';
    placeholder.innerHTML = `<div class="bubble-header"><span class="tool-dot"></span><span>${escapeHtml(label)}</span></div>
      <div class="bubble-content"><div class="typing-indicator" style="color:var(--text-secondary);"><span style="display:inline-block;animation:pulse 1s infinite;">●</span> Routing to the local agent pool…</div></div>`;
    streamContainer.appendChild(placeholder);
    scrollMessagesToBottom();
    try {
      const body = { text: prompt, mode: currentMode === 'general' ? 'research' : currentMode, model: selectedModel.key,
        attachments: attachmentIds };
      if (selectedAgent) body.agent_id = selectedAgent.id;
      if (conversationId && currentMode === 'chat') body.conversation_id = conversationId;
      const created = await api.post('/api/runs', body);
      const view = new RunView(created.id, label);
      placeholder.replaceWith(view.el);
      view.setStatus(created.status);
      streamRun(view);
    } catch (error) {
      placeholder.querySelector('.bubble-content').innerHTML = `<p>${escapeHtml(error.status === 422 || error.status === 413 ? error.message : 'Unable to reach the local backend. Start it with')} ${error.status ? '' : '<code>python -m uvicorn backend.main:app --reload</code>.'}</p><p style="font-size:12px;color:var(--text-secondary);">${escapeHtml(error.message)}</p>`;
      scrollMessagesToBottom();
    }
  }

  // =========================================================================
  // 6. Agent tab: saved agents + natural-language generation
  // =========================================================================
  const agentList = $('agentList');
  const agentEmptyState = $('agentEmptyState');

  function selectAgent(agent) {
    selectedAgent = agent;
    renderAttachments();
    if (agent) {
      showToast(`Messages now go to ${agent.name}`);
      document.querySelector('.left-tab[data-left-tab="build"]')?.click();
    }
    loadAgents();
  }

  async function loadAgents() {
    if (!agentList) return;
    let agents = [];
    try { agents = await api.get('/api/agents'); } catch (_) { return; }
    agentList.hidden = agents.length === 0;
    if (agentEmptyState) agentEmptyState.style.display = agents.length ? 'none' : '';
    agentList.innerHTML = '';
    agents.forEach((agent) => {
      const card = document.createElement('div');
      card.className = `agent-card${selectedAgent?.id === agent.id ? ' selected' : ''}`;
      card.innerHTML = `<div class="ac-top"><span class="ac-dot ${agent.status === 'running' ? 'running' : ''}"></span>
        <span class="ac-name">${escapeHtml(agent.name)}</span><span class="drawer-muted">${escapeHtml(agent.template || 'custom')}</span></div>
        <div class="ac-desc">${escapeHtml(agent.description || agent.config?.system_prompt || '')}</div>
        <div class="ac-actions"><button class="chip-btn primary" type="button" data-act="use">Use</button>
        <button class="chip-btn danger" type="button" data-act="delete">Delete</button></div>`;
      card.querySelector('[data-act="use"]').addEventListener('click', (e) => { e.stopPropagation(); selectAgent({ id: agent.id, name: agent.name }); });
      card.querySelector('[data-act="delete"]').addEventListener('click', async (e) => {
        e.stopPropagation();
        try {
          await api.del(`/api/agents/${encodeURIComponent(agent.id)}`);
          if (selectedAgent?.id === agent.id) selectedAgent = null;
          renderAttachments();
          loadAgents();
        } catch (error) { showToast(`Delete failed: ${error.message}`); }
      });
      card.addEventListener('click', () => showAgentDetails(agent));
      agentList.appendChild(card);
    });
  }

  function showAgentDetails(agent) {
    openDrawer(`Agent · ${agent.name}`, 'agent');
    const cfg = agent.config || {};
    drawerBody.innerHTML = `<div style="display:flex;flex-direction:column;gap:12px;">
      <div class="drawer-card"><strong>${escapeHtml(agent.name)}</strong><div class="drawer-muted">${escapeHtml(agent.description || '')}</div>
      <div class="drawer-muted" style="margin-top:6px">Status: ${escapeHtml(agent.status)} · generated by ${escapeHtml(agent.generated_by)}</div></div>
      <div class="drawer-card"><div class="drawer-section-label">TOOLS</div><div class="drawer-muted">${escapeHtml((cfg.tool_permissions || []).join(', '))}</div>
      <div class="drawer-section-label" style="margin-top:8px">MODEL</div><div class="drawer-muted">${escapeHtml(cfg.model_binding?.mode === 'explicit' ? cfg.model_binding.model : `Auto (${cfg.model_binding?.category || 'decision engine'})`)}</div>
      <div class="drawer-section-label" style="margin-top:8px">MEMORY</div><div class="drawer-muted">${escapeHtml(cfg.memory_scope)}</div></div>
      <div class="drawer-card"><div class="drawer-section-label">SYSTEM PROMPT</div><div class="md-content" style="font-size:12.5px">${renderMarkdown(cfg.system_prompt || '')}</div></div>
      <button class="chip-btn primary" type="button" id="useAgentBtn">Use this agent</button></div>`;
    $('useAgentBtn').addEventListener('click', () => { closeDrawer(); selectAgent({ id: agent.id, name: agent.name }); });
  }

  async function generateAgent(description) {
    ensureChatStage();
    const bubble = document.createElement('div');
    bubble.className = 'message-bubble agent';
    bubble.innerHTML = `<div class="bubble-header"><span class="tool-dot"></span><span>Agent Builder</span></div>
      <div class="bubble-content"><span style="display:inline-block;animation:pulse 1s infinite;">●</span> Designing your agent…</div>`;
    streamContainer.appendChild(bubble);
    scrollMessagesToBottom();
    try {
      const agent = await api.post('/api/agents/generate', { description });
      const cfg = agent.config || {};
      bubble.querySelector('.bubble-content').innerHTML = `<p>Created agent <strong>${escapeHtml(agent.name)}</strong>${agent.generated_by === 'heuristic' ? ' (from a template — add a model provider for AI-designed agents)' : ''}.</p>
        <p class="drawer-muted">Tools: ${escapeHtml((cfg.tool_permissions || []).join(', '))} · Memory: ${escapeHtml(cfg.memory_scope)}</p>
        <div class="esc-actions" style="display:flex;gap:8px;margin-top:8px"><button class="chip-btn primary" type="button">Use this agent</button></div>`;
      bubble.querySelector('button').addEventListener('click', () => selectAgent({ id: agent.id, name: agent.name }));
      loadAgents();
    } catch (error) {
      bubble.querySelector('.bubble-content').innerHTML = `<p>Could not create the agent: ${escapeHtml(error.message)}</p>`;
    }
    scrollMessagesToBottom();
  }

  // =========================================================================
  // 7. Drawers: files, terminal, MCP, plugins, history, VMs
  // =========================================================================
  function openDrawer(title, kind) {
    if (drawerCleanup) { drawerCleanup(); drawerCleanup = null; }
    currentDrawer = kind;
    drawerTitle.textContent = title;
    drawerBody.innerHTML = '<div class="drawer-muted">Loading…</div>';
    drawerOverlay.classList.add('active');
    toolDrawer.classList.add('open');
    toolDrawer.setAttribute('aria-hidden', 'false');
  }

  function closeDrawer() {
    if (drawerCleanup) { drawerCleanup(); drawerCleanup = null; }
    currentDrawer = null;
    drawerOverlay.classList.remove('active');
    toolDrawer.classList.remove('open');
    toolDrawer.setAttribute('aria-hidden', 'true');
  }

  drawerCloseBtn.addEventListener('click', closeDrawer);
  drawerOverlay.addEventListener('click', closeDrawer);

  toolRows.forEach((row) => {
    row.addEventListener('click', () => {
      const tool = row.dataset.tool;
      if (tool === 'files') renderFilesDrawer();
      else if (tool === 'terminal') renderTerminalDrawer();
      else if (tool === 'mcp') renderMcpDrawer();
      else if (tool === 'plugins') renderPluginsDrawer();
    });
  });
  $('leftPluginsBtn')?.addEventListener('click', renderPluginsDrawer);
  $('historyBtn')?.addEventListener('click', () => renderHistoryDrawer());
  $('sidebarSearchBtn')?.addEventListener('click', () => renderHistoryDrawer(true));
  $('vmPill')?.addEventListener('click', renderVmsDrawer);

  // ---- File preview -------------------------------------------------------
  let recentFiles = new Set();
  let filesTimer = null;

  function treeMarkup(node) {
    if (node.type === 'file') {
      const recent = recentFiles.has(node.path) ? ' recent' : '';
      return `<div class="ft-item${recent}" data-path="${escapeHtml(node.path)}" title="${escapeHtml(node.path)}">📄 ${escapeHtml(node.name)}</div>`;
    }
    const children = (node.children || []).map(treeMarkup).join('');
    return `<details ${node.path === '.' ? 'open' : ''}><summary class="ft-item">📁 ${escapeHtml(node.name || 'workspace')}</summary><div class="ft-children">${children}</div></details>`;
  }

  async function renderFilesDrawer() {
    if (currentDrawer !== 'files') openDrawer('File Preview', 'files');
    let tree;
    let changes = { items: [] };
    try {
      [tree, changes] = await Promise.all([api.get('/api/workspace/tree'), api.get('/api/workspace/changes?limit=30')]);
    } catch (error) { drawerBody.innerHTML = `<div class="drawer-card">Could not load the workspace: ${escapeHtml(error.message)}</div>`; return; }
    changes.items.forEach((c) => recentFiles.add(c.path));
    const empty = !(tree.children || []).length;
    drawerBody.innerHTML = `
      <div style="display:flex;flex-direction:column;gap:14px;">
        <div class="drawer-row-between"><span class="drawer-section-label">PROJECT FILES</span>
          <button class="chip-btn" type="button" id="filesRefresh">Refresh</button></div>
        ${empty ? `<div style="background:var(--bg-surface-alt);border:1px solid var(--border-card);border-radius:12px;padding:28px 16px;text-align:center;color:var(--text-secondary);">
            <div style="font-size:24px;margin-bottom:8px;opacity:0.6;">📁</div><strong style="display:block;font-size:13.5px;color:var(--text-primary);margin-bottom:4px;">No files created yet</strong>
            <p style="font-size:12.5px;color:var(--text-muted);max-width:280px;margin:0 auto;line-height:1.5;">Files created or modified by your agents will appear here automatically.</p></div>`
          : `<div class="file-tree">${(tree.children || []).map(treeMarkup).join('')}</div>`}
        <div class="file-viewer" id="fileViewer"></div>
        <div><div class="drawer-section-label">RECENT CHANGES</div>
          <div id="recentChanges" style="display:flex;flex-direction:column;gap:4px;margin-top:6px;">${changes.items.slice().reverse().slice(0, 12).map((c) =>
            `<div class="drawer-muted">${escapeHtml(c.change)} · <a href="#" data-path="${escapeHtml(c.path)}">${escapeHtml(c.path)}</a>${c.run_id ? ' · by agent' : ''}</div>`).join('') || '<div class="drawer-muted">No changes yet.</div>'}</div></div>
      </div>`;
    $('filesRefresh').addEventListener('click', renderFilesDrawer);
    drawerBody.querySelectorAll('[data-path]').forEach((el) => el.addEventListener('click', (e) => { e.preventDefault(); openFile(el.dataset.path); }));
  }

  function refreshFilesDrawer() {
    clearTimeout(filesTimer);
    filesTimer = setTimeout(() => { if (currentDrawer === 'files' && !$('fileEditor')) renderFilesDrawer(); }, 500);
  }

  async function openFile(path) {
    const viewer = $('fileViewer');
    if (!viewer) return;
    try {
      const file = await api.get(`/api/workspace/file?path=${encodeURIComponent(path)}`);
      if (file.binary) {
        viewer.innerHTML = `<div class="drawer-card"><strong>${escapeHtml(file.path)}</strong><div class="drawer-muted">Binary file · ${file.size} bytes · <a href="/api/workspace/file?raw=true&path=${encodeURIComponent(file.path)}" target="_blank">download</a></div></div>`;
        return;
      }
      viewer.innerHTML = `<div class="drawer-row-between" style="margin-bottom:6px"><span class="drawer-muted"><strong>${escapeHtml(file.path)}</strong> · ${escapeHtml(file.language)} · ${file.size} bytes${file.truncated ? ' · truncated' : ''}</span>
        <button class="chip-btn" type="button" id="editFileBtn">Edit</button></div><pre></pre>`;
      viewer.querySelector('pre').textContent = file.content;
      $('editFileBtn').addEventListener('click', () => {
        viewer.innerHTML = `<div class="drawer-muted" style="margin-bottom:6px"><strong>${escapeHtml(file.path)}</strong></div>
          <textarea class="drawer-textarea" id="fileEditor"></textarea>
          <div style="display:flex;gap:8px;margin-top:8px"><button class="chip-btn primary" type="button" id="saveFileBtn">Save</button><button class="chip-btn" type="button" id="cancelEditBtn">Cancel</button></div>`;
        $('fileEditor').value = file.content;
        $('cancelEditBtn').addEventListener('click', () => openFile(path));
        $('saveFileBtn').addEventListener('click', async () => {
          try {
            await api.put('/api/workspace/file', { path: file.path, content: $('fileEditor').value });
            showToast(`Saved ${file.path}`);
            openFile(path);
          } catch (error) { showToast(`Save failed: ${error.message}`); }
        });
      });
    } catch (error) { viewer.innerHTML = `<div class="drawer-card">Could not open ${escapeHtml(path)}: ${escapeHtml(error.message)}</div>`; }
  }

  // ---- Terminal (xterm.js) --------------------------------------------------
  function loadScript(src) {
    return new Promise((resolve, reject) => {
      if (document.querySelector(`script[src="${src}"]`)) { resolve(); return; }
      const script = document.createElement('script');
      script.src = src; script.onload = resolve; script.onerror = () => reject(new Error(`could not load ${src}`));
      document.head.appendChild(script);
    });
  }

  async function loadXterm() {
    if (!document.querySelector('link[data-xterm]')) {
      const link = document.createElement('link');
      link.rel = 'stylesheet'; link.dataset.xterm = '1';
      link.href = 'https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.min.css';
      document.head.appendChild(link);
    }
    await loadScript('https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.min.js');
    await loadScript('https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.min.js');
  }

  async function renderTerminalDrawer() {
    openDrawer('System Terminal', 'terminal');
    drawerBody.innerHTML = `<div style="display:flex;flex-direction:column;gap:12px;">
      <div class="drawer-row-between"><span class="drawer-section-label">SANDBOX SHELL</span><span class="drawer-muted" id="termInfo">connecting…</span></div>
      <div class="terminal-host" id="terminalHost"></div>
      <div class="drawer-muted">Commands run inside the project sandbox (the same environment agents use).</div></div>`;
    try { await loadXterm(); } catch (error) {
      $('terminalHost').innerHTML = `<div style="color:#F87171;padding:12px;font-family:var(--font-mono)">${escapeHtml(error.message)}</div>`;
      return;
    }
    if (currentDrawer !== 'terminal') return;
    const term = new window.Terminal({ fontFamily: "'JetBrains Mono', monospace", fontSize: 13, cursorBlink: true,
      theme: { background: '#0F172A', foreground: '#E2E8F0' } });
    const fit = new window.FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open($('terminalHost'));
    fit.fit();
    const ws = new WebSocket(wsUrl('/api/terminal', { cols: term.cols, rows: term.rows }));
    ws.onmessage = (message) => {
      const data = JSON.parse(message.data);
      if (data.type === 'stdout') term.write(data.data);
      else if (data.type === 'ready') { const info = $('termInfo'); if (info) info.textContent = `${data.sandbox.kind} · ${data.sandbox.isolation || data.sandbox.image || ''}`; }
      else if (data.type === 'exit') term.write('\r\n[process exited]\r\n');
      else if (data.type === 'error') term.write(`\r\n\x1b[31m${data.message}\x1b[0m\r\n`);
    };
    ws.onclose = () => { const info = $('termInfo'); if (info) info.textContent = 'disconnected'; };
    const dataSub = term.onData((data) => { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'stdin', data })); });
    const observer = new ResizeObserver(() => {
      try { fit.fit(); } catch (_) { return; }
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }));
    });
    observer.observe($('terminalHost'));
    term.focus();
    drawerCleanup = () => { observer.disconnect(); dataSub.dispose(); ws.close(); term.dispose(); };
  }

  // ---- MCP ----------------------------------------------------------------
  async function renderMcpDrawer() {
    if (currentDrawer !== 'mcp') openDrawer('Model Context Protocol (MCP)', 'mcp');
    let health;
    let servers = [];
    try { [health, servers] = await Promise.all([api.get('/api/system/health?probe=false'), api.get('/api/mcp/servers')]); }
    catch (error) { drawerBody.innerHTML = `<div class="drawer-card">Could not load MCP status: ${escapeHtml(error.message)}</div>`; return; }
    const builtin = health.mcp?.builtin || [];
    const engine = health.decision_engine?.active || engineName;
    const descriptions = ['Model assignment & zero-hallucination critic', 'Multi-agent state graph & artifact handoff',
      'Native file I/O, process execution & testing'];
    const statusOf = (ok, label) => `<span class="${ok ? 'status-text-ok' : 'status-text-bad'}">${escapeHtml(label || (ok ? 'Active' : 'Unavailable'))}</span>`;
    drawerBody.innerHTML = `<div style="display:flex;flex-direction:column;gap:14px;">
      <p style="color:var(--text-secondary);font-size:13px;">Connected MCP servers & system tool providers:</p>
      <div style="display:flex;flex-direction:column;gap:10px;">${builtin.map((b, i) => `
        <div class="drawer-card drawer-row-between"><div><strong>${escapeHtml(i === 0 ? `${engine} Decision & Verifier Engine` : b.name)}</strong>
        <div style="font-size:11.5px;color:var(--text-secondary);">${escapeHtml(i === 2 && b.kind ? `${descriptions[i]} · ${b.kind}${b.local_unsafe_exec && b.kind === 'local' ? ' (no isolation)' : ''}` : descriptions[i] || '')}</div></div>
        ${statusOf(b.ok !== false && b.available !== false, i === 0 ? (b.primary?.mode === 'shadow' ? `Active · ${engine}` : 'Active') : null)}</div>`).join('')}</div>
      <div class="drawer-section-label">YOUR MCP SERVERS</div>
      <div style="display:flex;flex-direction:column;gap:10px;" id="mcpServers">${servers.map((s) => `
        <div class="drawer-card" data-id="${escapeHtml(s.id)}"><div class="drawer-row-between"><strong>${escapeHtml(s.name)}</strong>
          <span class="${s.status === 'connected' ? 'status-text-ok' : s.status === 'error' ? 'status-text-bad' : 'status-text-warn'}">${escapeHtml(s.status)}</span></div>
          <div class="drawer-muted">${escapeHtml(s.transport)} · ${escapeHtml(s.transport === 'stdio' ? `${s.command} ${(s.args || []).join(' ')}` : s.url)}</div>
          ${s.tools?.length ? `<div class="drawer-muted">Tools: ${s.tools.map(escapeHtml).join(', ')}</div>` : ''}
          ${s.error ? `<div class="status-text-bad" style="font-weight:400">${escapeHtml(s.error)}</div>` : ''}
          <div style="display:flex;gap:6px;margin-top:8px"><label class="drawer-muted" style="display:flex;align-items:center;gap:6px"><input type="checkbox" class="toggle-input" data-act="toggle" ${s.enabled ? 'checked' : ''}> Enabled</label>
          <button class="chip-btn" type="button" data-act="health">Check</button><button class="chip-btn danger" type="button" data-act="delete">Remove</button></div></div>`).join('') || '<div class="drawer-muted">No MCP servers yet.</div>'}</div>
      <details class="drawer-card"><summary style="cursor:pointer;font-weight:600">Add MCP server</summary>
        <div style="display:flex;flex-direction:column;gap:8px;margin-top:10px">
          <input class="drawer-input" id="mcpName" placeholder="Name (e.g. filesystem)">
          <select class="drawer-select" id="mcpTransport"><option value="stdio">stdio (local command)</option><option value="http">Streamable HTTP</option><option value="sse">SSE</option></select>
          <input class="drawer-input" id="mcpCommand" placeholder="Command (e.g. npx)">
          <input class="drawer-input" id="mcpArgs" placeholder="Arguments (space separated)">
          <input class="drawer-input" id="mcpUrl" placeholder="URL (http/sse)" style="display:none">
          <button class="chip-btn primary" type="button" id="mcpAdd">Add server</button></div></details></div>`;
    $('mcpTransport').addEventListener('change', (e) => {
      const stdio = e.target.value === 'stdio';
      $('mcpCommand').style.display = stdio ? '' : 'none';
      $('mcpArgs').style.display = stdio ? '' : 'none';
      $('mcpUrl').style.display = stdio ? 'none' : '';
    });
    $('mcpAdd').addEventListener('click', async () => {
      const transport = $('mcpTransport').value;
      const body = { name: $('mcpName').value.trim(), transport };
      if (transport === 'stdio') { body.command = $('mcpCommand').value.trim(); body.args = $('mcpArgs').value.trim().split(/\s+/).filter(Boolean); }
      else body.url = $('mcpUrl').value.trim();
      try { const s = await api.post('/api/mcp/servers', body); showToast(`${s.name}: ${s.status}`); renderMcpDrawer(); }
      catch (error) { showToast(error.message); }
    });
    drawerBody.querySelectorAll('#mcpServers [data-id]').forEach((card) => {
      const id = card.dataset.id;
      card.querySelector('[data-act="toggle"]').addEventListener('change', async (e) => {
        try { await api.patch(`/api/mcp/servers/${id}`, { enabled: e.target.checked }); renderMcpDrawer(); } catch (error) { showToast(error.message); }
      });
      card.querySelector('[data-act="health"]').addEventListener('click', async () => {
        try { const h = await api.get(`/api/mcp/servers/${id}/health`); showToast(h.ok ? `Healthy · ${h.latency_ms} ms` : `Unhealthy: ${h.error || h.status}`); }
        catch (error) { showToast(error.message); }
      });
      card.querySelector('[data-act="delete"]').addEventListener('click', async () => {
        try { await api.del(`/api/mcp/servers/${id}`); renderMcpDrawer(); } catch (error) { showToast(error.message); }
      });
    });
  }

  // ---- Plugins ----------------------------------------------------------------
  async function renderPluginsDrawer() {
    openDrawer('Active Plugins & Modules', 'plugins');
    let plugins;
    try { plugins = await api.get('/api/plugins'); } catch (error) { drawerBody.innerHTML = `<div class="drawer-card">${escapeHtml(error.message)}</div>`; return; }
    const names = { decision_verification: `${engineName} Anti-Hallucination Protocol` };
    drawerBody.innerHTML = `<div style="display:flex;flex-direction:column;gap:12px;">${plugins.map((p) => `
      <div class="drawer-card drawer-row-between"><div><strong>${escapeHtml(names[p.id] ? `${p.name}` : p.name)}</strong>
        <div style="font-size:11px;color:var(--text-secondary);">${escapeHtml(p.description)}</div>
        ${p.builtin ? '' : `<div class="drawer-muted">v${escapeHtml(p.version)} · ${escapeHtml(p.source)}${p.error ? ` · <span class="status-text-bad">${escapeHtml(p.error)}</span>` : ''}</div>`}</div>
        <input type="checkbox" class="toggle-input" data-id="${escapeHtml(p.id)}" ${p.enabled ? 'checked' : ''} /></div>`).join('')}</div>`;
    drawerBody.querySelectorAll('input[data-id]').forEach((input) => {
      input.addEventListener('change', async () => {
        try {
          const updated = await api.patch(`/api/plugins/${encodeURIComponent(input.dataset.id)}`, { enabled: input.checked });
          showToast(`${updated.name} ${updated.enabled ? 'enabled' : 'disabled'}`);
        } catch (error) { input.checked = !input.checked; showToast(error.message); }
      });
    });
  }

  // ---- History --------------------------------------------------------------
  let historyCursor = null;
  async function renderHistoryDrawer(focusSearch = false, append = false) {
    if (!append) {
      openDrawer('Agent Session History', 'history');
      drawerBody.innerHTML = `<div style="display:flex;flex-direction:column;gap:10px;">
        <div style="display:flex;gap:8px"><input class="drawer-input" id="historyQuery" placeholder="Search runs…">
        <select class="drawer-select" id="historyMode" style="width:130px"><option value="">All modes</option><option value="build">Build</option><option value="chat">Chat</option><option value="research">Research</option><option value="analysis">Analysis</option></select></div>
        <div id="historyList" style="display:flex;flex-direction:column;gap:10px"></div>
        <button class="chip-btn" type="button" id="historyMore" style="display:none">Load more</button></div>`;
      let debounce;
      $('historyQuery').addEventListener('input', () => { clearTimeout(debounce); debounce = setTimeout(() => loadHistory(false), 250); });
      $('historyMode').addEventListener('change', () => loadHistory(false));
      $('historyMore').addEventListener('click', () => loadHistory(true));
      if (focusSearch) $('historyQuery').focus();
    }
    await loadHistory(append);
  }

  async function loadHistory(append) {
    const list = $('historyList');
    if (!list) return;
    const params = new URLSearchParams({ limit: '20' });
    if ($('historyQuery')?.value) params.set('q', $('historyQuery').value);
    if ($('historyMode')?.value) params.set('mode', $('historyMode').value);
    if (append && historyCursor) params.set('cursor', historyCursor);
    let page;
    try { page = await api.get(`/api/runs?${params}`); } catch (error) { list.innerHTML = `<div class="drawer-card">${escapeHtml(error.message)}</div>`; return; }
    historyCursor = page.next_cursor;
    if (!append) list.innerHTML = '';
    if (!page.items.length && !append) list.innerHTML = '<div class="drawer-muted">No runs yet. Your agent sessions will appear here.</div>';
    page.items.forEach((run) => {
      const card = document.createElement('div');
      card.className = 'drawer-card clickable';
      const verdict = run.verdict ? ` · ${escapeHtml(run.verdict.choice)} (${Math.round(run.verdict.confidence * 100)}%, ${escapeHtml(run.verdict.engine)})` : '';
      card.innerHTML = `<div style="font-size:11px;color:var(--text-secondary);">${escapeHtml(formatTime(run.created_at))}</div>
        <strong style="display:block;margin-top:2px;">${escapeHtml(run.title || run.task)}</strong>
        <div style="font-size:12px;color:var(--text-secondary);margin-top:4px;">Mode: ${escapeHtml(run.mode)} · ${escapeHtml(run.status)}${verdict}</div>`;
      card.addEventListener('click', () => showRunDetails(run.id));
      list.appendChild(card);
    });
    const more = $('historyMore');
    if (more) more.style.display = historyCursor ? '' : 'none';
  }

  function treeHtml(nodes) {
    return nodes.map((n) => `<div class="tree-node"><div class="tn-line"><span class="tn-role">${escapeHtml(n.role)}</span>
      <span class="tn-model">${escapeHtml(n.model_used || '')}</span><span class="tn-status ${escapeHtml(n.status)}">${escapeHtml(n.status)}</span>
      ${n.duration_ms != null ? `<span class="drawer-muted">${(n.duration_ms / 1000).toFixed(1)}s · ${n.tokens} tok</span>` : ''}
      ${(n.decisions || []).filter((d) => d.acted).map((d) => `<span class="tn-decision">⟶ ${escapeHtml(d.choice)} (${Math.round(d.confidence * 100)}%)</span>`).join('')}</div>
      ${n.children?.length ? `<div class="tn-children">${treeHtml(n.children)}</div>` : ''}</div>`).join('');
  }

  async function showRunDetails(runId) {
    drawerBody.innerHTML = '<div class="drawer-muted">Loading…</div>';
    let run;
    let tree;
    try { [run, tree] = await Promise.all([api.get(`/api/runs/${runId}`), api.get(`/api/runs/${runId}/tree`)]); }
    catch (error) { drawerBody.innerHTML = `<div class="drawer-card">${escapeHtml(error.message)}</div>`; return; }
    drawerBody.innerHTML = `<div style="display:flex;flex-direction:column;gap:12px;">
      <button class="chip-btn" type="button" id="historyBack" style="align-self:flex-start">← Back</button>
      <div class="drawer-card"><div style="font-size:11px;color:var(--text-secondary);">${escapeHtml(formatTime(run.created_at))} · ${escapeHtml(run.mode)} · ${escapeHtml(run.status)}</div>
        <strong style="display:block;margin-top:4px">${escapeHtml(run.task)}</strong>
        <div class="drawer-muted" style="margin-top:4px">${run.total_tokens} tokens · $${(run.cost_estimate || 0).toFixed(4)}${run.vault_note_path ? ` · vault: ${escapeHtml(run.vault_note_path)}` : ''}</div></div>
      <div class="drawer-card"><div class="drawer-section-label">AGENT TREE</div>${treeHtml(tree.roots) || '<div class="drawer-muted">No agent calls.</div>'}</div>
      <div class="drawer-card md-content">${renderMarkdown(run.final_output || '')}</div>
      <div style="display:flex;gap:8px">${run.status === 'failed' && !run.cancelled ? '<button class="chip-btn" type="button" id="historyResume">Resume</button>' : ''}
        <button class="chip-btn danger" type="button" id="historyDelete">Delete</button></div></div>`;
    $('historyBack').addEventListener('click', () => renderHistoryDrawer());
    $('historyDelete').addEventListener('click', async () => {
      try { await api.del(`/api/runs/${runId}`); showToast('Run deleted'); renderHistoryDrawer(); } catch (error) { showToast(error.message); }
    });
    $('historyResume')?.addEventListener('click', async () => {
      try { await api.post(`/api/runs/${runId}/resume`); showToast('Run resumed'); closeDrawer(); } catch (error) { showToast(error.message); }
    });
  }

  // ---- VMs ------------------------------------------------------------------
  async function renderVmsDrawer() {
    if (currentDrawer !== 'vms') openDrawer('Sandbox Environments (VMs)', 'vms');
    let data;
    try { data = await api.get('/api/vms'); } catch (error) { drawerBody.innerHTML = `<div class="drawer-card">${escapeHtml(error.message)}</div>`; return; }
    const sandbox = data.sandbox || {};
    drawerBody.innerHTML = `<div style="display:flex;flex-direction:column;gap:12px;">
      <div class="drawer-card"><strong>Sandbox: ${escapeHtml(sandbox.kind || 'unavailable')}</strong>
        <div class="drawer-muted">${sandbox.available ? `${escapeHtml(sandbox.image || '')} · runtime ${escapeHtml(sandbox.runtime || 'runc')} · network ${sandbox.network_default ? 'on' : 'off'}` : escapeHtml(sandbox.reason || '')}</div></div>
      ${data.vms.map((vm) => `<div class="drawer-card" data-id="${escapeHtml(vm.id)}"><div class="drawer-row-between"><strong>${escapeHtml(vm.name)}</strong>
        <span class="${vm.status === 'running' || vm.status === 'ready' ? 'status-text-ok' : vm.status === 'error' || vm.status === 'unavailable' ? 'status-text-bad' : 'status-text-warn'}">${escapeHtml(vm.status)}</span></div>
        <div class="drawer-muted">${escapeHtml(vm.kind)}${vm.selected ? ' · selected for runs & terminal' : ''}</div>
        <div style="display:flex;gap:6px;margin-top:8px">${vm.selected ? '' : '<button class="chip-btn primary" type="button" data-act="select">Select</button>'}
        ${vm.id !== 'default' ? `${vm.status === 'running' ? '<button class="chip-btn" type="button" data-act="stop">Stop</button>' : '<button class="chip-btn" type="button" data-act="start">Start</button>'}
        <button class="chip-btn danger" type="button" data-act="delete">Delete</button>` : ''}</div></div>`).join('')}
      <div style="display:flex;gap:8px"><input class="drawer-input" id="vmName" placeholder="New environment name"><button class="chip-btn primary" type="button" id="vmCreate">Create</button></div></div>`;
    $('vmCreate').addEventListener('click', async () => {
      try { await api.post('/api/vms', { name: $('vmName').value || 'Sandbox' }); renderVmsDrawer(); refreshStatus(); } catch (error) { showToast(error.message); }
    });
    drawerBody.querySelectorAll('[data-id] [data-act]').forEach((btn) => {
      const id = btn.closest('[data-id]').dataset.id;
      btn.addEventListener('click', async () => {
        const act = btn.dataset.act;
        try {
          if (act === 'delete') await api.del(`/api/vms/${id}`); else await api.post(`/api/vms/${id}/${act}`);
          renderVmsDrawer(); refreshStatus();
        } catch (error) { showToast(error.message); }
      });
    });
  }

  // =========================================================================
  // 8. Status cluster + global events
  // =========================================================================
  function applyStatus(status) {
    const badge = $('agentCountBadge');
    const label = $('agentCountLabel');
    const total = (status.agents_running || 0) + (status.subagents_running || 0);
    if (badge) { badge.textContent = String(total); badge.classList.toggle('busy', total > 0); }
    if (label) label.textContent = `${status.agents_running || 0} agent${status.agents_running === 1 ? '' : 's'} / ${status.subagents_running || 0} subagent${status.subagents_running === 1 ? '' : 's'} running`;
    if (status.vms !== undefined) {
      const vmLabel = $('vmPillLabel');
      if (vmLabel) vmLabel.textContent = `VMs · ${status.vms}`;
      const dot = $('vmDot');
      if (dot) dot.className = `vm-dot ${status.sandbox ? 'ok' : 'warn'}`;
    }
  }

  async function refreshStatus() {
    try { applyStatus(await api.get('/api/agents/status')); } catch (_) { /* backend offline */ }
  }

  let agentsReloadTimer = null;
  function connectEvents() {
    let ws;
    let lastSeq = 0;
    const open = () => {
      try { ws = new WebSocket(wsUrl('/api/events', { last_seq: lastSeq || '' })); } catch (_) { return; }
      ws.onmessage = (message) => {
        let event;
        try { event = JSON.parse(message.data); } catch (_) { return; }
        if (event.seq) lastSeq = Math.max(lastSeq, event.seq);
        if (event.type === 'agents.status') {
          if (event.data.agents_running !== undefined) applyStatus({ ...event.data });
          clearTimeout(agentsReloadTimer);
          agentsReloadTimer = setTimeout(() => { if (activeTab === 'agent') loadAgents(); }, 400);
        } else if (event.type === 'file.changed') {
          if (event.data.path) recentFiles.add(event.data.path);
          if (currentDrawer === 'files') refreshFilesDrawer();
        } else if (event.type === 'run.status' && TERMINAL.includes(event.data.status)) {
          refreshStatus();
        }
      };
      ws.onclose = () => setTimeout(open, 3000);
      ws.onerror = () => {};
    };
    open();
  }

  // =========================================================================
  // 9. Model / provider selection chain
  // =========================================================================
  const modelBadge = $('currentModelBadge');
  const modelPopover = $('modelPopover');
  const providerListPanel = $('providerListPanel');
  const customProviderPanel = $('customProviderPanel');
  const modelPopoverBackdrop = $('modelPopoverBackdrop');
  const addProviderBtn = $('addProviderBtn');
  const providerBackBtn = $('providerBackBtn');
  const customProviderBtn = $('customProviderBtn');
  const customProviderBackBtn = $('customProviderBackBtn');
  const customProviderForm = $('customProviderForm');
  const providerCancelBtn = $('providerCancelBtn');
  const providerKeyToggle = $('providerKeyToggle');
  const providerApiKeyInput = $('providerApiKeyInput');
  const savedProvidersList = $('savedProvidersList');
  const providerModelsSection = $('providerModelsSection');
  const providerModelsList = $('providerModelsList');

  // Only non-secret provider metadata is cached in the browser; API keys live encrypted in the backend.
  let savedProviders = JSON.parse(localStorage.getItem('jevora_providers') || '[]').map((p) => ({ id: p.id, name: p.name, url: p.url }));
  localStorage.setItem('jevora_providers', JSON.stringify(savedProviders));
  let selectedModel = { key: 'auto', label: 'Auto' };

  const PANEL_NONE = 0; const PANEL_MODEL = 1; const PANEL_PROVIDERS = 2; const PANEL_CUSTOM = 3;
  let activePanel = PANEL_NONE;

  function positionPopovers() {
    if (!modelBadge) return;
    const rect = modelBadge.getBoundingClientRect();
    const GAP = 10;
    const right = window.innerWidth - rect.right;
    [modelPopover, providerListPanel, customProviderPanel].forEach((el) => {
      if (el) { el.style.bottom = `${window.innerHeight - rect.top + GAP}px`; el.style.right = `${right}px`; el.style.top = 'auto'; }
    });
  }

  function openPanel(panel) {
    modelPopover?.classList.remove('open');
    providerListPanel?.classList.remove('open');
    customProviderPanel?.classList.remove('open');
    modelBadge?.classList.remove('popover-open');
    activePanel = panel;
    if (panel === PANEL_NONE) { modelPopoverBackdrop?.classList.remove('active'); return; }
    positionPopovers();
    modelPopoverBackdrop?.classList.add('active');
    if (panel === PANEL_MODEL) { modelPopover?.classList.add('open'); modelBadge?.classList.add('popover-open'); }
    else if (panel === PANEL_PROVIDERS) providerListPanel?.classList.add('open');
    else if (panel === PANEL_CUSTOM) customProviderPanel?.classList.add('open');
  }

  function closeAllPanels() { openPanel(PANEL_NONE); }

  async function annotateModelList() {
    let catalogue;
    try { catalogue = await api.get('/api/models'); } catch (_) { return; }
    document.querySelectorAll('.model-list-item').forEach((item) => {
      const key = item.dataset.model;
      const sub = item.querySelector('.model-item-sub');
      if (!item.dataset.sub) item.dataset.sub = sub?.textContent || '';
      if (key === 'auto') {
        const fast = catalogue.auto?.categories?.fast_classification?.model;
        const heavy = catalogue.auto?.categories?.heavy_reasoning?.model;
        if (sub) sub.textContent = fast || heavy ? 'Best model for task' : 'Add a provider to enable';
        return;
      }
      const alias = catalogue.aliases?.[key];
      const available = alias && alias.available !== false;
      item.classList.toggle('unavailable', !available);
      const vendor = item.dataset.sub.split('·')[0].trim();
      if (sub) sub.textContent = available ? item.dataset.sub : `${vendor} · not configured`;
    });
  }

  modelBadge?.addEventListener('click', (e) => {
    e.stopPropagation();
    if (activePanel === PANEL_MODEL) closeAllPanels();
    else { renderSavedProviders(); openPanel(PANEL_MODEL); annotateModelList(); }
  });
  modelPopoverBackdrop?.addEventListener('click', closeAllPanels);
  [modelPopover, providerListPanel, customProviderPanel].forEach((el) => el?.addEventListener('click', (e) => e.stopPropagation()));

  const modelItems = document.querySelectorAll('.model-list-item');
  modelItems.forEach((item) => {
    item.addEventListener('click', () => {
      if (item.classList.contains('unavailable')) { showToast('Add the matching provider first (Add Provider below)'); return; }
      modelItems.forEach((i) => i.classList.remove('active'));
      item.classList.add('active');
      const modelName = item.querySelector('.model-item-name')?.textContent || item.dataset.model;
      selectedModel = { key: item.dataset.model, label: modelName };
      if (modelBadge) modelBadge.textContent = `${modelName} ›`;
      showToast(`Model set to ${modelName}`);
      closeAllPanels();
    });
  });

  addProviderBtn?.addEventListener('click', () => openPanel(PANEL_PROVIDERS));
  providerBackBtn?.addEventListener('click', () => openPanel(PANEL_MODEL));

  const PROVIDER_DEFAULTS = {
    openai: { name: 'OpenAI', url: 'https://api.openai.com/v1' },
    google: { name: 'Google Gemini', url: 'https://generativelanguage.googleapis.com/v1beta' },
    anthropic: { name: 'Anthropic Claude', url: 'https://api.anthropic.com/v1' },
  };

  document.querySelectorAll('.provider-available-item[data-provider]').forEach((item) => {
    item.addEventListener('click', async () => {
      const defaults = PROVIDER_DEFAULTS[item.dataset.provider];
      if (!defaults) return;
      const apiKey = prompt(`Enter your API key for ${defaults.name}:`, '');
      if (apiKey === null) return;
      const saved = await saveProvider({ name: defaults.name, url: defaults.url, apiKey: apiKey || '' });
      if (saved) showToast(`${defaults.name} saved!`);
      renderSavedProviders();
      openPanel(PANEL_MODEL);
    });
  });

  customProviderBtn?.addEventListener('click', () => openPanel(PANEL_CUSTOM));
  customProviderBackBtn?.addEventListener('click', () => openPanel(PANEL_PROVIDERS));
  providerCancelBtn?.addEventListener('click', () => openPanel(PANEL_PROVIDERS));

  if (providerKeyToggle && providerApiKeyInput) {
    providerKeyToggle.addEventListener('click', () => {
      const isHidden = providerApiKeyInput.type === 'password';
      providerApiKeyInput.type = isHidden ? 'text' : 'password';
      providerKeyToggle.innerHTML = isHidden
        ? '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/><line x1="1" y1="1" x2="23" y2="23"/></svg>'
        : '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
    });
  }

  customProviderForm?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const name = $('providerNameInput')?.value.trim();
    const url = $('providerBaseUrlInput')?.value.trim();
    const apiKey = providerApiKeyInput?.value.trim();
    if (!name) { showToast('Provider name is required'); return; }
    if (!url) { showToast('Base URL is required'); return; }
    const saved = await saveProvider({ name, url, apiKey: apiKey || '' });
    if (!saved) return;
    showToast(`Provider "${name}" saved!`);
    customProviderForm.reset();
    if (providerApiKeyInput) providerApiKeyInput.type = 'password';
    renderSavedProviders();
    openPanel(PANEL_MODEL);
  });

  function persistProviders() {
    localStorage.setItem('jevora_providers', JSON.stringify(savedProviders.map((p) => ({ id: p.id, name: p.name, url: p.url }))));
  }

  async function saveProvider(provider) {
    const existing = savedProviders.find((p) => p.name === provider.name);
    const body = { name: provider.name, base_url: provider.url, api_key: provider.apiKey, kind: 'custom' };
    try {
      let stored;
      try {
        stored = existing?.id ? await api.put(`/api/providers/${encodeURIComponent(existing.id)}`, body) : await api.post('/api/providers', body);
      } catch (error) {
        if (error.status === 409 && error.body?.existing_id) stored = await api.put(`/api/providers/${encodeURIComponent(error.body.existing_id)}`, body);
        else if (error.status === 404 && existing?.id) stored = await api.post('/api/providers', body);
        else throw error;
      }
      const entry = { id: stored.id, name: stored.name, url: stored.base_url };
      savedProviders = savedProviders.filter((p) => p.name !== provider.name && p.id !== stored.id).concat(entry);
      persistProviders();
      return entry;
    } catch (error) {
      showToast(`Provider could not be saved: ${error.message}`);
      return null;
    }
  }

  async function removeProvider(index) {
    const [removed] = savedProviders.splice(index, 1);
    persistProviders();
    if (removed?.id) { try { await api.del(`/api/providers/${encodeURIComponent(removed.id)}`); } catch (_) { /* already gone */ } }
    renderSavedProviders();
  }

  function renderSavedProviders() {
    if (!savedProvidersList) return;
    savedProvidersList.innerHTML = '';
    if (savedProviders.length === 0) { savedProvidersList.innerHTML = '<div class="no-providers-hint">No providers saved yet</div>'; return; }
    savedProviders.forEach((provider, index) => {
      const item = document.createElement('button');
      item.className = 'saved-provider-item';
      item.type = 'button';
      const url = provider.url || '';
      item.innerHTML = `<span class="sp-name">${escapeHtml(provider.name)}</span>
        <span class="sp-url">${escapeHtml(url.replace(/^https?:\/\//, '').substring(0, 28))}${url.length > 35 ? '…' : ''}</span>
        <span class="sp-delete" title="Remove provider"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></span>`;
      item.addEventListener('click', async (e) => {
        if (e.target.closest('.sp-delete')) { e.stopPropagation(); removeProvider(index); return; }
        if (modelBadge) modelBadge.textContent = `${provider.name} · loading… ›`;
        openPanel(PANEL_MODEL);
        await loadProviderModels(provider);
      });
      savedProvidersList.appendChild(item);
    });
  }

  async function loadProviderModels(provider) {
    if (!providerModelsSection || !providerModelsList) return;
    providerModelsSection.hidden = false;
    providerModelsList.innerHTML = '<div class="provider-model-status">Loading models…</div>';
    if (!provider.id) { providerModelsList.innerHTML = '<div class="provider-model-status">Save this provider to fetch its models.</div>'; return; }
    try {
      const { models, error } = await api.get(`/api/providers/${encodeURIComponent(provider.id)}/models`);
      if (!models.length) {
        providerModelsList.innerHTML = `<div class="provider-model-status">No models returned. Check the provider URL and API key.${error ? `<br><span style="font-size:11px">${escapeHtml(error.slice(0, 160))}</span>` : ''}</div>`;
        if (modelBadge) modelBadge.textContent = `${selectedModel.label} ›`;
        return;
      }
      providerModelsList.innerHTML = '';
      models.forEach((model) => {
        const item = document.createElement('button');
        item.className = 'provider-model-item';
        item.type = 'button';
        item.textContent = model;
        item.addEventListener('click', () => {
          selectedModel = { key: `${provider.id}:${model}`, label: model };
          if (modelBadge) modelBadge.textContent = `${provider.name} · ${model} ›`;
          showToast(`Model set to ${model}`);
          closeAllPanels();
        });
        providerModelsList.appendChild(item);
      });
      if (modelBadge) modelBadge.textContent = `${provider.name} ›`;
      showToast(`${models.length} model${models.length === 1 ? '' : 's'} loaded from ${provider.name}`);
    } catch (_) {
      providerModelsList.innerHTML = '<div class="provider-model-status">Could not fetch models. Check the provider URL and API key.</div>';
      if (modelBadge) modelBadge.textContent = `${provider.name} ›`;
    }
  }

  async function syncSavedProviders() {
    try {
      const providers = await api.get('/api/providers');
      savedProviders = providers.map((p) => ({ id: p.id, name: p.name, url: p.base_url }));
      persistProviders();
      renderSavedProviders();
    } catch (_) { /* opening the HTML file directly remains a design preview */ }
  }

  // =========================================================================
  // 10. Boot
  // =========================================================================
  async function boot() {
    const params = new URLSearchParams(window.location.search);
    if (params.get('auth') === 'success') {
      await api.refresh();
      window.history.replaceState({}, '', window.location.pathname);
    }
    try {
      currentUser = await api.get('/api/auth/me');
      renderProfile();
    } catch (_) { /* backend offline */ }
    try {
      features = await api.get('/api/features');
      const webItem = $('webSearchMenuItem');
      if (features.web_search && webItem) {
        webItem.classList.remove('disabled');
        webItem.removeAttribute('aria-disabled');
        webItem.title = 'Search the web (Research mode)';
        webItem.querySelector('.menu-label')?.classList.remove('struck');
        webItem.querySelector('.menu-dot')?.classList.remove('disabled-dot');
      }
    } catch (_) { features = {}; }
    try {
      const health = await api.get('/api/system/health?probe=false');
      engineName = health.decision_engine?.active || engineName;
    } catch (_) { /* offline */ }
    renderSavedProviders();
    syncSavedProviders();
    refreshStatus();
    loadAgents();
    connectEvents();
  }

  boot();
});
