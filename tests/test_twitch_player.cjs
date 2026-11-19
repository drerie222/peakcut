/* Real browser layout against local HTML, with the Twitch iframe and API simulated. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { chromium, devices } = require("playwright");

const html = fs.readFileSync("web/index.html", "utf8");
const site = "https://peakcut.example.test";
const channel = "https://www.twitch.tv/example_player";
const fixture = {
  id: "example", name: "Player test", channel, channels: { twitch: channel }, accounts: [],
  profiles: { twitch: { channel, live: true, embed: "example_player", title: "Fixture" } },
};

async function serve(page) {
  page.on("pageerror", error => console.error("Browser error:", error.message));
  let writes = 0;
  await page.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.hostname === "player.twitch.tv")
      return route.fulfill({ body: "Preparing your stream…", contentType: "text/html" });
    if (url.origin !== site) return route.abort();
    if (request.method() !== "GET") writes++;
    if (url.pathname.startsWith("/app/")) return route.fulfill({ body: html, contentType: "text/html" });
    if (url.pathname === "/api/status") return route.fulfill({ json: { styles: {}, layouts: {}, user: {name:"Fixture"}, links: {}, ai: {ready:true} } });
    if (url.pathname === "/api/sync") return route.fulfill({ json: { jobs: [], creators: [fixture], accounts: [], v: 1 } });
    return route.fulfill({ json: {} });
  });
  await page.goto(site + "/app/creators/example");
  return () => writes;
}

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined, args: ["--no-sandbox"] });
  try {
    const mobile = await browser.newPage({ ...devices["iPhone 13"] });
    const mobileWrites = await serve(mobile);
    const mobileLink = mobile.getByRole("link", { name: "Open on Twitch" });
    await mobileLink.waitFor();
    assert.equal(await mobileLink.getAttribute("href"), channel);
    assert.equal(await mobile.locator(".player iframe").count(), 0);
    await mobile.getByRole("button", { name: "Record this stream" }).waitFor();
    assert.equal(mobileWrites(), 0);
    await mobile.close();

    const desktop = await browser.newPage({ viewport: { width: 1500, height: 900 } });
    const desktopWrites = await serve(desktop);
    await desktop.getByRole("button", { name: "Start preview" }).click();
    const frame = desktop.locator(".player iframe");
    await frame.waitFor();
    const src = new URL(await frame.getAttribute("src"));
    assert.equal(src.hostname, "player.twitch.tv");
    assert.equal(src.searchParams.get("parent"), "peakcut.example.test");
    assert.equal(src.searchParams.get("muted"), "true");
    assert.equal(src.searchParams.get("channel"), "example_player");
    assert.equal(await desktop.getByRole("link", { name: "Open on Twitch" }).getAttribute("href"), channel);
    assert.equal(desktopWrites(), 0);
    console.log("Mobile Twitch fallback and desktop preview verified without contacting Twitch or starting a recording.");
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
