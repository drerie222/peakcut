/* Check that the watchlist distinguishes a skipped call, a rejection and an API failure. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { chromium, devices } = require("playwright");

const site = "https://peakcut.example.test";
const html = fs.readFileSync("web/index.html", "utf8");
const creator = { id:"gaming-fixture", name:"Quiet streamer", watchlist:{order:1, priority:"A"},
  auto_watch:true, channels:{twitch:"https://www.twitch.tv/example"}, channel:"https://www.twitch.tv/example" };
const job = { id:"sample-recording", kind:"live", status:"live", stage:"Recording", creator:creator.id, watch_broadcast:"test",
  title:"Synthetic game", source:{name:"Synthetic game", type:"live"}, clips:[], duration:360,
  live:{elapsed:360, level:[], chat:null, buffer_sec:360, checks:0, lines:[], highlight_interval:300, highlight_budget:16},
  pipeline:{candidate_count:3, visual_checks:1, live_ai_requests:2, highlight_frames_seconds:1.4},
  candidates:[
    {id:"first", start:0, end:75, created:1, signal:"Scheduled review", state:"rejected", analysis:"no_highlight",
      reason:"Routine driving"},
    {id:"second", start:100, end:175, created:2, signal:"Scheduled review", state:"waiting", analysis:"api_failed",
      due:Date.now() / 1000 + 60, wait_reason:"provider_rate"},
    {id:"third", start:230, end:300, created:3, signal:"Jump: chat", state:"failed", analysis:"image_failed",
      reason:"Could not decode the video"},
  ] };

(async () => {
  const browser = await chromium.launch({ executablePath:process.env.CHROMIUM_PATH || undefined, args:["--no-sandbox"] });
  try {
    const page = await browser.newPage({ ...devices["iPhone 13"] });
    await page.route("**/*", route => {
      const url = new URL(route.request().url());
      if (url.origin !== site) return route.abort();
      if (url.pathname.startsWith("/app/")) return route.fulfill({body:html, contentType:"text/html"});
      if (url.pathname === "/api/status") return route.fulfill({json:{user:{name:"Fixture"}, links:{}, ai:{ready:true}}});
      if (url.pathname === "/api/sync") return route.fulfill({json:{v:1, creators:[creator], jobs:[job], accounts:[]}});
      return route.fulfill({json:{}});
    });
    await page.goto(site + "/app/watchlist");
    await page.getByText("1 waiting · 0 accepted · 1 rejected", {exact:true}).click();
    const status = page.locator('[data-l="Highlights and clips"]');
    for (const message of ["No highlight found", "Routine driving", "API request failed", "Provider rate limit",
      "try after", "AI not called · frame extraction failed", "3 candidates", "2 API attempts", "1.4 s frames"]) {
      assert.ok((await status.innerText()).includes(message), message);
    }
    assert.equal(await status.getByRole("button", {name:"Retry"}).count(), 1);
    console.log("The mobile watchlist shows separate review outcomes, wait time, phase timing and a targeted retry.");
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
