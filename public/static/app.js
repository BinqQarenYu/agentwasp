/* ============================================================
   Agent Wasp — Dashboard App JS
   Sparklines + Toast utilities
   ============================================================ */

/* ---- Sparkline Renderer ---- */
(function() {
  function renderSparkline(el) {
    var raw = el.getAttribute('data-sparkline');
    if (!raw) return;
    var data;
    try { data = JSON.parse(raw); } catch(e) { return; }
    if (!data || data.length < 2) return;

    var W = parseFloat(el.getAttribute('width') || 80);
    var H = parseFloat(el.getAttribute('height') || 28);
    var min = Math.min.apply(null, data);
    var max = Math.max.apply(null, data);
    var range = max - min || 1;
    var pad = 2;

    var pts = data.map(function(v, i) {
      var x = pad + (i / (data.length - 1)) * (W - pad * 2);
      var y = H - pad - ((v - min) / range) * (H - pad * 2);
      return x.toFixed(1) + ',' + y.toFixed(1);
    });

    var polyStr = pts.join(' ');
    var first = pts[0];
    var last  = pts[pts.length - 1];
    var fxStr = first.split(',')[0];
    var lxStr = last.split(',')[0];
    var areaStr = polyStr + ' ' + lxStr + ',' + (H - pad) + ' ' + fxStr + ',' + (H - pad);

    var line = el.querySelector('polyline');
    var area = el.querySelector('polygon');
    if (line) line.setAttribute('points', polyStr);
    if (area) area.setAttribute('points', areaStr);
  }

  function renderAll() {
    document.querySelectorAll('[data-sparkline]').forEach(renderSparkline);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', renderAll);
  } else {
    renderAll();
  }
})();

/* ---- Toast System (global) ---- */
window.WaspToast = (function() {
  var root = null;

  var ICONS = {
    success: '<svg viewBox="0 0 20 20" fill="currentColor" class="wt-icon"><path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd"/></svg>',
    error:   '<svg viewBox="0 0 20 20" fill="currentColor" class="wt-icon"><path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clip-rule="evenodd"/></svg>',
    warning: '<svg viewBox="0 0 20 20" fill="currentColor" class="wt-icon"><path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd"/></svg>',
    info:    '<svg viewBox="0 0 20 20" fill="currentColor" class="wt-icon"><path fill-rule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clip-rule="evenodd"/></svg>',
  };

  var CLOSE_ICON = '<svg viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clip-rule="evenodd"/></svg>';

  var DURATION = 3500;

  function getRoot() {
    if (!root || !document.body.contains(root)) {
      root = document.getElementById('wasp-toast-root');
      if (!root) {
        root = document.createElement('div');
        root.id = 'wasp-toast-root';
        document.body.appendChild(root);
      }
    }
    return root;
  }

  function dismiss(el) {
    if (el.dataset.dismissed) return;
    el.dataset.dismissed = '1';
    el.classList.add('wt-out');
    setTimeout(function() { if (el.parentNode) el.parentNode.removeChild(el); }, 320);
  }

  function show(message, type) {
    type = (type && ICONS[type]) ? type : 'info';
    var r = getRoot();

    var el = document.createElement('div');
    var LABELS = { success: 'Success', error: 'Error', warning: 'Warning', info: 'Info' };
    el.className = 'wt wt-' + type;
    el.setAttribute('role', 'alert');
    el.innerHTML =
      '<span class="wt-icon-wrap">' + ICONS[type] + '</span>' +
      '<div class="wt-content">' +
        '<span class="wt-label">' + (LABELS[type] || type) + '</span>' +
        '<span class="wt-body">' + String(message).replace(/</g, '&lt;') + '</span>' +
      '</div>' +
      '<button class="wt-close" aria-label="Dismiss">' + CLOSE_ICON + '</button>' +
      '<span class="wt-bar"></span>';

    el.querySelector('.wt-close').addEventListener('click', function(e) {
      e.stopPropagation();
      dismiss(el);
    });
    el.addEventListener('click', function() { dismiss(el); });

    r.appendChild(el);

    var timer = setTimeout(function() { dismiss(el); }, DURATION);

    // Pause on hover
    el.addEventListener('mouseenter', function() {
      clearTimeout(timer);
      var bar = el.querySelector('.wt-bar');
      if (bar) bar.classList.add('anim-paused');
    });
    el.addEventListener('mouseleave', function() {
      var bar = el.querySelector('.wt-bar');
      if (bar) bar.classList.remove('anim-paused');
      timer = setTimeout(function() { dismiss(el); }, 1200);
    });
  }

  return { show: show };
})();

/* backward compat */
function showToast(msg, type) { WaspToast.show(msg, type); }

/* ---- CSRF helper ---- */
function getCsrf() {
  var meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.content : '';
}

/* ---- Keyboard Shortcuts ---- */
(function() {
  var NAV = {
    '1': '/overview',
    '2': '/memory',
    '3': '/chat',
    '4': '/models',
    '5': '/skills',
    '6': '/scheduler',
    '7': '/health',
    '8': '/live',
    '9': '/audit',
    '0': '/metrics',
  };

  function isInputFocused() {
    var t = document.activeElement && document.activeElement.tagName;
    return t === 'INPUT' || t === 'TEXTAREA' || t === 'SELECT';
  }

  function openShortcutsModal() {
    var modal = document.getElementById('shortcuts-modal');
    if (modal) modal.classList.add('modal-open');
  }

  function closeShortcutsModal() {
    var modal = document.getElementById('shortcuts-modal');
    if (modal) modal.classList.remove('modal-open');
  }

  window.openShortcutsModal = openShortcutsModal;
  window.closeShortcutsModal = closeShortcutsModal;

  document.addEventListener('keydown', function(e) {
    // Ignore when typing in inputs
    if (isInputFocused()) return;
    // Ignore modifier combos
    if (e.ctrlKey || e.altKey || e.metaKey) return;

    var key = e.key;

    if (key === 'Escape') {
      closeShortcutsModal();
      return;
    }

    if (key === '?' || (key === '/' && e.shiftKey)) {
      e.preventDefault();
      openShortcutsModal();
      return;
    }

    if (key === 'r') {
      e.preventDefault();
      window.location.reload();
      return;
    }

    if (NAV[key]) {
      e.preventDefault();
      window.location.href = NAV[key];
      return;
    }
  });
})();

/* ---- Wasp Tooltip (global, fixed-position, never clipped) ---- */
(function() {
  var el = null;
  var timer = null;

  function getEl() {
    if (!el) {
      el = document.createElement('div');
      el.id = 'wasp-tip-el';
      document.body.appendChild(el);
    }
    return el;
  }

  function show(target) {
    var tip = target.getAttribute('data-tip');
    if (!tip) return;
    var t = getEl();
    t.textContent = tip;
    // Move off-screen first so we can measure dimensions without flash
    t.style.setProperty('--tip-left', '-9999px');
    t.style.setProperty('--tip-top', '-9999px');
    t.classList.add('tip-visible');

    var rect = target.getBoundingClientRect();
    var tw = t.offsetWidth;
    var th = t.offsetHeight;

    // Position above the target, centered
    var left = rect.left + rect.width / 2 - tw / 2;
    var top  = rect.top - th - 8;

    // Clamp to viewport
    left = Math.max(8, Math.min(left, window.innerWidth - tw - 8));
    if (top < 8) top = rect.bottom + 8; // flip below if no room above

    t.style.setProperty('--tip-left', left + 'px');
    t.style.setProperty('--tip-top', top + 'px');
  }

  function hide() {
    if (el) { el.classList.remove('tip-visible'); }
  }

  document.addEventListener('mouseover', function(e) {
    var target = e.target.closest('[data-tip]');
    if (target) {
      clearTimeout(timer);
      timer = setTimeout(function(){ show(target); }, 120);
    }
  });

  document.addEventListener('mouseout', function(e) {
    if (e.target.closest('[data-tip]')) {
      clearTimeout(timer);
      hide();
    }
  });

  document.addEventListener('scroll', hide, true);
})();

/* ============================================================
   WaspActions — Central Action Registry + Dispatcher
   Templates register handlers via:
     registerWaspActions("namespace", { 'actionName': function(el, e) { ... } });
   HTML elements use:
     data-action="namespace:actionName"  (fires on click/change/input/submit)
   Built-in actions use the "wasp" namespace (data-action="wasp:reload" etc.)
   Debug mode: set window.DEBUG_ACTIONS = true in console
   ============================================================ */
(function() {
  window.WaspActions = window.WaspActions || {};

  /* ---- Safe namespaced registration ---- */
  window.registerWaspActions = function(namespace, actionsObj) {
    if (!window.WaspActions) window.WaspActions = {};
    for (var key in actionsObj) {
      if (!Object.prototype.hasOwnProperty.call(actionsObj, key)) continue;
      var namespacedKey = namespace + ':' + key;
      if (window.WaspActions[namespacedKey]) {
        console.warn('[WaspActions] Already registered — skipping:', namespacedKey);
        continue;
      }
      window.WaspActions[namespacedKey] = actionsObj[key];
    }
  };

  /* ---- Built-in actions (namespace: "wasp") ---- */
  registerWaspActions('wasp', {
    'reload':              function()    { window.location.reload(); },
    'goBack':              function()    { window.history.back(); },
    'toggleTheme':         function()    { if (window.toggleTheme) window.toggleTheme(); },
    'togglePanel':         function(el)  {
      var target = el.dataset.target;
      if (target) {
        var panel = document.getElementById(target);
        if (panel) panel.classList.toggle('hidden');
      }
    },
    'openShortcutsModal':  function()    { if (window.openShortcutsModal)  window.openShortcutsModal(); },
    'closeShortcutsModal': function()    { if (window.closeShortcutsModal) window.closeShortcutsModal(); },
  });

  function _dispatch(el, e) {
    var action = el.dataset.action;
    if (window.DEBUG_ACTIONS) {
      var parts = action ? action.split(':') : [];
      var ns  = parts.length > 1 ? parts[0] : '(none)';
      var key = parts.length > 1 ? parts.slice(1).join(':') : action;
      console.log('[WaspActions]', e.type, '| ns:', ns, '| action:', key, '| el:', el);
    }
    var handler = window.WaspActions[action];
    if (handler) {
      handler(el, e);
    } else {
      console.warn('[WaspActions] Unknown action:', action);
    }
  }

  /* ---- Central click dispatcher ---- */
  document.addEventListener('click', function(e) {
    /* Backdrop-click close */
    if (e.target.hasAttribute && e.target.hasAttribute('data-close-on-backdrop-click')) {
      var closeAction = e.target.getAttribute('data-close-on-backdrop-click');
      if (closeAction && window.WaspActions[closeAction]) window.WaspActions[closeAction](e.target, e);
      return;
    }
    /* Dialog open/close — handled before data-action to allow early return */
    var opener = e.target.closest('[data-dialog-open]');
    if (opener) {
      var dlg = document.getElementById(opener.getAttribute('data-dialog-open'));
      if (dlg) dlg.showModal();
      return;
    }
    var closer = e.target.closest('[data-dialog-close]');
    if (closer) {
      var cid = closer.getAttribute('data-dialog-close');
      var dlg2 = cid ? document.getElementById(cid) : closer.closest('dialog');
      if (dlg2) dlg2.close();
      return;
    }
    /* data-action dispatch — skip checkboxes/radios; their actions fire on change */
    var el = e.target.closest('[data-action]');
    if (!el) return;
    if (el.type === 'checkbox' || el.type === 'radio') return;
    _dispatch(el, e);
  });

  /* ---- Central change dispatcher ---- */
  document.addEventListener('change', function(e) {
    var el = e.target.closest('[data-action]');
    if (!el) return;
    _dispatch(el, e);
  });

  /* ---- Central input dispatcher ---- */
  document.addEventListener('input', function(e) {
    var el = e.target.closest('[data-action]');
    if (!el) return;
    /* Checkboxes/radios fire change, not input — skip to avoid double dispatch */
    if (el.type === 'checkbox' || el.type === 'radio') return;
    _dispatch(el, e);
  });

  /* ---- Central submit dispatcher ---- */
  document.addEventListener('submit', function(e) {
    var el = e.target.closest('[data-action]');
    if (!el) return;
    _dispatch(el, e);
  });

  /* ---- Image error capture (error doesn't bubble — use capture phase) ----
     data-fallback-id="id"  → hide img, show element with that id
     data-hide-on-error     → hide img, show its next sibling               */
  document.addEventListener('error', function(e) {
    if (e.target.tagName !== 'IMG') return;
    var fbId = e.target.getAttribute('data-fallback-id');
    if (fbId) {
      e.target.classList.add('hidden');
      var fb = document.getElementById(fbId);
      if (fb) {
        fb.classList.remove('hidden');
        var disp = fb.getAttribute('data-fallback-display') || 'flex';
        if (disp === 'flex') fb.classList.add('flex');
        else if (disp === 'block') fb.classList.add('block');
      }
    } else if (e.target.hasAttribute('data-hide-on-error')) {
      e.target.classList.add('hidden');
      var sib = e.target.nextElementSibling;
      if (sib) { sib.classList.remove('hidden'); }
    }
  }, true);
})();

/* ---- Dynamic CSS Value Applier (data-dv → CSSOM, CSP-safe) ----
   Elements may have data-dv="prop1:val1;prop2:val2" set by Jinja.
   These are applied via el.style.setProperty() — never innerHTML/setAttribute.
   Called on initial load and after every SPA navigation.              */
window.applyDynStyles = function(root) {
  (root || document).querySelectorAll('[data-dv]').forEach(function(el) {
    var dv = el.getAttribute('data-dv');
    if (!dv) return;
    dv.split(';').forEach(function(decl) {
      decl = decl.trim();
      if (!decl) return;
      var idx = decl.indexOf(':');
      if (idx < 0) return;
      var prop = decl.slice(0, idx).trim();
      var val  = decl.slice(idx + 1).trim();
      if (prop && val) el.style.setProperty(prop, val);
    });
  });
};

(function() {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function() { window.applyDynStyles(document); });
  } else {
    window.applyDynStyles(document);
  }
})();
