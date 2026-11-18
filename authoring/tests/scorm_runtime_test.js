// Plays the exported packages in a real browser inside a mock LMS and checks what the course reports.
// Usage: node scorm_runtime_test.js http://localhost:PORT OUTDIR
const { chromium } = require("playwright");
const fs = require("fs");
const base = process.argv[2], outDir = process.argv[3];
const key = JSON.parse(fs.readFileSync(outDir + "/key.json", "utf8"));
let failures = 0;
function check(cond, msg) { console.log((cond ? "  PASS " : "  FAIL ") + msg); if (!cond) failures++; }

async function frameOf(page) {
  await page.waitForFunction(() => document.querySelector("iframe") && document.querySelector("iframe").contentWindow.ModuloPlayer);
  return page.frames().find(f => f.url().includes("index.html"));
}

// Walk the whole course from the current page, answering every question. mode: "right" or "wrong" for scored ones.
async function walk(frame, mode) {
  for (let guard = 0; guard < 200; guard++) {
    const qs = await frame.$$eval(".q", els => els.map(e => ({ id: e.querySelector("input").name.slice(2), done: e.querySelector("button").disabled, scored: !!e.querySelector(".tag") && e.querySelector(".tag").textContent.includes("Assessment") })));
    for (const q of qs) {
      if (q.done) continue;
      const right = key[q.id];
      const n = await frame.$$eval(`input[name="q-${q.id}"]`, x => x.length);
      let pick = right;
      if (mode === "wrong" && q.scored) { const w = [...Array(n).keys()].find(i => !right.includes(i)); pick = [w]; }
      for (const i of pick) await frame.click(`label[for="q-${q.id}-${i}"]`);
      await frame.click(`.q:has(input[name="q-${q.id}"]) button:has-text("Submit")`);
    }
    const next = await frame.$(".foot button:has-text('Next')");
    if (!next || await next.isHidden()) return;
    await next.click();
  }
}

async function run(browser, ver, pkg) {
  console.log(`\n== ${pkg} (SCORM ${ver}) ==`);
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", e => errors.push(String(e)));
  const url = `${base}/harness.html?v=${ver}&pkg=${pkg}`;
  await page.goto(url);
  let frame = await frameOf(page);
  check(await frame.evaluate(() => ModuloPlayer.connected && ModuloPlayer.lms) === ver, `player found the SCORM ${ver} API`);
  const P = ver === "2004"
    ? { status: "cmi.success_status", raw: "cmi.score.raw", loc: "cmi.location", exit: "cmi.exit", time: "cmi.session_time" }
    : { status: "cmi.core.lesson_status", raw: "cmi.core.score.raw", loc: "cmi.core.lesson_location", exit: "cmi.core.exit", time: "cmi.core.session_time" };

  // 1. Fail first: wrong answers on every assessment question.
  await walk(frame, "wrong");
  let cmi = await page.evaluate(() => window.cmi);
  check(cmi[P.status] === "failed", `${P.status} = failed after wrong answers (got ${cmi[P.status]})`);
  check(cmi[P.raw] === "0", `score reported as 0 (got ${cmi[P.raw]})`);
  const nInter = Object.keys(cmi).filter(k => /^cmi\.interactions\.\d+\.id$/.test(k)).length;
  check(nInter === 20, `20 assessment answers recorded as interactions (got ${nInter})`);
  if (ver === "2004") check(cmi["cmi.completion_status"] === "completed", `completion_status = completed after seeing every page (got ${cmi["cmi.completion_status"]})`);

  // 2. Results page offers a retake; take it and pass.
  // find and open the results page
  const idx = await frame.evaluate(() => { for (let i = 0; i < 200; i++) { ModuloPlayer.go(i, true); if (document.querySelector(".results")) return i; } return -1; });
  check(idx >= 0, "there is a results page");
  check(await frame.isVisible("text=Retake the assessment"), "results page shows Retake after a fail");
  await frame.click("text=Retake the assessment");
  check(await frame.evaluate(() => Object.keys(ModuloPlayer.state().a).length) === 12, "retake cleared the 20 assessment answers and kept the 12 knowledge checks");
  await walk(frame, "right");
  await frame.evaluate((i) => ModuloPlayer.go(i, true), idx);
  check(await frame.isVisible(".results.pass"), "results page shows a pass after right answers");
  cmi = await page.evaluate(() => window.cmi);
  check(cmi[P.status] === "passed", `${P.status} = passed (got ${cmi[P.status]})`);
  check(cmi[P.raw] === "100", `score reported as 100 (got ${cmi[P.raw]})`);
  if (ver === "2004") check(cmi["cmi.score.scaled"] === "1.00", `score.scaled = 1.00 (got ${cmi["cmi.score.scaled"]})`);
  const susp = cmi["cmi.suspend_data"] || "";
  check(susp.length > 0 && susp.length < 4096, `progress saved in suspend_data (${susp.length} characters, SCORM 1.2 limit 4096)`);

  // 3. Leave on page 5, then come back: the course resumes there with answers kept.
  await frame.evaluate(() => ModuloPlayer.go(4, true));
  await page.goto("about:blank");
  await page.goto(url);
  frame = await frameOf(page);
  cmi = await page.evaluate(() => window.cmi);
  check(cmi[P.exit] === (ver === "2004" ? "normal" : ""), `exit recorded as finished (got "${cmi[P.exit]}")`);
  check(/^(PT\d+H\d+M\d+S|\d{4}:\d\d:\d\d)$/.test(cmi[P.time] || ""), `session time in the right format (${cmi[P.time]})`);
  check(await frame.evaluate(() => ModuloPlayer.state().loc) === 4, "resumed on page 5");
  check(await frame.evaluate(() => Object.keys(ModuloPlayer.state().a).length) === 32, "all answers kept after resume");
  const h1 = await frame.evaluate(() => document.activeElement && document.activeElement.tagName);
  check(h1 === "H1", "keyboard focus starts on the page heading");
  check(errors.length === 0, "no JavaScript errors" + (errors.length ? ": " + errors.join(" | ") : ""));
  await ctx.close();
}

(async () => {
  const browser = await chromium.launch();
  await run(browser, "1.2", "scorm12");
  await run(browser, "2004", "scorm2004");

  // Web package without an LMS, on a phone, keyboard only.
  console.log("\n== web package on a phone, no LMS ==");
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await ctx.newPage();
  const errors = []; page.on("pageerror", e => errors.push(String(e)));
  await page.goto(`${base}/web/index.html`);
  await page.waitForFunction(() => window.ModuloPlayer);
  check(await page.evaluate(() => !ModuloPlayer.connected), "runs without an LMS (preview mode)");
  const sw = await page.evaluate(() => document.documentElement.scrollWidth);
  check(sw <= 390, `no sideways scrolling on a phone (page width ${sw})`);
  await page.screenshot({ path: outDir + "/phone.png" });
  for (let i = 0; i < 40 && await page.evaluate(() => document.activeElement.textContent !== "Menu"); i++) await page.keyboard.press("Tab");
  check(await page.evaluate(() => document.activeElement.textContent) === "Menu", "menu button reachable by keyboard");
  await page.keyboard.press("Enter");
  check(await page.evaluate(() => document.body.classList.contains("menu-open")), "menu opens with Enter");
  await page.keyboard.press("Escape");
  check(await page.evaluate(() => !document.body.classList.contains("menu-open")), "menu closes with Escape");
  await page.evaluate(() => ModuloPlayer.go(5, true));
  await page.reload(); await page.waitForFunction(() => window.ModuloPlayer);
  check(await page.evaluate(() => ModuloPlayer.state().loc) === 5, "preview remembers the page in this browser");
  // a question page on a phone
  await page.evaluate(() => { for (let i = 0; i < 80; i++) { ModuloPlayer.go(i, true); if (document.querySelector(".q")) return; } });
  await page.screenshot({ path: outDir + "/phone-question.png" });
  check(errors.length === 0, "no JavaScript errors" + (errors.length ? ": " + errors.join(" | ") : ""));
  await ctx.close();
  const d = await browser.newPage({ viewport: { width: 1280, height: 860 } });
  await d.goto(`${base}/web/index.html`);
  await d.waitForFunction(() => window.ModuloPlayer);
  await d.evaluate(() => { for (let i = 0; i < 80; i++) { ModuloPlayer.go(i, true); if (document.querySelector("details")) return; } });
  await d.click("summary >> nth=0");
  await d.screenshot({ path: outDir + "/desktop.png" });
  await browser.close();
  console.log(failures ? `\n${failures} check(s) failed` : "\nAll checks passed");
  process.exit(failures ? 1 : 0);
})();
