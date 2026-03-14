# Host Mode UI Support — Change Report

## Summary
Add `host_mode` checkbox to service creation and restore forms in `dashboard.html`.
The backend (`web_ui.py`) already accepts `host_mode` as a boolean in the request body — only the frontend was missing.

## File
`src/oduflow/templates/dashboard.html`

## Changes (6 edits)

### 1. Create Service form — add checkbox before form-actions
**Location:** Inside `<div id="create-svc-modal">`, after the "Environment variables" textarea `form-group`, before `<div class="form-actions">`.

**Add this HTML block:**
```html
<div class="form-group" style="display:flex;align-items:center;gap:8px;">
  <input type="checkbox" id="svc-host-mode">
  <label for="svc-host-mode" style="margin-bottom:0;cursor:pointer;">Host network mode</label>
</div>
```

### 2. Restore Service form — add checkbox before form-actions
**Location:** Inside `<div id="restore-svc-modal">`, after the "Environment variables" textarea `form-group`, before `<div class="form-actions">`.

**Add this HTML block:**
```html
<div class="form-group" style="display:flex;align-items:center;gap:8px;">
  <input type="checkbox" id="restore-svc-host-mode">
  <label for="restore-svc-host-mode" style="margin-bottom:0;cursor:pointer;">Host network mode</label>
</div>
```

### 3. `openCreateServiceModal()` — reset checkbox on open
**Location:** In the JS function `openCreateServiceModal()`, after the line that clears `svc-envvars`.

**Add this line:**
```js
document.getElementById('svc-host-mode').checked = false;
```

### 4. `submitCreateService()` — send host_mode in payload
**Location:** In the JS function `submitCreateService()`, where `payload` is built.

**Replace:**
```js
var payload = { name: name, image: image, port: parseInt(port, 10) };
```
**With:**
```js
var hostMode = document.getElementById('svc-host-mode').checked;
var payload = { name: name, image: image, port: parseInt(port, 10), host_mode: hostMode };
```

### 5. `_fillRestoreFields(p)` — fill checkbox from preset data
**Location:** At the end of `_fillRestoreFields(p)`, after the line that sets `restore-svc-envvars`.

**Add this line:**
```js
document.getElementById('restore-svc-host-mode').checked = !!p.host_mode;
```

### 6. `openRestoreServiceModal()` — reset checkbox on open + `submitRestoreService()` sends host_mode
**Location A:** In `openRestoreServiceModal()`, after the line that clears `restore-svc-envvars`.

**Add this line:**
```js
document.getElementById('restore-svc-host-mode').checked = false;
```

**Location B:** In `submitRestoreService()`, where `payload` is built.

**Replace:**
```js
var payload = { name: name, image: image, port: parseInt(port, 10) };
```
**With:**
```js
var hostMode = document.getElementById('restore-svc-host-mode').checked;
var payload = { name: name, image: image, port: parseInt(port, 10), host_mode: hostMode };
```
