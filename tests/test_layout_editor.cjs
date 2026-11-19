/* Verify the rendered layout selector and remake request in the real editor. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { chromium, devices } = require("playwright");

const site = "https://peakcut.example.test";
const html = fs.readFileSync("web/index.html", "utf8");
const layouts = {
  full_gameplay: "Full Gameplay / TikTok",
  blur: "Full frame + blurred fill",
  zoom: "Zoomed in + blurred fill",
  crop: "Fill the screen (centre crop)",
  gameplay: "Gameplay + facecam (blurred fill)",
};
const clip = { id:"clip01", title:"The last corner", hook:"THE LAST CORNER", description:"",
  start:0, end:9, score:92, version:1, status:"done", layout:"gameplay", style:"clean", file:"clip.mp4", cover:"clip.jpg" };
const job = { id:"synthetic-job", kind:"video", status:"done", title:"Synthetic race", created:1,
  duration:9, source:{ name:"Synthetic race", type:"file" }, clips:[clip], options:{layout:"gameplay"} };

(async () => {
  const browser = await chromium.launch({ executablePath:process.env.CHROMIUM_PATH || undefined, args:["--no-sandbox"] });
  try {
    const page = await browser.newPage({ ...devices["iPhone 13"] });
    let update;
    let version = 1;
    await page.route("**/*", async route => {
      const req = route.request(), url = new URL(req.url());
      if (url.origin !== site) return route.abort();
      if (url.pathname.startsWith("/app/")) return route.fulfill({body:html, contentType:"text/html"});
      if (url.pathname === "/api/status") return route.fulfill({json:{user:{name:"Fixture"}, links:{}, draft_only:true,
        styles:{clean:"Clean"}, layouts, ai:{ready:true}}});
      if (url.pathname === "/api/sync") return route.fulfill({json: url.searchParams.get("v") === String(version) ? {v:version}
        : {v:version, creators:[], jobs:[job], accounts:[], unread:0}});
      if (url.pathname === "/api/jobs/synthetic-job/clips/clip01/render" && req.method() === "POST") {
        update = req.postDataJSON();
        clip.layout = update.layout;
        clip.status = "queued";
        version += 1;
        return route.fulfill({json:job});
      }
      return route.fulfill({json:{}});
    });
    await page.goto(site + "/app/videos");
    await page.getByRole("button", {name:"Open clip: The last corner"}).click();
    const picker = page.getByRole("combobox", {name:"Layout"});
    assert.equal(await picker.inputValue(), "gameplay");
    assert.deepEqual(await picker.locator("option").allTextContents(), Object.values(layouts));
    await picker.selectOption({label:"Full Gameplay / TikTok"});
    const saved = page.waitForResponse(r => r.url().endsWith("/clips/clip01/render"));
    await page.getByRole("button", {name:"Save and re-make clip"}).click();
    await saved;
    assert.equal(update.layout, "full_gameplay");
    console.log("The mobile clip editor keeps old layouts and sends the Full Gameplay / TikTok selection to the remake API.");
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
