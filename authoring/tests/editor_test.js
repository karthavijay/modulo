// Drives the course editor in a browser against a running local server (MODULO_FAKE=1).
// Usage: node editor_test.js http://localhost:8800 JOB_ID DASHBOARD_KEY OUTDIR
const { chromium } = require("playwright");
const path = require("path");
const [base, job, key, outDir] = process.argv.slice(2);
const Q = "?key=" + encodeURIComponent(key);
let failures = 0;
function check(c, m) { console.log((c ? "  PASS " : "  FAIL ") + m); if (!c) failures++; }

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, acceptDownloads: true });
  const page = await ctx.newPage();
  const errors = []; page.on("pageerror", e => errors.push(String(e)));
  page.on("dialog", d => d.accept());
  // start from a fresh free draft of the sample course
  await page.request.post(`${base}/builder/${job}/build${Q}`, { form: { mode: "quick" } });
  await page.goto(`${base}/builder/${job}${Q}`);
  await page.waitForSelector(".block-card");
  check((await page.textContent("#status")) === "All changes saved", "editor loads the course");
  await page.screenshot({ path: outDir + "/editor.png" });

  // 1. rename the first page
  await page.fill("#panel .card:first-child input[type=text]", "Welcome to the floor");
  await page.waitForFunction(() => document.getElementById("status").textContent === "All changes saved", null, { timeout: 8000 });
  check(true, "editing the page title saves automatically");
  check((await page.textContent("#outline")).includes("Welcome to the floor"), "outline shows the new title");

  // 2. add a question with no right answer marked -> warning, then fix it
  await page.click("text=+ Question");
  const card = page.locator(".block-card").last();
  await card.locator("textarea").first().fill("Which item must be checked before every shift?");
  const answers = card.locator(".opt input[type=text]");
  await answers.nth(0).fill("The brakes");
  await answers.nth(1).fill("The paint");
  await card.locator("text=Counts toward the final score").click();
  await page.waitForFunction(() => document.getElementById("status").textContent === "All changes saved", null, { timeout: 8000 });
  let api = await (await page.request.get(`${base}/api/builder/${job}/course${Q}`)).json();
  const q = api.course.sections[0].pages[0].blocks.slice(-1)[0];
  check(q.type === "question" && q.scored && q.options[0].correct && q.options[0].text === "The brakes", "new scored question saved with its right answer");
  check(api.check.scored === 21, `scored questions now 21 (got ${api.check.scored})`);

  // 3. upload a picture into the first placeholder
  const img = page.locator(".block-card").filter({ hasText: "Upload a picture" }).first();
  await img.locator("input[type=file]").setInputFiles(path.join(__dirname, "test-image.png"));
  await page.waitForSelector("img.thumb");
  await page.locator(".block-card").filter({ has: page.locator("img.thumb") }).first().locator("input[type=text]").first().fill("A forklift in a warehouse aisle at shift start");
  await page.waitForFunction(() => document.getElementById("status").textContent === "All changes saved", null, { timeout: 8000 });
  api = await (await page.request.get(`${base}/api/builder/${job}/course${Q}`)).json();
  const im = api.course.sections[0].pages[0].blocks.find(b => b.type === "image");
  check(/^assets\/.+\.png$/.test(im.src) && im.alt.startsWith("A forklift"), "picture uploaded with alt text");

  // 4. preview shows the edits and the picture
  const pv = await ctx.newPage();
  await pv.goto(api.course && `${base}${await page.getAttribute("#previewBtn", "href")}`);
  await pv.waitForFunction(() => window.ModuloPlayer);
  check((await pv.textContent("h1")) === "Welcome to the floor", "preview shows the renamed page");
  check(await pv.evaluate(() => { const i = document.querySelector("figure img"); return !!i && i.complete && i.naturalWidth === 320; }), "preview shows the uploaded picture");
  await pv.screenshot({ path: outDir + "/preview-edited.png" });

  // 5. settings: a too-light color is flagged
  await page.click("text=Course settings");
  await page.locator("input[type=color]").evaluate(e => { e.value = "#ffee88"; e.dispatchEvent(new Event("input", { bubbles: true })); });
  check(await page.isVisible("text=Too light for white button text"), "too-light color is flagged");
  await page.locator("input[type=color]").evaluate(e => { e.value = "#1d5c4a"; e.dispatchEvent(new Event("input", { bubbles: true })); });
  await page.waitForFunction(() => document.getElementById("status").textContent === "All changes saved", null, { timeout: 8000 });

  // 6. download SCORM 1.2 from the Check and download page
  await page.click("#downloadBtn");
  check(await page.isVisible("text=Ready to download"), "check page says ready");
  await page.screenshot({ path: outDir + "/download.png" });
  const [dl] = await Promise.all([page.waitForEvent("download"), page.click("text=Download SCORM 1.2")]);
  const file = outDir + "/" + dl.suggestedFilename();
  await dl.saveAs(file);
  check(/_SCORM12\.zip$/.test(file), "SCORM 1.2 file downloaded: " + dl.suggestedFilename());

  // 7. go back to the previous save
  await page.click("text=Go back to the previous save");
  await page.waitForFunction(() => /previous save/.test(document.getElementById("status").textContent));
  api = await (await page.request.get(`${base}/api/builder/${job}/course${Q}`)).json();
  check(api.course.settings.accent !== "#1d5c4a", "going back undid the last change");

  // 8. phone width
  await page.setViewportSize({ width: 390, height: 844 });
  await page.click("text=Course settings");
  check(await page.evaluate(() => document.documentElement.scrollWidth <= 390), "editor fits a phone screen");
  check(errors.length === 0, "no JavaScript errors" + (errors.length ? ": " + errors.join(" | ") : ""));
  await browser.close();
  console.log(failures ? `\n${failures} check(s) failed` : "\nAll checks passed");
  process.exit(failures ? 1 : 0);
})();
