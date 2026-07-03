// ACP chat UI + window manager for the dashboard. Framework-free, vendored
// under /static and lazy-loaded on first "Chat" open (like xterm.js), so the
// dashboard stays a single no-build page. Depends on acp-client.js (protocol)
// and marked.min.js (markdown). See specs/0029-agent-console-and-chat.md.
//
// Model: one maximized chat + a bottom dock of minimized ones. Each chat is a
// live instance owning its own DOM subtree, AcpClient and WebSocket. Minimizing
// parks the subtree (WS stays alive → the agent keeps working); restoring moves
// it back into the modal; closing (x) tears it down. Sessions are durable per
// (branch, agent): opening resumes the stored session via ACP session/load.
(function () {
  'use strict';

  var AGENT_LABELS = { claude: 'Claude', codex: 'Codex' };
  var STATUS = {
    connecting: { label: 'Connecting…', cls: 'is-dim' },
    ready: { label: 'Ready', cls: 'is-green' },
    thinking: { label: 'Working…', cls: 'is-accent' },
    permission: { label: 'Needs approval', cls: 'is-yellow' },
    error: { label: 'Error', cls: 'is-red' },
    closed: { label: 'Disconnected', cls: 'is-red' }
  };

  var instances = {};      // key -> instance
  var activeKey = null;

  function keyOf(branch, type) { return branch + ' ' + type; }
  function labelOf(type) { return AGENT_LABELS[type] || type; }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  // -- markdown (agent output) with a minimal sanitize pass -------------------
  var _BLOCK_TAGS = { SCRIPT: 1, STYLE: 1, IFRAME: 1, OBJECT: 1, EMBED: 1, LINK: 1, META: 1, BASE: 1, FORM: 1 };
  function renderMarkdown(text) {
    var html;
    try {
      html = window.marked ? window.marked.parse(text, { breaks: true }) : null;
    } catch (e) { html = null; }
    if (html == null) {
      var d = el('div');
      d.textContent = text;
      return d.innerHTML;
    }
    var tpl = document.createElement('template');
    tpl.innerHTML = html;
    var walker = document.createTreeWalker(tpl.content, NodeFilter.SHOW_ELEMENT, null);
    var toRemove = [];
    var node = walker.nextNode();
    while (node) {
      if (_BLOCK_TAGS[node.tagName]) {
        toRemove.push(node);
      } else {
        for (var i = node.attributes.length - 1; i >= 0; i--) {
          var attr = node.attributes[i];
          var name = attr.name.toLowerCase();
          if (name.indexOf('on') === 0) {
            node.removeAttribute(attr.name);
          } else if ((name === 'href' || name === 'src') && /^\s*javascript:/i.test(attr.value)) {
            node.removeAttribute(attr.name);
          }
        }
        if (node.tagName === 'A') {
          node.setAttribute('target', '_blank');
          node.setAttribute('rel', 'noopener noreferrer');
        }
      }
      node = walker.nextNode();
    }
    for (var j = 0; j < toRemove.length; j++) {
      if (toRemove[j].parentNode) toRemove[j].parentNode.removeChild(toRemove[j]);
    }
    return tpl.innerHTML;
  }

  function contentText(content) {
    if (!content) return '';
    if (typeof content === 'string') return content;
    if (content.type === 'text') return content.text || '';
    return '';
  }

  // -- DOM helpers scoped to an instance -------------------------------------
  function q(inst, sel) { return inst.el.querySelector(sel); }

  function scrollToBottom(inst) {
    var m = q(inst, '.chat-messages');
    if (m) m.scrollTop = m.scrollHeight;
  }

  function addUser(inst, text) {
    var wrap = el('div', 'chat-msg chat-msg-user');
    wrap.appendChild(el('div', 'chat-bubble', text));
    q(inst, '.chat-messages').appendChild(wrap);
    inst.agentBubble = null;
    scrollToBottom(inst);
  }

  function appendAgent(inst, text) {
    if (!text) return;
    if (!inst.agentBubble) {
      var wrap = el('div', 'chat-msg chat-msg-agent');
      var bubble = el('div', 'chat-bubble chat-md');
      wrap.appendChild(bubble);
      q(inst, '.chat-messages').appendChild(wrap);
      inst.agentBubble = bubble;
      inst.agentBuffer = '';
    }
    inst.agentBuffer += text;
    inst.agentBubble.innerHTML = renderMarkdown(inst.agentBuffer);
    scrollToBottom(inst);
  }

  function appendThought(inst, text) {
    if (!text) return;
    if (!inst.thoughtBox) {
      var det = el('details', 'chat-thinking');
      det.appendChild(el('summary', null, 'Reasoning'));
      var body = el('div', 'chat-thinking-body');
      det.appendChild(body);
      q(inst, '.chat-messages').appendChild(det);
      inst.thoughtBox = body;
      inst.thoughtBuffer = '';
    }
    inst.thoughtBuffer += text;
    inst.thoughtBox.textContent = inst.thoughtBuffer;
    inst.agentBubble = null;
    scrollToBottom(inst);
  }

  function toolStatusText(s) {
    if (s === 'completed') return 'done';
    if (s === 'failed') return 'failed';
    if (s === 'in_progress') return 'running';
    return 'pending';
  }

  function renderToolCall(inst, u) {
    var card = el('div', 'chat-tool');
    var head = el('div', 'chat-tool-head');
    head.appendChild(el('span', 'chat-tool-title', u.title || u.kind || 'Tool'));
    var st = el('span', 'chat-tool-status', toolStatusText(u.status));
    st.setAttribute('data-status', u.status || 'pending');
    head.appendChild(st);
    card.appendChild(head);
    q(inst, '.chat-messages').appendChild(card);
    inst.tools[u.toolCallId] = card;
    inst.agentBubble = null;
    scrollToBottom(inst);
  }

  function updateToolCall(inst, u) {
    var card = inst.tools[u.toolCallId];
    if (!card) { renderToolCall(inst, u); return; }
    if (u.status) {
      var st = card.querySelector('.chat-tool-status');
      st.textContent = toolStatusText(u.status);
      st.setAttribute('data-status', u.status);
    }
    if (u.title) {
      card.querySelector('.chat-tool-title').textContent = u.title;
    }
  }

  function renderPlan(inst, u) {
    var entries = u.entries || u.plan || [];
    if (inst.planBox && inst.planBox.parentNode) {
      inst.planBox.parentNode.removeChild(inst.planBox);
    }
    var box = el('div', 'chat-plan');
    box.appendChild(el('div', 'chat-plan-title', 'Plan'));
    var list = el('ul', 'chat-plan-list');
    for (var i = 0; i < entries.length; i++) {
      var li = el('li');
      li.setAttribute('data-status', entries[i].status || 'pending');
      li.textContent = contentText(entries[i].content) || entries[i].content || '';
      list.appendChild(li);
    }
    box.appendChild(list);
    q(inst, '.chat-messages').appendChild(box);
    inst.planBox = box;
    inst.agentBubble = null;
    scrollToBottom(inst);
  }

  function addSystemLine(inst, text) {
    q(inst, '.chat-messages').appendChild(el('div', 'chat-system', text));
    scrollToBottom(inst);
  }

  function addErrorLine(inst, text) {
    q(inst, '.chat-messages').appendChild(el('div', 'chat-system chat-error', text));
    scrollToBottom(inst);
  }

  // -- status + dock ----------------------------------------------------------
  function setStatus(inst, status) {
    inst.status = status;
    var meta = STATUS[status] || STATUS.ready;
    var badge = q(inst, '.chat-status');
    if (badge) {
      badge.textContent = meta.label;
      badge.className = 'chat-status ' + meta.cls;
    }
    updatePill(inst);
  }

  function setBusy(inst, busy) {
    inst.busy = busy;
    var send = q(inst, '.chat-send');
    var stop = q(inst, '.chat-stop');
    if (send) send.disabled = busy;
    if (stop) stop.hidden = !busy;
  }

  function updatePill(inst) {
    if (!inst.pill) return;
    var meta = STATUS[inst.status] || STATUS.ready;
    inst.pill.querySelector('.chat-pill-dot').className = 'chat-pill-dot ' + meta.cls;
    inst.pill.querySelector('.chat-pill-state').textContent = meta.label;
    inst.pill.classList.toggle('has-unread', !!inst.unread);
  }

  function addPill(inst) {
    var dock = document.getElementById('chat-dock');
    var pill = el('button', 'chat-pill');
    pill.type = 'button';
    pill.title = 'Restore chat — ' + inst.branch;
    pill.appendChild(el('span', 'chat-pill-dot'));
    var body = el('span', 'chat-pill-body');
    body.appendChild(el('span', 'chat-pill-name', inst.branch));
    var meta = el('span', 'chat-pill-meta');
    meta.appendChild(el('span', 'chat-pill-agent', labelOf(inst.type)));
    meta.appendChild(el('span', 'chat-pill-state', (STATUS[inst.status] || STATUS.ready).label));
    body.appendChild(meta);
    pill.appendChild(body);
    var close = el('span', 'chat-pill-close', '×');
    close.setAttribute('role', 'button');
    close.setAttribute('aria-label', 'Close chat');
    close.title = 'Close chat';
    close.onclick = function (ev) { ev.stopPropagation(); closeChat(inst.key); };
    pill.appendChild(close);
    pill.onclick = function () { restoreChat(inst.key); };
    dock.appendChild(pill);
    inst.pill = pill;
    updatePill(inst);
  }

  function removePill(inst) {
    if (inst.pill && inst.pill.parentNode) inst.pill.parentNode.removeChild(inst.pill);
    inst.pill = null;
  }

  // -- permission -------------------------------------------------------------
  function renderPermission(inst, requestId, params) {
    var area = q(inst, '.chat-permission');
    area.innerHTML = '';
    var tc = params.toolCall || {};
    area.appendChild(el('div', 'chat-perm-title', 'Approve: ' + (tc.title || 'action') + '?'));
    var opts = params.options || [];
    var row = el('div', 'chat-perm-actions');
    for (var i = 0; i < opts.length; i++) {
      (function (opt) {
        var b = el('button', 'chat-perm-btn' + (opt.kind && opt.kind.indexOf('allow') === 0 ? ' primary' : ''));
        b.type = 'button';
        b.textContent = opt.name || opt.optionId;
        b.onclick = function () {
          inst.client.respondPermission(requestId, opt.optionId);
          area.hidden = true;
          area.innerHTML = '';
          if (inst.busy) setStatus(inst, 'thinking');
        };
        row.appendChild(b);
      })(opts[i]);
    }
    area.appendChild(row);
    area.hidden = false;
    setStatus(inst, 'permission');
    if (inst.key !== activeKey) { inst.unread = true; updatePill(inst); }
    scrollToBottom(inst);
  }

  // -- update dispatch --------------------------------------------------------
  function handleUpdate(inst, params) {
    var u = params && params.update;
    if (!u) return;
    switch (u.sessionUpdate) {
      case 'user_message_chunk': addUser(inst, contentText(u.content)); break;
      case 'agent_message_chunk': appendAgent(inst, contentText(u.content)); break;
      case 'agent_thought_chunk': appendThought(inst, contentText(u.content)); break;
      case 'tool_call': renderToolCall(inst, u); break;
      case 'tool_call_update': updateToolCall(inst, u); break;
      case 'plan': renderPlan(inst, u); break;
      default: break; // available_commands_update, current_mode_update, ...
    }
  }

  // -- models -----------------------------------------------------------------
  function applyModels(inst, models) {
    if (!models || !models.availableModels || !models.availableModels.length) return;
    var sel = q(inst, '.chat-model');
    sel.innerHTML = '';
    for (var i = 0; i < models.availableModels.length; i++) {
      var m = models.availableModels[i];
      var opt = el('option', null, m.name || m.modelId);
      opt.value = m.modelId;
      if (m.modelId === models.currentModelId) opt.selected = true;
      sel.appendChild(opt);
    }
    sel.hidden = false;
    sel.onchange = function () {
      if (inst.sessionId) inst.client.setModel(inst.sessionId, sel.value).catch(function () {});
    };
  }

  // -- REST helpers -----------------------------------------------------------
  function apiBase(branch) {
    return '/api/environments/' + encodeURIComponent(branch) + '/agent-acp';
  }
  function wsUrlFor(branch, type) {
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return proto + '//' + location.host + apiBase(branch) + '?type=' + encodeURIComponent(type);
  }
  function fetchInfo(branch, type) {
    return fetch(apiBase(branch) + '/info?type=' + encodeURIComponent(type), { credentials: 'same-origin' })
      .then(function (r) { return r.json(); });
  }
  function postSession(branch, type, sessionId) {
    return fetch(apiBase(branch) + '/session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ type: type, session_id: sessionId || '' })
    }).catch(function () {});
  }

  // -- instance construction --------------------------------------------------
  function buildInstanceDom(inst) {
    var root = el('div', 'chat-instance');

    var sub = el('div', 'chat-subheader');
    var left = el('div', 'chat-sub-left');
    var status = el('span', 'chat-status is-dim', 'Connecting…');
    left.appendChild(status);
    var model = el('select', 'chat-model');
    model.hidden = true;
    model.setAttribute('aria-label', 'Model');
    left.appendChild(model);
    sub.appendChild(left);
    var newBtn = el('button', 'chat-new', 'New conversation');
    newBtn.type = 'button';
    newBtn.title = 'End this conversation and start a fresh one';
    newBtn.onclick = function () { startNewConversation(inst); };
    sub.appendChild(newBtn);
    root.appendChild(sub);

    var messages = el('div', 'chat-messages');
    messages.setAttribute('role', 'log');
    messages.setAttribute('aria-live', 'polite');
    root.appendChild(messages);

    var perm = el('div', 'chat-permission');
    perm.hidden = true;
    root.appendChild(perm);

    var form = el('form', 'chat-composer');
    var ta = el('textarea', 'chat-input');
    ta.rows = 2;
    ta.placeholder = 'Message the agent…  (Enter to send, Shift+Enter for a new line)';
    form.appendChild(ta);
    var actions = el('div', 'chat-composer-actions');
    var send = el('button', 'btn chat-send', 'Send');
    send.type = 'submit';
    var stop = el('button', 'btn chat-stop', 'Stop');
    stop.type = 'button';
    stop.hidden = true;
    stop.onclick = function () { if (inst.sessionId) inst.client.cancel(inst.sessionId); setBusy(inst, false); setStatus(inst, 'ready'); };
    actions.appendChild(stop);
    actions.appendChild(send);
    form.appendChild(actions);
    form.onsubmit = function (e) { e.preventDefault(); sendPrompt(inst); };
    ta.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendPrompt(inst); }
    });
    root.appendChild(form);

    inst.el = root;
  }

  function sendPrompt(inst) {
    var ta = q(inst, '.chat-input');
    var text = (ta.value || '').trim();
    if (!text || inst.busy || !inst.sessionId) return;
    addUser(inst, text);
    ta.value = '';
    setBusy(inst, true);
    setStatus(inst, 'thinking');
    inst.client.prompt(inst.sessionId, text)
      .catch(function (e) { addErrorLine(inst, e.message || String(e)); })
      .then(function () {
        setBusy(inst, false);
        inst.agentBubble = null;
        inst.thoughtBox = null;
        if (inst.status !== 'closed') setStatus(inst, 'ready');
      });
    q(inst, '.chat-input').focus();
  }

  function startNewConversation(inst) {
    if (!inst.client || inst.client.closed) return;
    setStatus(inst, 'connecting');
    inst.client.newSession(inst.cwd).then(function (res) {
      inst.sessionId = res.sessionId;
      inst.agentBubble = null; inst.thoughtBox = null; inst.tools = {}; inst.planBox = null;
      q(inst, '.chat-messages').innerHTML = '';
      applyModels(inst, res.models);
      postSession(inst.branch, inst.type, res.sessionId);
      setStatus(inst, 'ready');
      addSystemLine(inst, 'Started a new conversation.');
    }).catch(function (e) { addErrorLine(inst, e.message || String(e)); setStatus(inst, 'error'); });
  }

  function bootstrap(inst) {
    setStatus(inst, 'connecting');
    fetchInfo(inst.branch, inst.type).then(function (info) {
      if (!info || !info.ok) throw new Error((info && info.error) || 'Failed to load chat info');
      inst.cwd = info.cwd;
      inst.client.on('update', function (p) { handleUpdate(inst, p); });
      inst.client.on('permission', function (p) { renderPermission(inst, p.requestId, p.params); });
      inst.client.on('error', function (msg) { addErrorLine(inst, msg); });
      inst.client.on('notice', function (msg) { if (msg) addSystemLine(inst, msg); });
      inst.client.on('close', function () { setStatus(inst, 'closed'); setBusy(inst, false); });
      return inst.client.connect().then(function () {
        return inst.client.initialize();
      }).then(function () {
        if (info.session_id) {
          return inst.client.loadSession(info.session_id, inst.cwd).then(function (res) {
            inst.sessionId = info.session_id;
            if (res && res.models) applyModels(inst, res.models);
            addSystemLine(inst, 'Resumed your previous conversation.');
          }).catch(function () {
            // Stale/invalid stored session — start fresh.
            return inst.client.newSession(inst.cwd).then(function (res) {
              inst.sessionId = res.sessionId;
              applyModels(inst, res.models);
              return postSession(inst.branch, inst.type, res.sessionId);
            });
          });
        }
        return inst.client.newSession(inst.cwd).then(function (res) {
          inst.sessionId = res.sessionId;
          applyModels(inst, res.models);
          return postSession(inst.branch, inst.type, res.sessionId);
        });
      });
    }).then(function () {
      setStatus(inst, 'ready');
      q(inst, '.chat-input').focus();
    }).catch(function (e) {
      addErrorLine(inst, e.message || String(e));
      setStatus(inst, 'error');
    });
  }

  // -- window manager ---------------------------------------------------------
  function open(branch, type) {
    type = (type || window.ODU_AGENT_DEFAULT || 'claude').toLowerCase();
    if (type !== 'claude' && type !== 'codex') type = 'claude';
    var key = keyOf(branch, type);
    if (instances[key]) { activate(instances[key]); return; }
    var inst = {
      key: key, branch: branch, type: type, status: 'connecting', busy: false,
      client: new window.AcpClient(wsUrlFor(branch, type)),
      sessionId: null, cwd: null, agentBubble: null, agentBuffer: '',
      thoughtBox: null, tools: {}, planBox: null, pill: null, unread: false
    };
    buildInstanceDom(inst);
    instances[key] = inst;
    activate(inst);
    bootstrap(inst);
  }

  function activate(inst) {
    if (activeKey && activeKey !== inst.key) minimize();
    var host = document.getElementById('chat-active');
    host.appendChild(inst.el);
    document.getElementById('chat-title').textContent = 'Chat · ' + labelOf(inst.type) + ' — ' + inst.branch;
    removePill(inst);
    inst.unread = false;
    activeKey = inst.key;
    showModal('chat-modal');
    var ta = q(inst, '.chat-input');
    if (ta) ta.focus();
  }

  function minimize() {
    if (!activeKey) { hideModal('chat-modal'); return; }
    var inst = instances[activeKey];
    document.getElementById('chat-parking').appendChild(inst.el);
    addPill(inst);
    hideModal('chat-modal');
    activeKey = null;
  }

  function restoreChat(key) {
    var inst = instances[key];
    if (inst) activate(inst);
  }

  function closeChat(key) {
    var inst = instances[key];
    if (!inst) return;
    try { inst.client.close(); } catch (e) { /* ignore */ }
    removePill(inst);
    if (inst.el && inst.el.parentNode) inst.el.parentNode.removeChild(inst.el);
    delete instances[key];
    if (activeKey === key) { activeKey = null; hideModal('chat-modal'); }
  }

  function closeActive() { if (activeKey) closeChat(activeKey); }

  function overlayClick(event) {
    if (event && event.target !== event.currentTarget) return;
    minimize();
  }

  // Exposed API + globals for the static modal's inline handlers.
  window.ChatManager = { open: open, minimize: minimize, restore: restoreChat, close: closeChat, closeActive: closeActive };
  window.minimizeActiveChat = minimize;
  window.closeActiveChat = closeActive;
  window.chatOverlayClick = overlayClick;
})();
