// ACP (Agent Client Protocol) client over WebSocket — vendored, framework-free.
//
// Speaks JSON-RPC 2.0 to the coder-container ACP adapter through the dashboard's
// `.../agent-acp` relay (see web_ui.ws_agent_acp). This is a from-scratch port of
// the wire logic in the reference project (formulahendry/acp-ui: src/lib/
// acp-bridge.ts) with the desktop/Tauri/stdio parts dropped — the browser chat
// only ever talks to a remote agent over ws/wss. See specs/0029-agent-console-and-chat.md.
//
// The agent runs inside the container with a real filesystem, so we advertise
// no client fs capability and reject any inbound fs/* RPC with method-not-found
// (the agent then does file I/O itself). We surface session/update notifications
// and session/request_permission requests as events for the chat UI to render.
(function () {
  'use strict';

  var JSONRPC_METHOD_NOT_FOUND = -32601;
  // Default per-request timeout. session/prompt and session/load are exempt
  // (timeoutMs 0): a prompt resolves only when the agent's whole turn ends and
  // a load only after the full transcript replays, so minutes are normal, not
  // a hang. A dead connection is covered by the heartbeat + onclose, which
  // rejects every pending request.
  var REQUEST_TIMEOUT_MS = 60000;
  var HEARTBEAT_MS = 25000; // keep NAT/proxy idle mappings warm ($/ping)

  function AcpClient(wsUrl) {
    this.wsUrl = wsUrl;
    this.ws = null;
    this.closed = false;
    this._nextId = 0;
    this._pending = {};       // id -> {resolve, reject, method, timer}
    this._listeners = {};     // event -> [cb]
    this._heartbeat = null;
    this._rxBuffer = '';
    this._configOptionSessions = {}; // session id -> modern ACP configOptions
  }

  // -- tiny event emitter -----------------------------------------------------
  AcpClient.prototype.on = function (event, cb) {
    (this._listeners[event] = this._listeners[event] || []).push(cb);
    return this;
  };
  AcpClient.prototype._emit = function (event, payload) {
    var cbs = this._listeners[event];
    if (!cbs) return;
    for (var i = 0; i < cbs.length; i++) {
      try { cbs[i](payload); } catch (e) { console.error('acp listener threw', e); }
    }
  };

  // -- connection -------------------------------------------------------------
  AcpClient.prototype.connect = function () {
    var self = this;
    return new Promise(function (resolve, reject) {
      var ws;
      try {
        ws = new WebSocket(self.wsUrl);
      } catch (e) {
        reject(e);
        return;
      }
      self.ws = ws;
      var settled = false;
      ws.onopen = function () {
        settled = true;
        self._startHeartbeat();
        self._emit('open');
        resolve();
      };
      ws.onerror = function () {
        if (!settled) { settled = true; reject(new Error('WebSocket connection failed')); }
      };
      ws.onclose = function (ev) {
        self._onClose('websocket closed (code=' + ev.code + ')');
        if (!settled) { settled = true; reject(new Error('WebSocket closed before open')); }
      };
      ws.onmessage = function (ev) { self._onData(ev.data); };
    });
  };

  AcpClient.prototype._onData = function (data) {
    if (typeof data !== 'string') return;
    // The relay emits one JSON-RPC frame per message, but tolerate NDJSON:
    // buffer and split on newlines so a partial frame is never parsed.
    this._rxBuffer += data;
    var idx;
    while ((idx = this._rxBuffer.indexOf('\n')) !== -1) {
      var line = this._rxBuffer.slice(0, idx);
      this._rxBuffer = this._rxBuffer.slice(idx + 1);
      this._handleLine(line);
    }
    // No trailing newline: the relay sends whole frames, so flush the remainder.
    if (this._rxBuffer.trim().length && data.indexOf('\n') === -1) {
      var rest = this._rxBuffer;
      this._rxBuffer = '';
      this._handleLine(rest);
    }
  };

  AcpClient.prototype._handleLine = function (line) {
    var text = (line || '').trim();
    if (!text) return;
    var msg;
    try {
      msg = JSON.parse(text);
    } catch (e) {
      console.error('acp: bad JSON frame', text);
      return;
    }
    var hasId = msg.id !== undefined && msg.id !== null;
    if (hasId && msg.method === undefined) {
      this._handleResponse(msg);
    } else if (hasId && msg.method !== undefined) {
      this._handleRequest(msg.id, msg.method, msg.params || {});
    } else if (msg.method !== undefined) {
      this._handleNotification(msg.method, msg.params || {});
    }
  };

  AcpClient.prototype._handleResponse = function (msg) {
    var p = this._pending[msg.id];
    if (!p) return;
    delete this._pending[msg.id];
    if (p.timer) clearTimeout(p.timer);
    if (msg.error) {
      p.reject(new Error((msg.error && msg.error.message) || 'JSON-RPC error'));
    } else {
      p.resolve(msg.result);
    }
  };

  AcpClient.prototype._handleRequest = function (id, method, params) {
    if (method === 'session/request_permission') {
      // Hand off to the UI; it resolves via respondPermission(id, optionId).
      this._emit('permission', { requestId: id, params: params });
      return;
    }
    if (method === 'fs/read_text_file' || method === 'fs/write_text_file') {
      // The agent has a real filesystem in its container; we don't proxy it.
      this._sendError(id, JSONRPC_METHOD_NOT_FOUND, method + ' not available on this client');
      return;
    }
    this._sendError(id, JSONRPC_METHOD_NOT_FOUND, 'Method not found: ' + method);
  };

  AcpClient.prototype._handleNotification = function (method, params) {
    if (method === 'session/update') {
      this._emit('update', params);
    } else if (method === '_chat/error') {
      this._emit('error', (params && params.message) || 'unknown error');
    } else if (method === '_chat/notice') {
      // Non-fatal relay-side note (e.g. degraded MCP wiring); rendered as a
      // system line, the session keeps going.
      this._emit('notice', (params && params.message) || '');
    }
    // $/ping and anything else: ignore.
  };

  // -- outbound ---------------------------------------------------------------
  AcpClient.prototype._send = function (obj) {
    if (this.closed || !this.ws || this.ws.readyState !== WebSocket.OPEN) {
      throw new Error('ACP connection is not open');
    }
    this.ws.send(JSON.stringify(obj) + '\n');
  };

  AcpClient.prototype._request = function (method, params, timeoutMs) {
    var self = this;
    var id = this._nextId++;
    if (timeoutMs === undefined) timeoutMs = REQUEST_TIMEOUT_MS;
    return new Promise(function (resolve, reject) {
      var timer = timeoutMs > 0 ? setTimeout(function () {
        if (self._pending[id]) {
          delete self._pending[id];
          reject(new Error('Request timed out: ' + method));
        }
      }, timeoutMs) : null;
      self._pending[id] = { resolve: resolve, reject: reject, method: method, timer: timer };
      try {
        self._send({ jsonrpc: '2.0', id: id, method: method, params: params || {} });
      } catch (e) {
        delete self._pending[id];
        if (timer) clearTimeout(timer);
        reject(e);
      }
    });
  };

  AcpClient.prototype._notify = function (method, params) {
    this._send({ jsonrpc: '2.0', method: method, params: params || {} });
  };

  AcpClient.prototype._sendResult = function (id, result) {
    try { this._send({ jsonrpc: '2.0', id: id, result: result }); } catch (e) { /* closed */ }
  };
  AcpClient.prototype._sendError = function (id, code, message) {
    try { this._send({ jsonrpc: '2.0', id: id, error: { code: code, message: message } }); } catch (e) { /* closed */ }
  };

  // -- ACP methods (client -> agent) -----------------------------------------
  AcpClient.prototype.initialize = function () {
    return this._request('initialize', {
      protocolVersion: 1,
      clientCapabilities: { fs: { readTextFile: false, writeTextFile: false } }
    });
  };
  AcpClient.prototype.newSession = function (cwd) {
    var self = this;
    return this._request('session/new', { cwd: cwd, mcpServers: [] }).then(function (result) {
      if (result && result.sessionId) self._rememberConfigOptions(result.sessionId, result);
      return result;
    });
  };
  AcpClient.prototype.loadSession = function (sessionId, cwd) {
    var self = this;
    // No timeout: replaying a long transcript legitimately takes a while.
    return this._request('session/load', { sessionId: sessionId, cwd: cwd, mcpServers: [] }, 0)
      .then(function (result) {
        self._rememberConfigOptions(sessionId, result);
        return result;
      });
  };
  AcpClient.prototype.prompt = function (sessionId, content) {
    var prompt = Array.isArray(content)
      ? content
      : [{ type: 'text', text: String(content || '') }];
    // No timeout: resolves only when the agent's whole turn ends (stopReason).
    return this._request('session/prompt', {
      sessionId: sessionId,
      prompt: prompt
    }, 0);
  };
  AcpClient.prototype.cancel = function (sessionId) {
    try { this._notify('session/cancel', { sessionId: sessionId }); } catch (e) { /* closed */ }
  };
  AcpClient.prototype.setModel = function (sessionId, modelId) {
    var self = this;
    if (this._configOptionSessions[sessionId]) {
      return this._request('session/set_config_option', {
        sessionId: sessionId,
        configId: 'model',
        value: modelId
      }).then(function (result) {
        self._rememberConfigOptions(sessionId, result);
        return result;
      });
    }
    return this._request('session/set_model', { sessionId: sessionId, modelId: modelId });
  };
  AcpClient.prototype.setMode = function (sessionId, modeId) {
    return this._request('session/set_mode', { sessionId: sessionId, modeId: modeId });
  };

  // Answer a pending session/request_permission. optionId=null -> cancelled.
  AcpClient.prototype.respondPermission = function (requestId, optionId) {
    if (optionId) {
      this._sendResult(requestId, { outcome: { outcome: 'selected', optionId: optionId } });
    } else {
      this._sendResult(requestId, { outcome: { outcome: 'cancelled' } });
    }
  };

  AcpClient.prototype._rememberConfigOptions = function (sessionId, result) {
    var options = result && result.configOptions;
    if (!Array.isArray(options)) return;
    for (var i = 0; i < options.length; i++) {
      if (options[i] && (options[i].id === 'model' || options[i].category === 'model')) {
        this._configOptionSessions[sessionId] = true;
        return;
      }
    }
  };

  // -- lifecycle --------------------------------------------------------------
  AcpClient.prototype._startHeartbeat = function () {
    var self = this;
    this._stopHeartbeat();
    if (HEARTBEAT_MS <= 0) return;
    this._heartbeat = setInterval(function () {
      if (self.closed || !self.ws || self.ws.readyState !== WebSocket.OPEN) {
        self._stopHeartbeat();
        return;
      }
      try { self.ws.send('{"jsonrpc":"2.0","method":"$/ping"}\n'); } catch (e) { self._stopHeartbeat(); }
    }, HEARTBEAT_MS);
  };
  AcpClient.prototype._stopHeartbeat = function () {
    if (this._heartbeat) { clearInterval(this._heartbeat); this._heartbeat = null; }
  };

  AcpClient.prototype._onClose = function (reason) {
    if (this.closed) return;
    this.closed = true;
    this._stopHeartbeat();
    var ids = Object.keys(this._pending);
    for (var i = 0; i < ids.length; i++) {
      var p = this._pending[ids[i]];
      if (p.timer) clearTimeout(p.timer);
      try { p.reject(new Error('connection closed: ' + reason)); } catch (e) { /* ignore */ }
    }
    this._pending = {};
    this._emit('close', reason);
  };

  AcpClient.prototype.close = function () {
    this._stopHeartbeat();
    if (this.ws) {
      try { this.ws.close(1000, 'client closed'); } catch (e) { /* ignore */ }
    }
    this._onClose('client closed');
  };

  window.AcpClient = AcpClient;
})();
