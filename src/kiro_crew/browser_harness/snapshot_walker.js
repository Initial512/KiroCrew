// The page-side half of the `browser` MCP tool: ONE script, evaluated in the
// agent's tab through the Harness `js` helper, that installs `window.__kcWalker`
// once per document and returns it. `browser_harness/ops.py` appends a method
// call to this source (`<source>.snapshot()`), so every page-side read and
// write the tool performs is a method on the object this file builds. Nothing
// else is ever evaluated in the page.
//
// Moved from `website/electron/browser-ops.js` (the native panel's walker) when
// browsing moved to Browser Harness. The ref layer is OURS: `snapshot` mints
// `eN` refs into `window.__kcRefs` and the other methods resolve them back to
// live elements. Refs live as long as the Document object: a real navigation
// swaps the document (and this whole realm), an SPA route change does not.
//
// Every method returns a JSON-serialisable value. A refusal is a return value
// (`{ok:false, code}`), never a throw, so Python can tell "the page said no"
// from a transport failure.
(() => {
  var NAME_CAP = 120;
  var CONSOLE_CAP = 500;
  // One console entry's text; a page that logs a megabyte string must not make the
  // worker's reply line a megabyte. The gateway caps the whole outline at 20,000
  // characters, so a snapshot past NODE_CAP nodes carries nothing it would show.
  var CONSOLE_TEXT_CAP = 2000;
  var NODE_CAP = 1500;
  var INTERACTIVE_TAGS = { a: true, button: true, input: true, select: true, textarea: true };
  var INTERACTIVE_ROLES = {
    button: 1, link: 1, checkbox: 1, radio: 1, tab: 1, menuitem: 1, option: 1,
    "switch": 1, combobox: 1, textbox: 1
  };
  var SECRET_NAME_RE = /(^|[^a-z])(password|passwd|pwd|otp|totp|mfa|cvv|cvc|secret|token|apikey)([^a-z]|$)/i;

  if (window.__kcRefDocObj !== document || !(window.__kcRefs instanceof Map)) {
    window.__kcRefs = new Map();
    window.__kcRefSeq = 0;
    window.__kcRefDocObj = document;
  }
  var refs = window.__kcRefs;
  // Refs are strong references; drop the ones whose element left the DOM so an
  // SPA that re-renders for hours does not pin every element ever walked.
  refs.forEach(function (el, r) { if (!el || !el.isConnected) refs.delete(r); });

  function visible(el) {
    var style = window.getComputedStyle(el);
    if (!style) return false;
    if (style.display === "none" || style.visibility === "hidden") return false;
    var rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return false;
    if (el.offsetParent === null && style.position !== "fixed") return false;
    return true;
  }

  function explicitRole(el) {
    var r = el.getAttribute && el.getAttribute("role");
    return r ? r.trim().toLowerCase() : "";
  }

  function inputType(el) {
    return ((el.tagName || "").toLowerCase() === "input")
      ? String(el.getAttribute("type") || "text").toLowerCase()
      : "";
  }

  function roleFor(el) {
    var role = explicitRole(el);
    if (role) return role;
    var tag = el.tagName.toLowerCase();
    if (tag === "a") return el.hasAttribute("href") ? "link" : "";
    if (tag === "button") return "button";
    if (tag === "select") return "combobox";
    if (tag === "textarea") return "textbox";
    if (tag === "input") {
      var t = inputType(el);
      if (t === "checkbox") return "checkbox";
      if (t === "radio") return "radio";
      if (t === "button" || t === "submit" || t === "reset") return "button";
      if (t === "hidden") return "";
      return "textbox";
    }
    if (/^h[1-6]$/.test(tag)) return "heading";
    return "";
  }

  function interactive(el, role) {
    var tag = el.tagName.toLowerCase();
    if (tag === "a") return el.hasAttribute("href");
    if (INTERACTIVE_TAGS[tag]) {
      if (tag === "input" && inputType(el) === "hidden") return false;
      return true;
    }
    if (el.isContentEditable) return true;
    var ti = el.getAttribute && el.getAttribute("tabindex");
    if (ti !== null && ti !== undefined && ti !== "-1" && ti !== "") return true;
    if (role && INTERACTIVE_ROLES[role]) return true;
    return false;
  }

  // A field whose VALUE must never reach the model: the type says so, or the
  // name/id/autocomplete does. The snapshot drops its value and `type` refuses it.
  function secretish(el) {
    if (inputType(el) === "password") return true;
    return SECRET_NAME_RE.test(
      (el.getAttribute("name") || "") + " " + (el.getAttribute("id") || "") + " " +
      (el.getAttribute("autocomplete") || "")
    );
  }

  function labelText(el) {
    var lb = el.getAttribute && el.getAttribute("aria-labelledby");
    if (lb) {
      var parts = lb.split(/\s+/).map(function (id) {
        var n = document.getElementById(id);
        return n ? (n.textContent || "").trim() : "";
      }).filter(Boolean);
      if (parts.length) return parts.join(" ");
    }
    var al = el.getAttribute && el.getAttribute("aria-label");
    if (al && al.trim()) return al.trim();
    if (el.id) {
      var sel = "label[for=\"" + (window.CSS && CSS.escape ? CSS.escape(el.id) : el.id) + "\"]";
      var lab = document.querySelector(sel);
      if (lab && (lab.textContent || "").trim()) return lab.textContent.trim();
    }
    if (el.closest) {
      var wrap = el.closest("label");
      if (wrap && (wrap.textContent || "").trim()) return wrap.textContent.trim();
    }
    var secret = secretish(el);
    var attrs = secret ? ["alt", "title", "placeholder"] : ["alt", "title", "placeholder", "value"];
    for (var i = 0; i < attrs.length; i++) {
      var v = el.getAttribute && el.getAttribute(attrs[i]);
      if (v && v.trim()) return v.trim();
    }
    if (secret) return "";
    return (el.textContent || "").replace(/\s+/g, " ").trim();
  }

  function accName(el) {
    var n = labelText(el);
    if (n.length > NAME_CAP) n = n.slice(0, NAME_CAP) + "\u2026";
    return n;
  }

  function stateOf(el, role) {
    var st = {};
    if (el.disabled === true || (el.getAttribute && el.getAttribute("aria-disabled") === "true")) st.disabled = true;
    var ac = el.getAttribute && el.getAttribute("aria-checked");
    if (ac === "true") st.checked = true;
    else if ((role === "checkbox" || role === "radio") && el.checked === true) st.checked = true;
    var ae = el.getAttribute && el.getAttribute("aria-expanded");
    if (ae === "true") st.expanded = true;
    else if (ae === "false") st.expanded = false;
    if (inputType(el) === "password") st.password = true;
    return st;
  }

  function assignRef(el) {
    if (el.__kcRef && refs.get(el.__kcRef) === el) return el.__kcRef;
    var r = "e" + (++window.__kcRefSeq);
    try { Object.defineProperty(el, "__kcRef", { value: r, configurable: true }); }
    catch (e) { try { el.__kcRef = r; } catch (e2) {} }
    refs.set(r, el);
    return r;
  }

  function lookup(ref) {
    var el = refs.get(ref);
    return el && el.isConnected ? el : null;
  }

  // The element that really holds focus. `document.activeElement` stops at a
  // shadow HOST (a web-component login form), which is a <div> and never a
  // password input; the snapshot walks open shadow roots, so the fields inside
  // them get refs and must be judged by the same rule. Follow
  // `shadowRoot.activeElement` down as far as it goes (closed roots cannot be
  // entered and stop at the host, which the walker also never descends).
  function deepActive() {
    var a = document.activeElement;
    var guard = 0;
    while (a && a.shadowRoot && a.shadowRoot.activeElement && guard++ < 32) {
      a = a.shadowRoot.activeElement;
    }
    return a;
  }

  // Console capture: the Harness daemon drains CDP events into one shared
  // buffer, so console output is read in the page instead. The hook wraps the
  // console methods once per document and keeps the last CONSOLE_CAP entries.
  function installConsoleHook() {
    if (window.__kcConsoleDoc === document && Array.isArray(window.__kcConsole)) return;
    window.__kcConsole = [];
    window.__kcConsoleDoc = document;
    var buf = window.__kcConsole;
    function push(level, args) {
      var text = Array.prototype.map.call(args, function (a) {
        if (typeof a === "string") return a;
        try { return JSON.stringify(a); } catch (e) { return String(a); }
      }).join(" ");
      if (text.length > CONSOLE_TEXT_CAP) text = text.slice(0, CONSOLE_TEXT_CAP) + "\u2026";
      buf.push({ source: "console", level: level, text: text, ts: Date.now() });
      if (buf.length > CONSOLE_CAP) buf.splice(0, buf.length - CONSOLE_CAP);
    }
    ["log", "info", "warn", "error", "debug"].forEach(function (level) {
      var orig = console[level];
      if (typeof orig !== "function" || orig.__kcWrapped) return;
      var wrapped = function () { push(level, arguments); return orig.apply(console, arguments); };
      wrapped.__kcWrapped = true;
      try { console[level] = wrapped; } catch (e) {}
    });
    window.addEventListener("error", function (ev) {
      push("error", [String(ev && ev.message || "error")]);
    });
    window.addEventListener("unhandledrejection", function (ev) {
      push("error", ["Unhandled promise rejection: " + String(ev && ev.reason)]);
    });
  }

  var api = {
    snapshot: function () {
      installConsoleHook();
      var out = [];
      function emit(el, depth) {
        var role = roleFor(el);
        var name = accName(el);
        if (interactive(el, role)) {
          out.push({ depth: depth, role: role || "generic", name: name, ref: assignRef(el), state: stateOf(el, role) });
          return depth + 1;
        }
        if (role === "heading" && name) {
          out.push({ depth: depth, role: "heading", name: name });
          return depth + 1;
        }
        return depth;
      }
      var truncated = false;
      function walk(root, depth) {
        var kids = root.children || [];
        for (var i = 0; i < kids.length; i++) {
          if (out.length >= NODE_CAP) { truncated = true; return; }
          var el = kids[i];
          if (!el || el.nodeType !== 1) continue;
          var tag = el.tagName.toLowerCase();
          if (tag === "script" || tag === "style" || tag === "noscript" || tag === "template" || tag === "head") continue;
          if (!visible(el)) continue;
          var next = emit(el, depth);
          walk(el, next);
          if (el.shadowRoot) walk(el.shadowRoot, next);
        }
      }
      try {
        walk(document.body || document.documentElement, 0);
      } catch (e) {
        return { url: location.href, title: document.title, nodes: out, error: String(e && e.message) };
      }
      var result = { url: location.href, title: document.title, nodes: out };
      if (truncated) result.error = "page has more than " + NODE_CAP + " elements; the outline stops there";
      return result;
    },

    // Scroll a ref into view and return its centre in CSS pixels.
    resolve: function (ref) {
      var el = lookup(ref);
      if (!el) return { ok: false, code: "ref_stale" };
      try { el.scrollIntoView({ block: "center", inline: "center" }); } catch (e) {}
      var r = el.getBoundingClientRect();
      if (!r || (r.width === 0 && r.height === 0)) return { ok: false, code: "ref_stale" };
      return {
        ok: true,
        x: r.left + r.width / 2,
        y: r.top + r.height / 2,
        tag: (el.tagName || "").toLowerCase(),
        password: inputType(el) === "password"
      };
    },

    // Focus a ref and report whether focus actually landed and what it is.
    focus: function (ref) {
      var el = lookup(ref);
      if (!el) return { ok: false, code: "ref_stale" };
      if (inputType(el) === "password") return { ok: false, code: "password_field" };
      try { el.focus(); } catch (e) {}
      var active = deepActive();
      return {
        ok: true,
        focused: active === el,
        activeTag: (active && active.tagName || "").toLowerCase(),
        disabled: !!el.disabled,
        readOnly: !!el.readOnly
      };
    },

    // What holds focus now: the `type` op without a ref types into it.
    activeField: function () {
      var active = deepActive();
      if (!active || active === document.body) return { focused: false, password: false, tag: "" };
      return {
        focused: true,
        password: inputType(active) === "password",
        tag: (active.tagName || "").toLowerCase()
      };
    },

    selectOption: function (ref, values) {
      var el = lookup(ref);
      if (!el) return { ok: false, code: "ref_stale" };
      if ((el.tagName || "").toLowerCase() !== "select") return { ok: false, code: "not_select" };
      var set = {};
      for (var i = 0; i < values.length; i++) set[values[i]] = true;
      var selected = [];
      for (var j = 0; j < el.options.length; j++) {
        var opt = el.options[j];
        var match = !!(set[opt.value] || set[opt.label] || set[(opt.textContent || "").trim()]);
        opt.selected = match;
        if (match) selected.push(opt.value);
      }
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return { ok: true, selected: selected };
    },

    textPresent: function (text) {
      var b = document.body;
      return !!(b && (b.innerText || "").indexOf(text) !== -1);
    },

    // Navigation commit tracking: stamp the CURRENT document, then poll
    // `state` until the stamp is gone (a new document) or the href matches.
    stamp: function (token) {
      window.__kcNavToken = token;
      return true;
    },
    state: function () {
      return {
        readyState: String(document.readyState),
        stale: window.__kcNavToken || "",
        href: String(location.href),
        title: String(document.title)
      };
    },

    back: function () {
      if (window.history.length <= 1) return { ok: false, code: "no_history" };
      window.history.back();
      return { ok: true };
    },

    consoleMessages: function (limit) {
      installConsoleHook();
      var buf = window.__kcConsole || [];
      return buf.slice(Math.max(0, buf.length - limit));
    },

    // How many password inputs have a box on the page, by the live view's mask
    // rule (screencast.py `_MASK_JS`): any `input[type=password]` with a non-empty
    // box, open shadow roots included to the same depth, plus the documents of
    // same-origin frames -- and any filled field `secretish` names (an OTP, a card
    // code, a token), whose value the outline also withholds. The decision hooks
    // send no screenshot while this is not 0.
    passwordBoxes: function () {
      var n = 0;
      function walkRoot(root, d) {
        var found = root.querySelectorAll("input, textarea");
        for (var i = 0; i < found.length; i++) {
          var el = found[i];
          var guarded = inputType(el) === "password" || (secretish(el) && !!el.value);
          if (!guarded) continue;
          var b = el.getBoundingClientRect();
          if (b.width > 0 && b.height > 0) n++;
        }
        if (d >= 8) return;
        var all = root.querySelectorAll("*");
        for (var j = 0; j < all.length; j++) {
          if (all[j].shadowRoot) walkRoot(all[j].shadowRoot, d + 1);
          var tag = (all[j].tagName || "").toLowerCase();
          if (tag === "iframe" || tag === "frame") {
            var inner = null;
            try { inner = all[j].contentDocument; } catch (e) { inner = null; }
            if (inner) walkRoot(inner, d + 1);
          }
        }
      }
      walkRoot(document, 0);
      return n;
    },

    // Visible elements matching a CSS selector, as {t, x, y} (short text and the
    // box centre in CSS pixels): the `reflex` op's game/grid state. Read-only;
    // form fields are skipped so a typed value is never read back. A selector the
    // page rejects answers {ok:false}.
    cells: function (selector) {
      var found;
      try { found = document.querySelectorAll(String(selector)); } catch (e) { return { ok: false, code: "bad_selector" }; }
      var out = [];
      for (var i = 0; i < found.length && out.length < 400; i++) {
        var el = found[i];
        var tag = (el.tagName || "").toLowerCase();
        if (tag === "input" || tag === "textarea" || tag === "select") continue;
        if (!visible(el)) continue;
        var b = el.getBoundingClientRect();
        out.push({ t: (el.textContent || "").trim().slice(0, 40), x: b.left + b.width / 2, y: b.top + b.height / 2 });
      }
      return { ok: true, cells: out };
    },

    // Boxes of visible <canvas> elements in the viewport, CSS pixels
    // [left, top, width, height]: the only targets the screenshot locator may
    // click, because the outline cannot see inside them.
    canvasRects: function () {
      var out = [];
      var rects = { w: window.innerWidth, h: window.innerHeight, rects: out };
      var all = document.querySelectorAll("canvas");
      for (var i = 0; i < all.length && out.length < 32; i++) {
        var el = all[i];
        if (!visible(el)) continue;
        var b = el.getBoundingClientRect();
        if (b.width < 16 || b.height < 16) continue;
        if (b.right <= 0 || b.bottom <= 0 || b.left >= window.innerWidth || b.top >= window.innerHeight) continue;
        out.push([b.left, b.top, b.width, b.height]);
      }
      return rects;
    }
  };
  return api;
})()
