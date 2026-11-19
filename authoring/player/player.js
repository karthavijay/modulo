/* Modulo course player.
 * Reads window.COURSE (from course.js) and plays it in the browser.
 * Talks to the LMS through SCORM 1.2 (window.API) or SCORM 2004 (window.API_1484_11)
 * when it finds one; otherwise it saves progress in this browser (preview mode).
 */
(function () {
  "use strict";
  var C = window.COURSE;
  var VERSION = window.MODULO_SCORM || "auto"; // "1.2", "2004", "none" or "auto"
  if (!C) { document.body.textContent = "Course data is missing (course.js)."; return; }

  // ------------------------------------------------------------------ course model
  var S = C.settings || {};
  var PASS = Number(S.pass_mark == null ? 80 : S.pass_mark);
  var SEQUENTIAL = S.navigation === "sequential";
  var SHOW_QUIZ_FB = S.quiz_feedback !== false;
  var MAX_ATTEMPTS = Number(S.max_attempts || 0);
  var pages = [], scored = [], blockPage = {};
  (C.sections || []).forEach(function (sec) {
    (sec.pages || []).forEach(function (p) {
      p._section = sec; p._index = pages.length; pages.push(p);
      (p.blocks || []).forEach(function (b) {
        blockPage[b.id] = p._index;
        if (b.type === "question" && b.scored) scored.push(b);
      });
    });
  });
  if (!pages.length) { document.body.textContent = "This course has no pages yet."; return; }

  // ------------------------------------------------------------------ state
  var state = { v: [], a: {}, n: 1, loc: 0, max: 0 };
  var visited = {};

  // ------------------------------------------------------------------ LMS
  var lms = (function () {
    function find(win, name) {
      var tries = 0;
      try {
        while (win && !win[name] && win.parent && win.parent !== win && tries < 12) { tries++; win = win.parent; }
        return (win && win[name]) || null;
      } catch (e) { return null; }
    }
    function look(name) {
      var api = find(window, name);
      if (!api && window.opener) { try { api = find(window.opener, name); } catch (e) { api = null; } }
      return api;
    }
    var api = null, ver = null;
    if (VERSION === "2004" || VERSION === "auto") { api = look("API_1484_11"); if (api) ver = "2004"; }
    if (!api && (VERSION === "1.2" || VERSION === "auto")) { api = look("API"); if (api) ver = "1.2"; }
    var started = Date.now(), finished = false, dirty = false;
    var K12 = { loc: "cmi.core.lesson_location", susp: "cmi.suspend_data", entry: "cmi.core.entry" };
    var K04 = { loc: "cmi.location", susp: "cmi.suspend_data", entry: "cmi.entry" };
    var K = ver === "2004" ? K04 : K12;
    function call(fn12, fn04, args) {
      if (!api) return "";
      try { var f = api[ver === "2004" ? fn04 : fn12]; return f ? String(f.apply(api, args)) : ""; } catch (e) { return ""; }
    }
    function get(k) { return call("LMSGetValue", "GetValue", [k]); }
    function set(k, v) { dirty = true; return call("LMSSetValue", "SetValue", [k, String(v)]); }
    function commit() { if (dirty) { dirty = false; call("LMSCommit", "Commit", [""]); } }
    function pad(n, w) { n = String(n); while (n.length < w) n = "0" + n; return n; }
    function sessionTime() {
      var s = Math.max(0, Math.round((Date.now() - started) / 1000));
      var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
      return ver === "2004" ? "PT" + h + "H" + m + "M" + sec + "S" : pad(h, 4) + ":" + pad(m, 2) + ":" + pad(sec, 2);
    }
    return {
      version: ver,
      start: function () {
        if (!api) return false;
        var ok = call("LMSInitialize", "Initialize", [""]);
        if (ok !== "true" && ok !== "1") { api = null; return false; }
        return true;
      },
      load: function () {
        if (!api) return null;
        var raw = get(K.susp);
        var data = null;
        if (raw) { try { data = JSON.parse(raw); } catch (e) { data = null; } }
        var loc = parseInt(get(K.loc), 10);
        if (data && !isNaN(loc)) data.loc = loc;
        return data;
      },
      save: function (snapshot, loc) {
        if (!api) return;
        set(K.susp, snapshot);
        set(K.loc, String(loc));
      },
      report: function (r) {
        if (!api) return;
        if (ver === "2004") {
          set("cmi.progress_measure", r.progress.toFixed(2));
          set("cmi.completion_status", r.completed ? "completed" : "incomplete");
          if (r.hasQuiz) {
            set("cmi.success_status", r.score == null ? "unknown" : (r.passed ? "passed" : "failed"));
            if (r.score != null) {
              set("cmi.score.min", "0"); set("cmi.score.max", "100");
              set("cmi.score.raw", String(r.score)); set("cmi.score.scaled", (r.score / 100).toFixed(2));
            }
          }
        } else {
          // With a quiz: incomplete until every scored question is answered, then passed or failed.
          var st = r.hasQuiz ? (r.score == null ? "incomplete" : (r.passed ? "passed" : "failed"))
                             : (r.completed ? "completed" : "incomplete");
          set("cmi.core.lesson_status", st);
          if (r.score != null) {
            set("cmi.core.score.min", "0"); set("cmi.core.score.max", "100"); set("cmi.core.score.raw", String(r.score));
          }
        }
      },
      interaction: function (q, chosen, correct) {
        if (!api) return;
        var n = parseInt(get("cmi.interactions._count"), 10); if (isNaN(n)) n = 0;
        var p = "cmi.interactions." + n + ".";
        var letters = function (arr) { return arr.map(function (i) { return "abcdefghijklmnopqrstuvwxyz".charAt(i); }); };
        var right = []; q.options.forEach(function (o, i) { if (o.correct) right.push(i); });
        var id = ("q_" + q.id).replace(/[^A-Za-z0-9_\-]/g, "_");
        set(p + "id", id);
        set(p + "type", "choice");
        if (ver === "2004") {
          set(p + "description", (q.prompt || "").slice(0, 250));
          set(p + "learner_response", letters(chosen).join("[,]"));
          set(p + "correct_responses.0.pattern", letters(right).join("[,]"));
          set(p + "result", correct ? "correct" : "incorrect");
        } else {
          set(p + "student_response", letters(chosen).join(","));
          set(p + "correct_responses.0.pattern", letters(right).join(","));
          set(p + "result", correct ? "correct" : "wrong");
        }
      },
      commit: commit,
      finish: function (done) {
        if (!api || finished) return;
        finished = true;
        if (ver === "2004") { set("cmi.session_time", sessionTime()); set("cmi.exit", done ? "normal" : "suspend"); }
        else { set("cmi.core.session_time", sessionTime()); set("cmi.core.exit", done ? "" : "suspend"); }
        commit();
        call("LMSFinish", "Terminate", [""]);
      }
    };
  })();

  var connected = lms.start();
  var storeKey = "modulo:" + (C.id || C.title) + ":" + (C.revision || "");

  function packVisited() {
    var idx = Object.keys(visited).map(Number).sort(function (a, b) { return a - b; });
    var out = [], i = 0;
    while (i < idx.length) {
      var j = i; while (j + 1 < idx.length && idx[j + 1] === idx[j] + 1) j++;
      out.push(i === j ? String(idx[i]) : idx[i] + "-" + idx[j]); i = j + 1;
    }
    return out.join(",");
  }
  function unpackVisited(s) {
    visited = {};
    String(s || "").split(",").forEach(function (part) {
      if (!part) return;
      var ab = part.split("-").map(Number);
      for (var k = ab[0]; k <= (ab.length > 1 ? ab[1] : ab[0]); k++) if (k >= 0 && k < pages.length) visited[k] = true;
    });
  }
  function snapshot() {
    return JSON.stringify({ v: packVisited(), a: state.a, n: state.n, m: state.max });
  }
  function restore(d) {
    if (!d) return;
    unpackVisited(d.v);
    state.a = d.a && typeof d.a === "object" ? d.a : {};
    state.n = d.n || 1;
    state.max = Math.min(d.m || 0, pages.length - 1);
    state.loc = Math.min(Math.max(0, d.loc || 0), pages.length - 1);
  }
  var saveTimer = null;
  function persist() {
    if (connected) {
      lms.save(snapshot(), state.loc);
      lms.report(evaluate());
      clearTimeout(saveTimer); saveTimer = setTimeout(lms.commit, 400);
    } else {
      try { localStorage.setItem(storeKey, JSON.stringify({ d: snapshot(), loc: state.loc })); } catch (e) { /* private mode */ }
    }
  }
  if (connected) restore(lms.load());
  else {
    try { var saved = JSON.parse(localStorage.getItem(storeKey) || "null"); if (saved) { var d = JSON.parse(saved.d); d.loc = saved.loc; restore(d); } } catch (e) { /* ignore */ }
  }

  // ------------------------------------------------------------------ scoring
  function isCorrect(q, chosen) {
    var right = []; q.options.forEach(function (o, i) { if (o.correct) right.push(i); });
    if (right.length !== chosen.length) return false;
    for (var i = 0; i < right.length; i++) if (chosen.indexOf(right[i]) < 0) return false;
    return true;
  }
  function evaluate() {
    var n = Object.keys(visited).length;
    var answered = 0, right = 0;
    scored.forEach(function (q) { var a = state.a[q.id]; if (a) { answered++; if (isCorrect(q, a)) right++; } });
    var hasQuiz = scored.length > 0;
    var score = hasQuiz && answered === scored.length ? Math.round((right / scored.length) * 100) : null;
    var passed = score != null && score >= PASS;
    var allSeen = n >= pages.length;
    return { hasQuiz: hasQuiz, answered: answered, right: right, score: score, passed: passed,
             progress: n / pages.length, completed: hasQuiz ? (passed || (allSeen && score != null)) : allSeen,
             finished: hasQuiz ? passed || (score != null && attemptsLeft() === 0) : allSeen };
  }
  function attemptsLeft() { return MAX_ATTEMPTS ? Math.max(0, MAX_ATTEMPTS - state.n) : Infinity; }

  // ------------------------------------------------------------------ rendering helpers
  function el(tag, attrs, kids) {
    var e = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (k === "text") e.textContent = attrs[k];
      else if (k === "html") e.innerHTML = attrs[k];
      else if (k.slice(0, 2) === "on") e.addEventListener(k.slice(2), attrs[k]);
      else if (attrs[k] !== false && attrs[k] != null) e.setAttribute(k, attrs[k] === true ? "" : attrs[k]);
    });
    (kids || []).forEach(function (k) { if (k) e.appendChild(typeof k === "string" ? document.createTextNode(k) : k); });
    return e;
  }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  function inline(s) {
    return esc(s).replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  }
  // A small, safe subset of Markdown: paragraphs, ### headings, - lists, 1. lists, **bold**, *italic*.
  function md(src) {
    var out = [], list = null, para = [];
    function flushPara() { if (para.length) { out.push("<p>" + para.map(inline).join("<br>") + "</p>"); para = []; } }
    function flushList() { if (list) { out.push("<" + list.tag + ">" + list.items.map(function (i) { return "<li>" + inline(i) + "</li>"; }).join("") + "</" + list.tag + ">"); list = null; } }
    String(src || "").split(/\r?\n/).forEach(function (line) {
      var t = line.trim(), m;
      if (!t) { flushPara(); flushList(); return; }
      if ((m = t.match(/^#{2,4}\s+(.*)$/))) { flushPara(); flushList(); out.push("<h3>" + inline(m[1]) + "</h3>"); return; }
      if ((m = t.match(/^[-*•]\s+(.*)$/))) { flushPara(); if (!list || list.tag !== "ul") { flushList(); list = { tag: "ul", items: [] }; } list.items.push(m[1]); return; }
      if ((m = t.match(/^\d+[.)]\s+(.*)$/))) { flushPara(); if (!list || list.tag !== "ol") { flushList(); list = { tag: "ol", items: [] }; } list.items.push(m[1]); return; }
      flushList(); para.push(t);
    });
    flushPara(); flushList();
    return out.join("");
  }
  function announce(msg) { var r = document.getElementById("announcer"); r.textContent = ""; setTimeout(function () { r.textContent = msg; }, 50); }

  // Small line icons (24px grid, drawn with the current text colour).
  var ICONS = {
    menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
    left: '<path d="M15 18l-6-6 6-6"/>',
    right: '<path d="M9 6l6 6-6 6"/>',
    chev: '<path d="M6 9l6 6 6-6"/>',
    check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    cross: '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    lock: '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 7.6v.4"/>',
    tip: '<path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2.1h5c0-.9.4-1.6 1-2.1A6 6 0 0 0 12 3z"/>',
    warning: '<path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17v.4"/>',
    image: '<rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="10" r="1.8"/><path d="M21 16l-5-5-8 8"/>',
    book: '<path d="M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2z"/><path d="M4 19V5"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    layers: '<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 13l9 5 9-5"/>',
    target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
    spark: '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M6 18l2.5-2.5M15.5 8.5L18 6"/>',
    flag: '<path d="M5 21V4M5 4h11l-2 4 2 4H5"/>',
    help: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6v.6M12 17v.4"/>',
    award: '<circle cx="12" cy="9" r="6"/><path d="M8.5 14L7 21l5-3 5 3-1.5-7"/>'
  };
  function icon(name, cls) {
    return '<svg class="ico' + (cls ? " " + cls : "") + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' + ICONS[name] + "</svg>";
  }
  function iconEl(name, cls) { var s = el("span", { html: icon(name, cls) }); return s.firstChild; }

  // ------------------------------------------------------------------ brand colour
  function hexRgb(h) { return [1, 3, 5].map(function (i) { return parseInt(h.substr(i, 2), 16); }); }
  function rgbHex(c) { return "#" + c.map(function (v) { v = Math.round(Math.max(0, Math.min(255, v))).toString(16); return v.length < 2 ? "0" + v : v; }).join(""); }
  function mix(a, b, t) { var x = hexRgb(a), y = hexRgb(b); return rgbHex(x.map(function (v, i) { return v + (y[i] - v) * t; })); }
  function lum(h) { return hexRgb(h).map(function (v) { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }).reduce(function (s, v, i) { return s + v * [0.2126, 0.7152, 0.0722][i]; }, 0); }
  function contrast(a, b) { var x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); }
  var ACCENT = S.accent && /^#[0-9a-f]{6}$/i.test(S.accent) ? S.accent : "#2f3e9e";
  var INK_DARK = contrast(ACCENT, "#ffffff") < 3;
  (function () {
    var css = document.documentElement.style, textL = ACCENT, k = 0;
    while (contrast(textL, "#ffffff") < 4.6 && k++ < 12) textL = mix(textL, "#000000", 0.15);
    var textD = mix(ACCENT, "#ffffff", 0.5); k = 0;
    while (contrast(textD, "#171a20") < 5 && k++ < 12) textD = mix(textD, "#ffffff", 0.2);
    css.setProperty("--accent", ACCENT);
    css.setProperty("--accent-ink", INK_DARK ? "#111318" : "#ffffff");
    css.setProperty("--a-soft-l", mix(ACCENT, "#ffffff", 0.9));
    css.setProperty("--a-line-l", mix(ACCENT, "#ffffff", 0.68));
    css.setProperty("--a-text-l", textL);
    css.setProperty("--a-soft-d", mix(ACCENT, "#171a20", 0.8));
    css.setProperty("--a-line-d", mix(ACCENT, "#171a20", 0.52));
    css.setProperty("--a-text-d", textD);
  })();

  // ------------------------------------------------------------------ course facts (for the cover)
  function words(s) { return String(s || "").split(/\s+/).filter(Boolean).length; }
  var minutes = (function () {
    var w = 0, q = 0;
    pages.forEach(function (p) {
      (p.blocks || []).forEach(function (b) {
        w += words(b.text) + words(b.prompt) + words(b.caption);
        (b.items || []).forEach(function (it) { w += words(it.title) + words(it.text); });
        (b.options || []).forEach(function (o) { w += words(o.text); });
        if (b.type === "question") q++;
        if (b.type === "image") w += 20;
      });
    });
    var m = w / 200 + q * 0.5;
    return m < 10 ? Math.max(1, Math.round(m)) : Math.round(m / 5) * 5;
  })();
  var sectionList = (C.sections || []).filter(function (s) { return (s.pages || []).length; });

  // ------------------------------------------------------------------ shell
  var root = document.getElementById("app");
  document.documentElement.lang = C.language || "en";
  document.title = C.title || "Course";

  var progressFill = el("div", { "class": "fill" });
  var progressBar = el("div", { "class": "topbar-progress", role: "progressbar", "aria-label": "Course progress", "aria-valuemin": "0", "aria-valuemax": "100" }, [progressFill]);
  var pctLbl = el("div", { "class": "pct", "aria-hidden": "true" });
  var menuBtn = el("button", { "class": "menu-btn", type: "button", "aria-expanded": "false", "aria-controls": "menu", onclick: function () { toggleMenu(); } },
    [iconEl("menu"), "Menu"]);
  var brand = el("div", { "class": "brand" }, [
    el("span", { "class": "mark", "aria-hidden": "true", text: (C.title || "C").trim().charAt(0).toUpperCase() }),
    el("p", { "class": "ctitle", text: C.title || "" })]);
  var notice = connected ? null : el("p", { "class": "notice", title: "Preview: progress is saved in this browser only" }, [el("span", { "class": "dot", "aria-hidden": "true" }),
    el("span", null, ["Preview", el("span", { "class": "wordy", text: ", saved in this browser" })]),
    el("button", { type: "button", text: "Start over", onclick: function () { try { localStorage.removeItem(storeKey); } catch (e) { /* ignore */ } state = { v: [], a: {}, n: 1, loc: 0, max: 0 }; visited = {}; openSec = {}; go(0, true); } })]);
  var top = el("header", { "class": "top" + (notice ? " has-notice" : "") }, [menuBtn, brand, notice, pctLbl, progressBar]);
  var menu = el("nav", { "class": "menu", id: "menu", "aria-label": "Course menu" });
  var backdrop = el("div", { "class": "backdrop", "aria-hidden": "true", onclick: function () { toggleMenu(false); menuBtn.focus(); } });
  var main = el("main", { id: "main", tabindex: "-1" });
  var backBtn = el("button", { "class": "btn ghost back", type: "button", "aria-label": "Back", onclick: function () { go(state.loc - 1); } },
    [iconEl("left"), el("span", { "class": "lbl", text: "Back" })]);
  var nextTitle = el("span", { "class": "l2" });
  var nextBtn = el("button", { "class": "btn next", type: "button", onclick: function () { go(state.loc + 1); } },
    [el("span", { "class": "l1" }, [el("span", { text: "Next" }), iconEl("right")]), nextTitle]);
  var count = el("div", { "class": "count", "aria-live": "off" });
  var foot = el("nav", { "class": "foot", "aria-label": "Page navigation" }, [el("div", { "class": "in" }, [backBtn, count, nextBtn])]);
  root.appendChild(el("a", { "class": "skip", href: "#main", text: "Skip to content" }));
  root.appendChild(top);
  root.appendChild(el("div", { "class": "wrap" }, [menu, backdrop, main]));
  root.appendChild(foot);
  root.appendChild(el("div", { id: "announcer", "class": "sr-only", "aria-live": "polite" }));

  function toggleMenu(open) {
    var isOpen = open == null ? !document.body.classList.contains("menu-open") : open;
    document.body.classList.toggle("menu-open", isOpen);
    menuBtn.setAttribute("aria-expanded", String(isOpen));
    if (isOpen) { var cur = menu.querySelector("[aria-current=page]"); if (cur) cur.focus(); }
  }
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && document.body.classList.contains("menu-open")) { toggleMenu(false); menuBtn.focus(); }
  });

  function canReach(i) { return !SEQUENTIAL || i <= state.max; }
  function pageComplete(p) {
    return (p.blocks || []).every(function (b) { return b.type !== "question" || !!state.a[b.id]; });
  }
  function seenCount() { return Object.keys(visited).length; }
  function pct() { return Math.round((seenCount() / pages.length) * 100); }

  // ------------------------------------------------------------------ sidebar
  var openSec = {};
  function ring(cls, size, stroke, frac) {
    var r = (size - stroke) / 2, full = 2 * Math.PI * r;
    return '<svg class="' + cls + '" viewBox="0 0 ' + size + " " + size + '" aria-hidden="true" focusable="false" style="--full:' + full.toFixed(1) + '">' +
      '<circle class="track" cx="' + size / 2 + '" cy="' + size / 2 + '" r="' + r + '"/>' +
      '<circle class="val" cx="' + size / 2 + '" cy="' + size / 2 + '" r="' + r + '" stroke-dasharray="' + full.toFixed(1) + '" stroke-dashoffset="' + (full * (1 - frac)).toFixed(1) + '"' + (frac <= 0 ? ' style="stroke:transparent"' : "") + "/></svg>";
  }

  function renderMenu() {
    menu.innerHTML = "";
    var n = seenCount();
    menu.appendChild(el("div", { "class": "menu-head" }, [
      el("p", { "class": "eyebrow", text: "Your progress" }),
      el("div", { "class": "ring-row" }, [
        el("span", { html: ring("ring", 52, 6, n / pages.length) }).firstChild,
        el("div", null, [el("p", { "class": "big", text: pct() + "% complete" }),
          el("p", { "class": "sub", text: n + " of " + pages.length + " pages seen" })])])]));
    var cur = pages[state.loc]._section;
    sectionList.forEach(function (sec, si) {
      var secPages = sec.pages, seenHere = secPages.filter(function (p) { return visited[p._index]; }).length;
      var done = seenHere === secPages.length, isCur = sec === cur;
      if (openSec[si] == null) openSec[si] = isCur;
      var listId = "sec-" + si;
      var ol = el("ol", { id: listId, hidden: !openSec[si] });
      var btn = el("button", { "class": "sec-btn", type: "button", "aria-expanded": String(!!openSec[si]), "aria-controls": listId,
        onclick: function () { openSec[si] = !openSec[si]; ol.hidden = !openSec[si]; btn.setAttribute("aria-expanded", String(openSec[si])); } }, [
        el("span", { "class": "sec-num", "aria-hidden": "true", html: done ? icon("check") : String(si + 1) }),
        el("span", { "class": "sec-txt" }, [el("span", { "class": "sec-title", text: sec.title || "Section " + (si + 1) }),
          el("span", { "class": "sec-meta", text: done ? "Completed" : seenHere + " of " + secPages.length + " pages" })]),
        iconEl("chev", "chev")]);
      secPages.forEach(function (p) {
        var i = p._index, locked = !canReach(i);
        var label = [el("span", { "class": "dot", "aria-hidden": "true" }), el("span", { text: p.title || "Page " + (i + 1) })];
        if (visited[i]) label.push(el("span", { "class": "sr-only", text: " (visited)" }));
        if (locked) { label.push(iconEl("lock", "lock")); label.push(el("span", { "class": "sr-only", text: " (locked until you reach it)" })); }
        ol.appendChild(el("li", null, [el("button", {
          "class": "pg" + (visited[i] ? " visited" : ""), type: "button",
          "aria-current": i === state.loc ? "page" : false, "aria-disabled": locked ? "true" : false,
          onclick: function () { if (!locked) { toggleMenu(false); go(i); } }
        }, label)]));
      });
      menu.appendChild(el("div", { "class": "sec" + (done ? " done" : "") + (isCur ? " current" : "") }, [btn, ol]));
    });
  }

  function renderProgress() {
    var p = pct();
    progressFill.style.width = p + "%";
    progressBar.setAttribute("aria-valuenow", String(p));
    progressBar.setAttribute("aria-valuetext", p + "% of the course seen");
    pctLbl.innerHTML = "<b>" + p + "%</b><span class=\"wordy\"> complete</span>";
  }

  function updateFoot() {
    var p = pages[state.loc];
    backBtn.disabled = state.loc === 0;
    var last = state.loc === pages.length - 1;
    nextBtn.hidden = last;
    nextBtn.disabled = SEQUENTIAL && !pageComplete(p);
    nextBtn.title = nextBtn.disabled ? "Answer the question on this page to continue" : "";
    nextTitle.textContent = last ? "" : (pages[state.loc + 1].title || "");
    count.innerHTML = "";
    if (last) count.appendChild(el("span", { "class": "done-chip", html: icon("flag") + "End of course" }));
    else count.appendChild(document.createTextNode((state.loc + 1) + " of " + pages.length));
    if (nextBtn.disabled) count.appendChild(el("span", { "class": "lockhint", text: "Answer to continue" }));
  }

  // ------------------------------------------------------------------ blocks
  function renderBlock(b, first) {
    switch (b.type) {
      case "heading": return el("div", { "class": "block" }, [el("h2", { text: b.text || "" })]);
      case "text": return el("div", { "class": "block text" + (first ? " lead" : ""), html: md(b.text) });
      case "callout": {
        var names = { warning: "Important", tip: "Tip", info: "Note" };
        var st = names[b.style] ? b.style : "info";
        return el("aside", { "class": "block callout " + st, "aria-label": b.title || names[st] }, [
          el("span", { "class": "badge", html: icon(st) }),
          el("div", { "class": "body" }, [el("span", { "class": "ctitle", text: b.title || names[st] }), el("div", { "class": "text", html: md(b.text) })])]);
      }
      case "image": {
        if (b.src) {
          var img = el("img", { src: b.src, alt: b.alt || "", loading: "lazy", decoding: "async" });
          return el("figure", { "class": "block" }, [el("div", { "class": "frame" }, [img]), b.caption ? el("figcaption", { text: b.caption }) : null]);
        }
        return el("figure", { "class": "block" }, [el("div", { "class": "placeholder", role: "img", "aria-label": "Image to come" + (b.alt ? ": " + b.alt : "") }, [
          iconEl("image"), el("b", { text: "Image to come" }), el("span", { text: b.description || b.alt || "" })]), b.caption ? el("figcaption", { text: b.caption }) : null]);
      }
      case "reveal": return renderReveal(b);
      case "question": return renderQuestion(b);
      case "results": return renderResults();
      default: return null;
    }
  }

  function renderReveal(b) {
    var wrap = el("div", { "class": "block reveal" });
    if (b.text) wrap.appendChild(el("div", { "class": "text reveal-intro", html: md(b.text) }));
    var items = b.items || [], opened = 0;
    var fill = el("i"), metaTxt = el("span");
    function meta() { metaTxt.textContent = opened === items.length ? "All " + items.length + " opened" : opened + " of " + items.length + " opened"; fill.style.width = (items.length ? opened / items.length * 100 : 0) + "%"; }
    if (items.length > 1) wrap.appendChild(el("div", { "class": "reveal-meta", "aria-hidden": "true" }, [el("span", { text: "Select each one to learn more" }), el("span", { "class": "bar" }, [fill]), metaTxt]));
    items.forEach(function (it) {
      var seen = el("span", { "class": "seen", hidden: true, html: icon("check") + "Viewed" });
      var d = el("details", null, [el("summary", null, [el("span", { "class": "plus", html: icon("plus") }), el("span", { "class": "stitle", text: it.title || "" }), seen]),
        el("div", { "class": "inner text", html: md(it.text) })]);
      d.addEventListener("toggle", function () { if (d.open && seen.hidden) { seen.hidden = false; opened++; meta(); } });
      wrap.appendChild(d);
    });
    meta();
    return wrap;
  }

  var LETTERS = "ABCDEFGHIJKL";
  function renderQuestion(q) {
    var multiple = !!q.multiple;
    var name = "q-" + q.id;
    var box = el("div", { "class": "block q" + (q.scored ? " scored" : "") });
    var fs = el("fieldset");
    var legendId = name + "-legend";
    if (q.scored) {
      var qn = scored.indexOf(q) + 1;
      box.appendChild(el("span", { "class": "tag", html: icon("target") + "Assessment question" + (qn ? " " + qn + " of " + scored.length : "") }));
    } else box.appendChild(el("span", { "class": "tag", html: icon("help") + "Knowledge check" }));
    fs.appendChild(el("legend", { id: legendId, html: inline(q.prompt || "").replace(/\r?\n+/g, "<br>") }));
    fs.appendChild(el("p", { "class": "hint", text: multiple ? "Select all that apply." : "Select one answer." }));
    var inputs = [], labels = [];
    (q.options || []).forEach(function (o, i) {
      var id = name + "-" + i;
      var input = el("input", { type: multiple ? "checkbox" : "radio", name: name, id: id, value: String(i) });
      inputs.push(input);
      var lab = el("label", { "class": "opt" + (multiple ? " multi" : ""), "for": id }, [input,
        el("span", { "class": "ind", "aria-hidden": "true", html: '<span class="l">' + LETTERS.charAt(i) + "</span>" + icon("check", "ok") + icon("cross", "no") }),
        el("span", { "class": "otext", html: inline(o.text) })]);
      labels.push(lab);
      fs.appendChild(lab);
    });
    function syncChecked() { inputs.forEach(function (inp, i) { labels[i].classList.toggle("is-checked", inp.checked); }); }
    inputs.forEach(function (inp) { inp.addEventListener("change", syncChecked); });
    box.appendChild(fs);
    var fb = el("div", { "class": "fb", role: "status" });
    var submit = el("button", { "class": "btn", type: "button", text: "Submit" });
    var retry = el("button", { "class": "btn ghost", type: "button", text: "Try again", hidden: true });
    box.appendChild(el("div", { "class": "qrow" }, [submit, retry]));
    box.appendChild(fb);

    function setFb(kind, iconName, verdict, bodyHtml) {
      fb.className = "fb " + kind;
      fb.innerHTML = '<span class="fbi">' + icon(iconName) + '</span><div class="fbt">' + (verdict ? '<p class="verdict">' + verdict + "</p>" : "") + bodyHtml + "</div>";
    }
    function chosen() { var c = []; inputs.forEach(function (inp, i) { if (inp.checked) c.push(i); }); return c; }
    function lock(answer, fresh) {
      inputs.forEach(function (inp, i) { inp.checked = answer.indexOf(i) >= 0; inp.disabled = true; });
      syncChecked();
      box.classList.add("locked");
      submit.disabled = true; submit.hidden = true;
      var ok = isCorrect(q, answer);
      var showFb = !q.scored || SHOW_QUIZ_FB;
      var parts = [];
      if (showFb) {
        var general = ok ? q.feedback_correct : q.feedback_incorrect;
        answer.forEach(function (i) { var f = q.options[i] && q.options[i].feedback; if (f && !multiple) parts.push(md(f)); });
        if (general) parts.push(md(general));
        if (multiple || !q.options.some(function (o) { return o.feedback; })) {
          if (!ok && !general) {
            parts.push("<p>The correct answer: " + q.options.filter(function (o) { return o.correct; }).map(function (o) { return inline(o.text); }).join("; ") + "</p>");
          }
        }
        inputs.forEach(function (inp, i) {
          var lab = labels[i], o = q.options[i];
          if (o.correct && (ok || q.scored || !q.retry)) { lab.classList.add("correct"); lab.appendChild(el("span", { "class": "mark", text: "Correct answer" })); }
          else if (answer.indexOf(i) >= 0 && !o.correct) { lab.classList.add("wrong"); lab.appendChild(el("span", { "class": "mark", text: "Your answer" })); }
        });
        setFb(ok ? "ok" : "bad", ok ? "check" : "cross", ok ? "Correct." : "Not quite.", parts.join(""));
      } else {
        setFb("neutral", "check", "Answer saved.", "<p>You'll see your score at the end.</p>");
      }
      retry.hidden = !(showFb && !ok && !q.scored && q.retry !== false);
      if (fresh) announce(fb.textContent);
    }
    submit.addEventListener("click", function () {
      var c = chosen();
      if (!c.length) { setFb("neutral", "help", "", "<p>Choose an answer first.</p>"); announce("Choose an answer first."); return; }
      state.a[q.id] = c;
      if (q.scored) lms.interaction(q, c, isCorrect(q, c));
      lock(c, true);
      afterChange();
    });
    retry.addEventListener("click", function () {
      delete state.a[q.id];
      box.classList.remove("locked");
      inputs.forEach(function (inp, i) { inp.checked = false; inp.disabled = false; var lab = labels[i]; lab.classList.remove("correct", "wrong", "is-checked"); var m = lab.querySelector(".mark"); if (m) lab.removeChild(m); });
      submit.disabled = false; submit.hidden = false; retry.hidden = true; fb.className = "fb"; fb.innerHTML = "";
      inputs[0] && inputs[0].focus();
      afterChange();
    });
    if (state.a[q.id]) lock(state.a[q.id], false);
    return box;
  }

  function renderResults() {
    var r = evaluate();
    var box = el("section", { "class": "block results", "aria-labelledby": "results-h" });
    if (!r.hasQuiz) {
      box.appendChild(el("h2", { id: "results-h", text: "Your results" }));
      box.appendChild(el("p", { "class": "lede", text: "There are no scored questions in this course." }));
      return box;
    }
    if (r.score == null) {
      box.appendChild(el("h2", { id: "results-h", text: "Your results" }));
      box.appendChild(el("div", { "class": "pending" }, [
        el("p", { "class": "lede", text: "You've answered " + r.answered + " of " + scored.length + " assessment questions. Answer the rest to see your score." }),
        el("div", { "class": "meter", "aria-hidden": "true" }, [el("i", { style: "width:" + Math.round(r.answered / scored.length * 100) + "%" })])]));
      var first = scored.filter(function (q) { return !state.a[q.id]; })[0];
      if (first) box.appendChild(el("div", { "class": "row" }, [el("button", { "class": "btn", type: "button", html: "Go to the next unanswered question" + icon("right"), onclick: function () { go(blockPage[first.id]); } })]));
      return box;
    }
    box.className += r.passed ? " pass" : " fail";
    box.appendChild(el("p", { "class": "verdict-pill", html: icon(r.passed ? "award" : "cross") + (r.passed ? "Passed" : "Not passed yet") }));
    var ringWrap = el("div", { "class": "score-ring" });
    ringWrap.innerHTML = ring("", 180, 12, r.score / 100);
    ringWrap.appendChild(el("div", { "class": "num" }, [el("p", { "class": "score", text: r.score + "%" }), el("span", { "class": "of", text: "your score" })]));
    box.appendChild(ringWrap);
    box.appendChild(el("h2", { id: "results-h", text: r.passed ? "Well done, you passed" : "Not quite there yet" }));
    box.appendChild(el("p", { "class": "lede", text: "You answered " + r.right + " of " + scored.length + " correctly. The pass mark is " + PASS + "%." }));
    var left = attemptsLeft();
    box.appendChild(el("ul", { "class": "stats" }, [
      el("li", null, [el("b", { text: r.right + "/" + scored.length }), el("span", { text: "Correct" })]),
      el("li", null, [el("b", { text: PASS + "%" }), el("span", { text: "Pass mark" })]),
      el("li", null, [el("b", { text: "#" + state.n }), el("span", { text: "Attempt" + (MAX_ATTEMPTS ? " of " + MAX_ATTEMPTS : "") })])]));
    if (r.passed) {
      box.appendChild(el("p", { "class": "msg", html: "<strong>You passed.</strong> " + esc(S.pass_message || "Well done. Your result has been recorded, and you can close the course.") }));
    } else if (left > 0) {
      box.appendChild(el("p", { "class": "msg", html: "<strong>Not passed yet.</strong> " + esc(S.fail_message || "Review the topics you missed, then try the questions again.") +
        (left !== Infinity ? " You have " + left + " more " + (left === 1 ? "try" : "tries") + "." : "") }));
      box.appendChild(el("div", { "class": "row" }, [el("button", { "class": "btn", type: "button", text: "Retake the assessment", onclick: retake })]));
    } else {
      box.appendChild(el("p", { "class": "msg", html: "<strong>Not passed.</strong> You've used all your tries. Your result has been recorded. Please talk to your manager or training team." }));
    }
    return box;
  }

  function retake() {
    scored.forEach(function (q) { delete state.a[q.id]; });
    state.n += 1;
    afterChange();
    go(scored.length ? blockPage[scored[0].id] : 0);
  }

  // ------------------------------------------------------------------ navigation
  function renderCover() {
    var facts = [icon("layers") + sectionList.length + (sectionList.length === 1 ? " section" : " sections"),
      icon("book") + pages.length + " pages", icon("clock") + "About " + minutes + " min"];
    if (scored.length) facts.push(icon("target") + "Pass mark " + PASS + "%");
    var cover = el("header", { "class": "cover" + (INK_DARK ? " ink-dark" : "") }, [
      el("p", { "class": "kicker", text: "Course" }),
      el("p", { "class": "ctitle", text: C.title || "" }),
      C.description ? el("p", { "class": "desc", text: C.description }) : null,
      el("ul", { "class": "facts" }, facts.map(function (f) { return el("li", { html: f }); }))]);
    return cover;
  }

  function renderPage() {
    var p = pages[state.loc];
    var page = el("article", { "class": "page" });
    if (state.loc === 0) page.appendChild(renderCover());
    var head = el("div", { "class": "page-head" });
    var eyebrow = el("div", { "class": "eyebrow-row" });
    if (p._section && p._section.title) eyebrow.appendChild(el("p", { "class": "section-name", text: p._section.title }));
    eyebrow.appendChild(el("span", { "class": "page-no", text: "Page " + (state.loc + 1) + " of " + pages.length }));
    head.appendChild(eyebrow);
    var h1 = el("h1", { tabindex: "-1", text: p.title || "" });
    head.appendChild(h1);
    head.appendChild(el("hr", { "class": "head-rule", "aria-hidden": "true" }));
    page.appendChild(head);
    // A page that opens with a picture shows it beside the words on wide screens (see .split in the CSS).
    var blocks = p.blocks || [], target = page;
    if (blocks.length > 1 && blocks[0].type === "image" && blocks.slice(1).every(function (b) { return b.type !== "results"; })) {
      var media = el("div", { "class": "split-media" }), body = el("div", { "class": "split-body" });
      page.appendChild(el("div", { "class": "split" }, [media, body]));
      page.classList.add("has-split");
      var pic = renderBlock(blocks[0]); if (pic) media.appendChild(pic);
      blocks = blocks.slice(1); target = body;
    }
    var firstText = true;
    blocks.forEach(function (b) {
      var n = renderBlock(b, firstText && b.type === "text");
      if (b.type !== "image") firstText = false;
      if (n) target.appendChild(n);
    });
    if (state.loc === pages.length - 1) {
      var r = evaluate();
      page.appendChild(el("div", { "class": "endcard" }, [iconEl("flag"), el("h2", { text: "You've reached the end" }),
        el("p", { text: r.hasQuiz && !r.passed ? "Your progress is saved. Finish the assessment to complete the course." : "Your progress is saved. You can close the course now." })]));
    }
    main.innerHTML = "";
    main.appendChild(page);
    return h1;
  }

  function go(i, force) {
    if (i < 0 || i >= pages.length) return;
    if (!force && !canReach(i) && !(i === state.loc + 1 && pageComplete(pages[state.loc]))) return;
    var prevSec = pages[state.loc] && pages[state.loc]._section;
    state.loc = i;
    visited[i] = true;
    if (i > state.max) state.max = i;
    var si = sectionList.indexOf(pages[i]._section);
    if (pages[i]._section !== prevSec || openSec[si] == null) openSec[si] = true;
    var h1 = renderPage();
    renderMenu(); renderProgress(); updateFoot();
    document.title = (pages[i].title ? pages[i].title + " | " : "") + (C.title || "Course");
    window.scrollTo(0, 0);
    try { h1.focus({ preventScroll: true }); } catch (e) { h1.focus(); }
    persist();
  }

  function afterChange() {
    updateFoot(); renderMenu(); persist();
    // refresh a results block on the current page, if any
    var res = main.querySelector(".results");
    if (res) { var fresh = renderResults(); res.parentNode.replaceChild(fresh, res); }
  }

  function exit() { if (connected) { persist(); lms.finish(evaluate().finished); } }
  window.addEventListener("pagehide", exit);
  window.addEventListener("beforeunload", exit);

  // For automated tests and the editor preview.
  window.ModuloPlayer = { go: go, evaluate: evaluate, state: function () { return state; }, lms: lms.version, connected: connected };

  var hash = location.hash.match(/^#page=(\d+)$/);
  go(!connected && hash ? Math.min(+hash[1], pages.length - 1) : Math.min(state.loc, pages.length - 1), true);
})();
