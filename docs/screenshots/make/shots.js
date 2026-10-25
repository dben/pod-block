// Takes the README screenshots from the seeded app. usage: node shots.js OUT_DIR [name...]
const { chromium } = require('playwright');
const APP = 'http://127.0.0.1:8000';
const out = process.argv[2];
const only = process.argv.slice(3);

// The box around everything the selectors match, with some room around it.
async function around(page, selectors, pad = 16) {
  const boxes = [];
  for (const s of selectors) {
    const loc = page.locator(s);
    for (let i = 0; i < await loc.count(); i++) boxes.push(await loc.nth(i).boundingBox());
  }
  const x = Math.max(0, Math.min(...boxes.map(b => b.x)) - pad), y = Math.max(0, Math.min(...boxes.map(b => b.y)) - pad);
  const right = Math.max(...boxes.map(b => b.x + b.width)) + pad, bottom = Math.max(...boxes.map(b => b.y + b.height)) + pad;
  return { clip: { x, y, width: right - x, height: bottom - y }, fullPage: true };
}

async function episode(page, podcast, title) {
  await page.goto(`${APP}/podcasts/${podcast}`);
  await page.getByRole('link', { name: title, exact: true }).click();
  await page.waitForLoadState('networkidle');
}

async function timeline(page) {
  await episode(page, 1, 'Printers, again');
  await page.waitForSelector('.tl-card:not(.tl-loading)');
  await page.waitForTimeout(800);
}

const SHOTS = {
  async timeline(page) {
    await timeline(page);
    return around(page, ['.tl-card', '.sections-card']);
  },
  async home(page) {
    await page.goto(APP + '/');
    return around(page, ['.topbar', 'main.page'], 0);
  },
  async podcast(page) {
    await page.goto(APP + '/podcasts/2');
    await page.waitForLoadState('networkidle');
    const rows = page.locator('.ep:not(.ep-expired) .ep-select');
    await rows.nth(0).click();
    await rows.nth(1).click({ modifiers: ['Shift'] });
    await page.waitForTimeout(500);
    return {};
  },
  async filters(page) {
    await page.goto(APP + '/podcasts/3?tab=filters');
    return around(page, ['#tabs', '.filters-panel'], 10);
  },
  async settings(page) {
    await page.goto(APP + '/podcasts/1?tab=settings');
    return around(page, ['#tabs', '.settings-panel'], 10);
  },
  async feed(page) {
    await page.goto(APP + '/podcasts/1?tab=feed');
    return around(page, ['#tabs', '.feed-panel'], 10);
  },
  async captions(page) {
    await episode(page, 1, 'Printers, again');
    await page.evaluate(async () => {
      const player = document.getElementById('player');
      player.currentTime = 312;
      await player.play();
    });
    // Pause part way into a line, with the one before it still showing.
    await page.waitForFunction(() => document.querySelectorAll('.captions p').length === 2
      && document.querySelector('.captions p.now'));
    await page.waitForTimeout(1000);
    await page.evaluate(() => document.getElementById('player').pause());
    await page.waitForTimeout(300);
    return around(page, ['.status-card']);
  },
  async phone(page) {
    await timeline(page);
    await page.locator('.tl-card').scrollIntoViewIfNeeded();
    await page.evaluate(() => window.scrollBy(0, -(document.querySelector('.topbar').offsetHeight + 12)));
    await page.waitForTimeout(800);
    return {};
  },
};

(async () => {
  const browser = await chromium.launch({ args: ['--autoplay-policy=no-user-gesture-required'] });
  const login = await browser.newContext();
  const page = await login.newPage();
  await page.goto(APP + '/login');
  await page.fill('input[name=username]', 'admin');
  await page.fill('input[name=password]', 'demo-key-for-screenshots');
  await page.click('button[type=submit]');
  await page.waitForLoadState('networkidle');
  const storageState = await login.storageState();
  await login.close();

  for (const [name, take] of Object.entries(SHOTS)) {
    if (only.length && !only.includes(name)) continue;
    const phone = name === 'phone';
    const ctx = await browser.newContext({
      storageState, deviceScaleFactor: 2, isMobile: phone, hasTouch: phone,
      viewport: phone ? { width: 390, height: 844 } : { width: 1280, height: name === 'podcast' ? 1180 : 900 },
    });
    const page = await ctx.newPage();
    await page.screenshot({ path: `${out}/${name}.png`, ...await take(page) });
    console.log(name);
    await ctx.close();
  }
  await browser.close();
})();
