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

  // ------------------------------------------------------------------ shell
  var root = document.getElementById("app");
  document.documentElement.lang = C.language || "en";
  if (S.accent && /^#[0-9a-f]{6}$/i.test(S.accent)) document.documentElement.style.setProperty("--accent", S.accent);
  document.title = C.title || "Course";

  var progressFill = el("div", { "class": "fill" });
  var progressLbl = el("div", { "class": "lbl" });
  var progressBar = el("div", { "class": "bar", role: "progressbar", "aria-label": "Course progress", "aria-valuemin": "0", "aria-valuemax": "100" }, [progressFill]);
  var menuBtn = el("button", { "class": "btn ghost menu-btn", type: "button", "aria-expanded": "false", "aria-controls": "menu", text: "Menu", onclick: function () { toggleMenu(); } });
  var top = el("header", { "class": "top" }, [menuBtn, el("p", { "class": "ctitle", text: C.title || "" }), el("div", { "class": "progress" }, [progressBar, progressLbl])]);
  var menu = el("nav", { "class": "menu", id: "menu", "aria-label": "Course menu" });
  var main = el("main", { id: "main", tabindex: "-1" });
  var backBtn = el("button", { "class": "btn ghost", type: "button", text: "Back", onclick: function () { go(state.loc - 1); } });
  var nextBtn = el("button", { "class": "btn", type: "button", text: "Next", onclick: function () { go(state.loc + 1); } });
  var count = el("div", { "class": "count", "aria-live": "off" });
  var foot = el("nav", { "class": "foot", "aria-label": "Page navigation" }, [el("div", { "class": "in" }, [backBtn, count, nextBtn])]);
  root.appendChild(el("a", { "class": "skip", href: "#main", text: "Skip to content" }));
  root.appendChild(top);
  root.appendChild(el("div", { "class": "wrap" }, [menu, main]));
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

  function renderMenu() {
    menu.innerHTML = "";
    (C.sections || []).forEach(function (sec) {
      if (!(sec.pages || []).length) return;
      menu.appendChild(el("h2", { text: sec.title || "" }));
      var ol = el("ol");
      sec.pages.forEach(function (p) {
        var i = p._index, locked = !canReach(i);
        var label = [el("span", { "class": "tick", "aria-hidden": "true", text: visited[i] ? "✓" : "" }), el("span", { text: p.title || "Page " + (i + 1) })];
        if (visited[i]) label.push(el("span", { "class": "sr-only", text: " (visited)" }));
        if (locked) label.push(el("span", { "class": "sr-only", text: " (locked until you reach it)" }));
        ol.appendChild(el("li", null, [el("button", {
          type: "button", "aria-current": i === state.loc ? "page" : false, "aria-disabled": locked ? "true" : false,
          onclick: function () { if (!locked) { toggleMenu(false); go(i); } }
        }, label)]));
      });
      menu.appendChild(ol);
    });
  }

  function renderProgress() {
    var pct = Math.round((Object.keys(visited).length / pages.length) * 100);
    progressFill.style.width = pct + "%";
    progressBar.setAttribute("aria-valuenow", String(pct));
    progressLbl.textContent = pct + "% seen";
  }

  function updateFoot() {
    var p = pages[state.loc];
    backBtn.disabled = state.loc === 0;
    var last = state.loc === pages.length - 1;
    nextBtn.hidden = last;
    nextBtn.disabled = SEQUENTIAL && !pageComplete(p);
    nextBtn.title = nextBtn.disabled ? "Answer the question on this page to continue" : "";
    count.textContent = "Page " + (state.loc + 1) + " of " + pages.length;
  }

  // ------------------------------------------------------------------ blocks
  function renderBlock(b) {
    switch (b.type) {
      case "heading": return el("div", { "class": "block" }, [el("h2", { text: b.text || "" })]);
      case "text": return el("div", { "class": "block text", html: md(b.text) });
      case "callout": {
        var names = { warning: "Important", tip: "Tip", info: "Note" };
        var st = names[b.style] ? b.style : "info";
        return el("aside", { "class": "block callout " + st, "aria-label": b.title || names[st] }, [
          el("span", { "class": "ctitle", text: b.title || names[st] }), el("div", { "class": "text", html: md(b.text) })]);
      }
      case "image": {
        if (b.src) {
          var img = el("img", { src: b.src, alt: b.alt || "", loading: "lazy" });
          return el("figure", { "class": "block" }, [img, b.caption ? el("figcaption", { text: b.caption }) : null]);
        }
        return el("figure", { "class": "block" }, [el("div", { "class": "placeholder", role: "img", "aria-label": "Image to come" + (b.alt ? ": " + b.alt : "") }, [
          el("b", { text: "Image to come" }), el("span", { text: b.description || b.alt || "" })]), b.caption ? el("figcaption", { text: b.caption }) : null]);
      }
      case "reveal": {
        var wrap = el("div", { "class": "block" });
        if (b.text) wrap.appendChild(el("div", { "class": "text", html: md(b.text) }));
        (b.items || []).forEach(function (it) {
          var seen = el("span", { "class": "seen", hidden: true, text: "Viewed" });
          var d = el("details", null, [el("summary", null, [el("span", { text: it.title || "" }), seen]), el("div", { "class": "inner text", html: md(it.text) })]);
          d.addEventListener("toggle", function () { if (d.open) seen.hidden = false; });
          wrap.appendChild(d);
        });
        return wrap;
      }
      case "question": return renderQuestion(b);
      case "results": return renderResults();
      default: return null;
    }
  }

  function renderQuestion(q) {
    var multiple = !!q.multiple;
    var name = "q-" + q.id;
    var box = el("div", { "class": "block q" });
    var fs = el("fieldset");
    var legendId = name + "-legend";
    if (q.scored) box.appendChild(el("span", { "class": "tag", text: "Assessment question" }));
    else box.appendChild(el("span", { "class": "tag", text: "Knowledge check" }));
    fs.appendChild(el("legend", { id: legendId, html: inline(q.prompt || "").replace(/\r?\n+/g, "<br>") }));
    fs.appendChild(el("p", { "class": "hint", text: multiple ? "Select all that apply." : "Select one answer." }));
    var inputs = [];
    (q.options || []).forEach(function (o, i) {
      var id = name + "-" + i;
      var input = el("input", { type: multiple ? "checkbox" : "radio", name: name, id: id, value: String(i) });
      inputs.push(input);
      fs.appendChild(el("label", { "class": "opt", "for": id }, [input, el("span", { html: inline(o.text) })]));
    });
    box.appendChild(fs);
    var fb = el("div", { "class": "fb", role: "status" });
    var submit = el("button", { "class": "btn", type: "button", text: "Submit" });
    var retry = el("button", { "class": "btn ghost", type: "button", text: "Try again", hidden: true });
    box.appendChild(el("div", { "class": "row" }, [submit, retry]));
    box.appendChild(fb);

    function chosen() { var c = []; inputs.forEach(function (inp, i) { if (inp.checked) c.push(i); }); return c; }
    function lock(answer, fresh) {
      inputs.forEach(function (inp, i) { inp.checked = answer.indexOf(i) >= 0; inp.disabled = true; });
      submit.disabled = true;
      var ok = isCorrect(q, answer);
      var showFb = !q.scored || SHOW_QUIZ_FB;
      fb.className = "fb " + (showFb ? (ok ? "ok" : "bad") : "neutral");
      var parts = [];
      if (showFb) {
        parts.push("<p><strong>" + (ok ? "Correct." : "Not quite.") + "</strong></p>");
        var general = ok ? q.feedback_correct : q.feedback_incorrect;
        answer.forEach(function (i) { var f = q.options[i] && q.options[i].feedback; if (f && !multiple) parts.push(md(f)); });
        if (general) parts.push(md(general));
        if (multiple || !q.options.some(function (o) { return o.feedback; })) {
          if (!ok && !general) {
            parts.push("<p>The correct answer: " + q.options.filter(function (o) { return o.correct; }).map(function (o) { return inline(o.text); }).join("; ") + "</p>");
          }
        }
        // mark options
        inputs.forEach(function (inp, i) {
          var lab = inp.parentNode, o = q.options[i];
          if (o.correct && (ok || q.scored || !q.retry)) { lab.classList.add("correct"); lab.appendChild(el("span", { "class": "mark", text: "Correct answer" })); }
          else if (answer.indexOf(i) >= 0 && !o.correct) { lab.classList.add("wrong"); lab.appendChild(el("span", { "class": "mark", text: "Your answer" })); }
        });
      } else {
        parts.push("<p>Your answer is saved. You'll see your score at the end.</p>");
      }
      fb.innerHTML = parts.join("");
      retry.hidden = !(showFb && !ok && !q.scored && q.retry !== false);
      if (fresh) announce(fb.textContent);
    }
    submit.addEventListener("click", function () {
      var c = chosen();
      if (!c.length) { fb.className = "fb neutral"; fb.innerHTML = "<p>Choose an answer first.</p>"; announce("Choose an answer first."); return; }
      state.a[q.id] = c;
      if (q.scored) lms.interaction(q, c, isCorrect(q, c));
      lock(c, true);
      afterChange();
    });
    retry.addEventListener("click", function () {
      delete state.a[q.id];
      inputs.forEach(function (inp) { inp.checked = false; inp.disabled = false; var lab = inp.parentNode; lab.classList.remove("correct", "wrong"); var m = lab.querySelector(".mark"); if (m) lab.removeChild(m); });
      submit.disabled = false; retry.hidden = true; fb.className = "fb"; fb.innerHTML = "";
      inputs[0] && inputs[0].focus();
      afterChange();
    });
    if (state.a[q.id]) lock(state.a[q.id], false);
    return box;
  }

  function renderResults() {
    var r = evaluate();
    var box = el("section", { "class": "block results", "aria-labelledby": "results-h" });
    box.appendChild(el("h2", { id: "results-h", text: "Your results", style: "margin-top:0" }));
    if (!r.hasQuiz) { box.appendChild(el("p", { text: "There are no scored questions in this course." })); return box; }
    if (r.score == null) {
      box.appendChild(el("p", { text: "You've answered " + r.answered + " of " + scored.length + " assessment questions. Answer the rest to see your score." }));
      var first = scored.filter(function (q) { return !state.a[q.id]; })[0];
      if (first) box.appendChild(el("div", { "class": "row" }, [el("button", { "class": "btn", type: "button", text: "Go to the next unanswered question", onclick: function () { go(blockPage[first.id]); } })]));
      return box;
    }
    box.className += r.passed ? " pass" : " fail";
    box.appendChild(el("p", { "class": "score", text: r.score + "%" }));
    box.appendChild(el("p", { text: "You answered " + r.right + " of " + scored.length + " correctly. The pass mark is " + PASS + "%." }));
    if (r.passed) {
      box.appendChild(el("p", { html: "<strong>You passed.</strong> " + esc(S.pass_message || "Well done. Your result has been recorded, and you can close the course.") }));
    } else if (attemptsLeft() > 0) {
      var left = attemptsLeft();
      box.appendChild(el("p", { html: "<strong>Not passed yet.</strong> " + esc(S.fail_message || "Review the topics you missed, then try the questions again.") +
        (left !== Infinity ? " You have " + left + " more " + (left === 1 ? "try" : "tries") + "." : "") }));
      box.appendChild(el("div", { "class": "row" }, [el("button", { "class": "btn", type: "button", text: "Retake the assessment", onclick: retake })]));
    } else {
      box.appendChild(el("p", { html: "<strong>Not passed.</strong> You've used all your tries. Your result has been recorded. Please talk to your manager or training team." }));
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
  function renderPage() {
    var p = pages[state.loc];
    var page = el("article", { "class": "page" });
    if (!connected) {
      page.appendChild(el("p", { "class": "notice" }, [
        "Preview: progress is saved in this browser only. ",
        el("button", { type: "button", text: "Start over", onclick: function () { try { localStorage.removeItem(storeKey); } catch (e) { /* ignore */ } state = { v: [], a: {}, n: 1, loc: 0, max: 0 }; visited = {}; go(0, true); } })]));
    }
    if (p._section && p._section.title) page.appendChild(el("p", { "class": "section-name", text: p._section.title }));
    var h1 = el("h1", { tabindex: "-1", text: p.title || "" });
    page.appendChild(h1);
    (p.blocks || []).forEach(function (b) { var n = renderBlock(b); if (n) page.appendChild(n); });
    main.innerHTML = "";
    main.appendChild(page);
    return h1;
  }

  function go(i, force) {
    if (i < 0 || i >= pages.length) return;
    if (!force && !canReach(i) && !(i === state.loc + 1 && pageComplete(pages[state.loc]))) return;
    state.loc = i;
    visited[i] = true;
    if (i > state.max) state.max = i;
    var h1 = renderPage();
    renderMenu(); renderProgress(); updateFoot();
    document.title = (pages[i].title ? pages[i].title + " | " : "") + (C.title || "Course");
    window.scrollTo(0, 0);
    h1.focus();
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
