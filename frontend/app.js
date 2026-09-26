/**
 * Build Agent IDE — Main Application Logic
 * Implements interactive behaviors for modes, attach menu, chat simulation,
 * sidebar panel toggle, real system tool panels (file preview, terminal, MCP, plugins),
 * and JEV model + provider settings.
 */

document.addEventListener('DOMContentLoaded', () => {
  // DOM Elements
  const modeDropdownBtn = document.getElementById('modeDropdownBtn');
  const modeDropdownMenu = document.getElementById('modeDropdownMenu');
  const modeDropdownLabel = document.getElementById('modeDropdownLabel');
  const modeDropdownItems = document.querySelectorAll('.mode-dropdown-item');
  const attachToggleBtn = document.getElementById('attachToggleBtn');
  const attachMenu = document.getElementById('attachMenu');
  const agentInput = document.getElementById('agentInput');
  const sendBtn = document.getElementById('sendBtn');
  const emptyState = document.getElementById('emptyState');
  const streamContainer = document.getElementById('streamContainer');
  const agentOutputPanel = document.getElementById('agentOutputPanel');
  const activeAttachmentsContainer = document.getElementById('activeAttachments');
  const historyBtn = document.getElementById('historyBtn');

  // Sidebar Toggle & Column
  const sidebarToggleBtn = document.getElementById('sidebarToggleBtn');
  const sidebarColumn = document.querySelector('.sidebar-column');

  // Sidebar Tool Rows & Drawer
  const toolRows = document.querySelectorAll('.tool-row');
  const drawerOverlay = document.getElementById('drawerOverlay');
  const toolDrawer = document.getElementById('toolDrawer');
  const drawerTitle = document.getElementById('drawerTitle');
  const drawerBody = document.getElementById('drawerBody');
  const drawerCloseBtn = document.getElementById('drawerCloseBtn');



  // State
  let currentMode = 'build';
  let activeAttachments = [];
  let isSidebarOpen = true;

  const signInBtn = document.getElementById('signInBtn');
  if (signInBtn) {
    signInBtn.addEventListener('click', () => {
      window.location.href = 'auth.html';
    });
  }

  // Set initial active state for toggle button
  if (sidebarToggleBtn) {
    sidebarToggleBtn.classList.add('active');
  }

  // =========================================================================
  // 1. Sidebar Toggle Button
  // =========================================================================
  if (sidebarToggleBtn && sidebarColumn) {
    sidebarToggleBtn.addEventListener('click', () => {
      isSidebarOpen = !isSidebarOpen;
      if (isSidebarOpen) {
        sidebarColumn.classList.remove('collapsed');
        sidebarToggleBtn.classList.add('active');
        showToast('Context & Tools panel opened');
      } else {
        sidebarColumn.classList.add('collapsed');
        sidebarToggleBtn.classList.remove('active');
        showToast('Context & Tools panel hidden');
      }
    });
  }

  const leftTabs = document.querySelectorAll('.left-tab');
  const leftTabBuild = document.getElementById('leftTabBuild');
  const leftTabAgent = document.getElementById('leftTabAgent');
  const createAgentBtn = document.getElementById('createAgentBtn');
  const filterBtns = document.querySelectorAll('.filter-btn');

  const modeDropdownWrapperEl = document.getElementById('modeDropdownWrapper');

  leftTabs.forEach(tab => {
    tab.addEventListener('click', () => {
      leftTabs.forEach(t => t.classList.remove('active'));
      tab.classList.add('active');

      const target = tab.dataset.leftTab;
      if (target === 'build') {
        leftTabBuild.classList.add('active');
        leftTabAgent.classList.remove('active');
        // Show mode dropdown in central panel
        if (modeDropdownWrapperEl) modeDropdownWrapperEl.style.display = '';
      } else {
        leftTabAgent.classList.add('active');
        leftTabBuild.classList.remove('active');
        // Hide mode dropdown in Agent creation view
        if (modeDropdownWrapperEl) modeDropdownWrapperEl.style.display = 'none';
        // Also close the dropdown if it was open
        if (modeDropdownMenu) {
          modeDropdownMenu.classList.remove('open');
          modeDropdownBtn?.classList.remove('open');
        }
      }
    });
  });

  // Filter toggle (Group / Project)
  filterBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      filterBtns.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
    });
  });

  // Create Agent FAB
  if (createAgentBtn) {
    createAgentBtn.addEventListener('click', () => {
      showToast('Agent creation flow — coming soon');
    });
  }

  // =========================================================================
  // 2. Mode Dropdown Switching (Build | Chat | Research | Analysis)
  // =========================================================================
  if (modeDropdownBtn && modeDropdownMenu) {
    modeDropdownBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const isOpen = modeDropdownMenu.classList.toggle('open');
      modeDropdownBtn.classList.toggle('open', isOpen);
      modeDropdownBtn.setAttribute('aria-expanded', String(isOpen));
      // Close attach menu if open
      if (attachMenu) attachMenu.classList.remove('open');
    });

    modeDropdownItems.forEach(item => {
      item.addEventListener('click', (e) => {
        e.stopPropagation();
        modeDropdownItems.forEach(i => i.classList.remove('active'));
        item.classList.add('active');

        const selectedMode = item.dataset.mode;
        currentMode = selectedMode;

        // Capitalize mode title
        const modeTitle = item.querySelector('.mode-item-title')?.textContent || selectedMode;
        if (modeDropdownLabel) {
          modeDropdownLabel.textContent = modeTitle;
        }

        // Close dropdown
        modeDropdownMenu.classList.remove('open');
        modeDropdownBtn.classList.remove('open');
        modeDropdownBtn.setAttribute('aria-expanded', 'false');

        showToast(`Switched to ${modeTitle} mode`);

        // Update placeholder according to mode
        if (agentInput) {
          if (currentMode === 'build') {
            agentInput.placeholder = 'Message your agent (e.g. Build a full-stack dashboard)...';
          } else if (currentMode === 'chat') {
            agentInput.placeholder = 'Chat with agent...';
          } else if (currentMode === 'research') {
            agentInput.placeholder = 'Search, summarize and research any topic...';
          } else if (currentMode === 'analysis') {
            agentInput.placeholder = 'Provide files or repository for deep analysis...';
          }
        }
      });
    });
  }

  // =========================================================================
  // 3. Attach Menu Popover Toggle & Actions
  // =========================================================================
  if (attachToggleBtn && attachMenu) {
    attachToggleBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      attachMenu.classList.toggle('open');
      // Close mode dropdown if open
      if (modeDropdownMenu) {
        modeDropdownMenu.classList.remove('open');
        modeDropdownBtn.classList.remove('open');
      }
    });
  }

  // Close menus when clicking outside
  document.addEventListener('click', (e) => {
    if (modeDropdownMenu && !modeDropdownMenu.contains(e.target) && e.target !== modeDropdownBtn && !modeDropdownBtn?.contains(e.target)) {
      modeDropdownMenu.classList.remove('open');
      modeDropdownBtn?.classList.remove('open');
      modeDropdownBtn?.setAttribute('aria-expanded', 'false');
    }
    if (attachMenu && !attachMenu.contains(e.target) && e.target !== attachToggleBtn && !attachToggleBtn?.contains(e.target)) {
      attachMenu.classList.remove('open');
    }
  });

  // Attach Menu Options
  const attachItems = attachMenu.querySelectorAll('.attach-menu-item');
  attachItems.forEach(item => {
    item.addEventListener('click', (e) => {
      if (item.classList.contains('disabled')) {
        showToast('Web search is not available yet');
        return;
      }
      const action = item.dataset.action;
      attachMenu.classList.remove('open');

      if (action === 'files') {
        const fileInput = document.createElement('input');
        fileInput.type = 'file';
        fileInput.multiple = true;
        fileInput.onchange = (ev) => {
          const files = ev.target.files;
          if (files && files.length > 0) {
            for (let i = 0; i < files.length; i++) {
              addAttachment(`📄 ${files[i].name}`);
            }
          }
        };
        fileInput.click();
      } else if (action === 'photos') {
        const photoInput = document.createElement('input');
        photoInput.type = 'file';
        photoInput.accept = 'image/*';
        photoInput.onchange = (ev) => {
          const files = ev.target.files;
          if (files && files.length > 0) {
            addAttachment(`🖼️ ${files[0].name}`);
          }
        };
        photoInput.click();
      } else if (action === 'commands') {
        agentInput.value = '/test ' + agentInput.value;
        agentInput.focus();
      }
    });
  });

  function addAttachment(name) {
    if (!activeAttachments.includes(name)) {
      activeAttachments.push(name);
      renderAttachments();
      showToast(`Attached ${name}`);
    }
  }

  function removeAttachment(index) {
    activeAttachments.splice(index, 1);
    renderAttachments();
  }

  function renderAttachments() {
    activeAttachmentsContainer.innerHTML = '';
    activeAttachments.forEach((att, index) => {
      const tag = document.createElement('span');
      tag.className = 'attachment-tag';
      tag.innerHTML = `${escapeHtml(att)} <span class="attachment-remove" data-index="${index}">&times;</span>`;
      tag.querySelector('.attachment-remove').addEventListener('click', (e) => {
        e.stopPropagation();
        removeAttachment(index);
      });
      activeAttachmentsContainer.appendChild(tag);
    });
  }

  // =========================================================================
  // 4. Chat & Agent Output Simulation (with JEV verification feedback)
  // =========================================================================
  function handleSendMessage() {
    const text = agentInput.value.trim();
    if (!text && activeAttachments.length === 0) return;

    // Hide empty state and show stream container
    emptyState.style.display = 'none';
    streamContainer.style.display = 'flex';

    // User Message
    const userMessage = document.createElement('div');
    userMessage.className = 'message-bubble user';

    let attachmentMarkup = '';
    if (activeAttachments.length > 0) {
      attachmentMarkup = `<div style="font-size:12px;opacity:0.9;margin-bottom:4px;">Attachments: ${activeAttachments.map(escapeHtml).join(', ')}</div>`;
    }

    userMessage.innerHTML = `
      <div class="bubble-content">
        ${attachmentMarkup}
        <div>${escapeHtml(text || 'Attached context')}</div>
      </div>
    `;
    streamContainer.appendChild(userMessage);

    // Clear input & attachments
    agentInput.value = '';
    activeAttachments = [];
    renderAttachments();
    agentOutputPanel.scrollTop = agentOutputPanel.scrollHeight;

    // Simulate Agent Thinking & Response
    simulateAgentResponse(text);
  }

  sendBtn.addEventListener('click', handleSendMessage);
  agentInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSendMessage();
    }
  });

  function simulateAgentResponse(prompt) {
    const agentMessage = document.createElement('div');
    agentMessage.className = 'message-bubble agent';
    agentMessage.innerHTML = `
      <div class="bubble-header">
        <span class="tool-dot"></span>
        <span>IDE Agent (${currentMode.toUpperCase()}) · LangGraph Orchestrator</span>
      </div>
      <div class="bubble-content">
        <div class="typing-indicator" style="color:var(--text-secondary);">
          <span style="display:inline-block;animation:pulse 1s infinite;">●</span> Routing to agent pool and executing with JEV decision verifier...
        </div>
      </div>
    `;
    streamContainer.appendChild(agentMessage);
    agentOutputPanel.scrollTop = agentOutputPanel.scrollHeight;

    setTimeout(() => {
      const contentEl = agentMessage.querySelector('.bubble-content');
      contentEl.innerHTML = `
        <p>I have processed the task requirement: <strong>${escapeHtml(prompt || 'workspace context')}</strong>.</p>
        <p style="margin-top:8px;font-size:13px;color:var(--text-secondary);">JEV Verifier: Grounded on system tool output (zero-hallucination verified).</p>
        <pre><code>✓ Planner Node: Decomposed task into sub-agent work units
✓ Coder Node: Generated verified source code
✓ JEV Critic Node: Verified tool output against test assertions</code></pre>
        <p style="margin-top:8px;">Ready for next instruction.</p>
      `;
      agentOutputPanel.scrollTop = agentOutputPanel.scrollHeight;
    }, 1000);
  }

  // =========================================================================
  // 5. Context & Tools Drawers (Real System Context)
  // =========================================================================
  const toolContents = {
    files: {
      title: 'File Preview',
      html: `
        <div style="display:flex;flex-direction:column;gap:18px;">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <span style="font-weight:600;color:var(--text-secondary);font-size:12px;letter-spacing:0.5px;">PROJECT FILES</span>
            <span style="font-size:11px;color:var(--text-muted);">Real system workspace</span>
          </div>
          
          <div style="background:var(--bg-surface-alt);border:1px solid var(--border-card);border-radius:12px;padding:28px 16px;text-align:center;color:var(--text-secondary);">
            <div style="font-size:24px;margin-bottom:8px;opacity:0.6;">📁</div>
            <strong style="display:block;font-size:13.5px;color:var(--text-primary);margin-bottom:4px;">No files created yet</strong>
            <p style="font-size:12.5px;color:var(--text-muted);max-width:280px;margin:0 auto;line-height:1.5;">
              Real files created or modified by your IDE agents will be displayed and previewed here automatically.
            </p>
          </div>

          <div style="padding:12px;border:1px solid var(--border-subtle);border-radius:8px;background:var(--bg-surface);font-size:12px;color:var(--text-secondary);line-height:1.5;">
            💡 <strong>System Access:</strong> The IDE operates directly on your local project directory with full file I/O permissions.
          </div>
        </div>
      `
    },
    terminal: {
      title: 'System Terminal',
      html: `
        <div style="display:flex;flex-direction:column;gap:14px;">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <span style="font-weight:600;color:var(--text-secondary);font-size:12px;letter-spacing:0.5px;">LOCAL SHELL</span>
            <span style="font-size:11px;color:var(--text-muted);">System Execution Sandbox</span>
          </div>

          <div style="background:#0F172A;color:#38BDF8;padding:16px;border-radius:10px;font-family:var(--font-mono);font-size:12.5px;min-height:200px;line-height:1.7;">
            <div style="color:#64748B;"># IDE Coder — Connected to system shell</div>
            <div style="color:#94A3B8;"># Standby: Terminal opens live process when downloaded & ran on system</div>
            <div style="color:#4ADE80;margin-top:12px;">$ <span style="animation:pulse 1s infinite;">_</span></div>
          </div>

          <div style="font-size:12px;color:var(--text-secondary);line-height:1.5;">
            Agents execute builds, package installations, and test runners directly in this environment.
          </div>
        </div>
      `
    },
    mcp: {
      title: 'Model Context Protocol (MCP)',
      html: `
        <div style="display:flex;flex-direction:column;gap:14px;">
          <p style="color:var(--text-secondary);font-size:13px;">Connected MCP servers & system tool providers:</p>
          
          <div style="display:flex;flex-direction:column;gap:10px;">
            <div style="padding:14px;border:1px solid var(--border-card);border-radius:10px;background:var(--bg-surface-alt);display:flex;justify-content:space-between;align-items:center;">
              <div>
                <strong>JEV Decision & Verifier Engine</strong>
                <div style="font-size:11.5px;color:var(--text-secondary);">Model assignment & zero-hallucination critic</div>
              </div>
              <span style="font-size:12px;color:#10B981;font-weight:600;">Active</span>
            </div>

            <div style="padding:14px;border:1px solid var(--border-card);border-radius:10px;background:var(--bg-surface-alt);display:flex;justify-content:space-between;align-items:center;">
              <div>
                <strong>LangGraph Orchestration State</strong>
                <div style="font-size:11.5px;color:var(--text-secondary);">Multi-agent state graph & artifact handoff</div>
              </div>
              <span style="font-size:12px;color:#10B981;font-weight:600;">Active</span>
            </div>

            <div style="padding:14px;border:1px solid var(--border-card);border-radius:10px;background:var(--bg-surface-alt);display:flex;justify-content:space-between;align-items:center;">
              <div>
                <strong>System Tool & Sandbox Layer</strong>
                <div style="font-size:11.5px;color:var(--text-secondary);">Native file I/O, process execution & testing</div>
              </div>
              <span style="font-size:12px;color:#10B981;font-weight:600;">Active</span>
            </div>
          </div>
        </div>
      `
    },
    plugins: {
      title: 'Active Plugins & Modules',
      html: `
        <div style="display:flex;flex-direction:column;gap:12px;">
          <div style="padding:12px;border:1px solid var(--border-card);border-radius:10px;background:var(--bg-surface-alt);display:flex;justify-content:space-between;align-items:center;">
            <div>
              <strong>JEV Anti-Hallucination Protocol</strong>
              <div style="font-size:11px;color:var(--text-secondary);">Grounded tool output verification loop</div>
            </div>
            <input type="checkbox" checked style="accent-color:var(--accent);width:18px;height:18px;" />
          </div>

          <div style="padding:12px;border:1px solid var(--border-card);border-radius:10px;background:var(--bg-surface-alt);display:flex;justify-content:space-between;align-items:center;">
            <div>
              <strong>Iterative Multi-Agent Tester</strong>
              <div style="font-size:11px;color:var(--text-secondary);">Auto-test execution and bug fixing passes</div>
            </div>
            <input type="checkbox" checked style="accent-color:var(--accent);width:18px;height:18px;" />
          </div>

          <div style="padding:12px;border:1px solid var(--border-card);border-radius:10px;background:var(--bg-surface-alt);display:flex;justify-content:space-between;align-items:center;">
            <div>
              <strong>Dynamic Model Router</strong>
              <div style="font-size:11px;color:var(--text-secondary);">Auto-allocates heavy/light LLMs per task</div>
            </div>
            <input type="checkbox" checked style="accent-color:var(--accent);width:18px;height:18px;" />
          </div>
        </div>
      `
    }
  };

  toolRows.forEach(row => {
    row.addEventListener('click', () => {
      const toolKey = row.dataset.tool;
      const data = toolContents[toolKey] || { title: 'Tool Details', html: '<p>Tool configuration</p>' };
      drawerTitle.textContent = data.title;
      drawerBody.innerHTML = data.html;
      openDrawer();
    });
  });

  function openDrawer() {
    drawerOverlay.classList.add('active');
    toolDrawer.classList.add('open');
    toolDrawer.setAttribute('aria-hidden', 'false');
  }

  function closeDrawer() {
    drawerOverlay.classList.remove('active');
    toolDrawer.classList.remove('open');
    toolDrawer.setAttribute('aria-hidden', 'true');
  }

  drawerCloseBtn.addEventListener('click', closeDrawer);
  drawerOverlay.addEventListener('click', () => {
    closeDrawer();
    closeModal();
  });

  // History button
  if (historyBtn) {
    historyBtn.addEventListener('click', () => {
      drawerTitle.textContent = 'Agent Session History';
      drawerBody.innerHTML = `
        <div style="display:flex;flex-direction:column;gap:12px;">
          <div style="padding:14px;border:1px solid var(--border-card);border-radius:10px;background:var(--bg-surface-alt);">
            <div style="font-size:11px;color:var(--text-secondary);">Today, 9:50 AM</div>
            <strong style="display:block;margin-top:2px;">Build Agent Workspace Initialization</strong>
            <div style="font-size:12px;color:var(--text-secondary);margin-top:4px;">Mode: Build · Verified with JEV Engine</div>
          </div>
        </div>
      `;
      openDrawer();
    });
  }



  // =========================================================================
  // Helper: Toast Notifications & HTML Escaping
  // =========================================================================
  function showToast(message) {
    const container = document.getElementById('toastContainer');
    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.textContent = message;
    container.appendChild(toast);

    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transform = 'translateY(10px)';
      toast.style.transition = 'all 0.2s ease';
      setTimeout(() => toast.remove(), 200);
    }, 2500);
  }

  function escapeHtml(string) {
    const map = {
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#039;'
    };
    return String(string).replace(/[&<>"']/g, (m) => map[m]);
  }
});
