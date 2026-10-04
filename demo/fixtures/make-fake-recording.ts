// Fabricates out/<theme>/raw.webm, timeline.json and two stills so the edit and check chain can run without the stack.
// Usage: node fixtures/make-fake-recording.ts [light|dark ...]   Everything lands under demo/out/ (gitignored).
import { copyFileSync, mkdirSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { chromium } from "playwright";
import type { Page } from "playwright";
import { HEIGHT, THEMES, WIDTH, parseTheme, themeDir } from "../lib.ts";
import type { Scene, Theme, Timeline } from "../lib.ts";

interface FakeScene extends Omit<Scene, "startMs" | "endMs"> {
  seconds: number;
  heading: string;
  rows: string[];
  tint: string;
}

const ROWS = {
  receipts: ["Harbor Coffee Roasters   42.10   matched", "Northwind Paper Supply   118.00   matched", "Fernhill Courier   23.75   mismatch"],
  inbox: ["Billing question   reply drafted", "Delivery delay   reply drafted", "Newsletter   archived"],
  leads: ["Alder and Finch Ltd   3 sources", "Birchwood Dental   2 sources", "Copperline Studio   4 sources"],
};

const SCENES: FakeScene[] = [
  { id: "intro", workflow: "intro", kind: "show", speed: 1, caption: "A tour of three workflows", seconds: 2.5, heading: "Ops automation kit", rows: [], tint: "a" },
  { id: "receipts-open", workflow: "receipts", kind: "show", speed: 1, caption: "Drop receipts in a folder", seconds: 3, heading: "Receipts", rows: ROWS.receipts, tint: "b" },
  { id: "receipts-wait", workflow: "receipts", kind: "wait", speed: 8, caption: "", seconds: 4, heading: "Receipts: reading files", rows: ROWS.receipts, tint: "b" },
  { id: "receipts-done", workflow: "receipts", kind: "show", speed: 1, caption: "Mismatches are flagged for review", seconds: 6, heading: "Receipts: reconciled", rows: ROWS.receipts, tint: "b", loop: "receipts-reconcile", gif: true },
  { id: "inbox-open", workflow: "inbox", kind: "show", speed: 1, caption: "Drafts wait for approval", seconds: 3, heading: "Inbox", rows: ROWS.inbox, tint: "c", gif: true },
  { id: "inbox-wait", workflow: "inbox", kind: "wait", speed: 8, caption: "", seconds: 3, heading: "Inbox: drafting replies", rows: ROWS.inbox, tint: "c" },
  { id: "leads", workflow: "leads", kind: "show", speed: 1, caption: "Company names become CRM records", seconds: 3, heading: "Leads", rows: ROWS.leads, tint: "d" },
  { id: "outro", workflow: "outro", kind: "show", speed: 1, caption: "", seconds: 2, heading: "Done", rows: [], tint: "a" },
];

const TINTS: Record<Theme, Record<string, string>> = {
  light: { a: "#F6F7F8", b: "#E3F1EF", c: "#EAF0F7", d: "#F4EFE4" },
  dark: { a: "#0E1012", b: "#10201E", c: "#111A24", d: "#221D12" },
};

function pageHtml(theme: Theme, scene: FakeScene): string {
  const text = theme === "light" ? "#15181B" : "#E6E8EA";
  const rows = scene.rows.map((row) => `<li>${row}</li>`).join("");
  return `<!doctype html><meta charset="utf-8"><title>fake</title>
  <style>
    body { margin: 0; width: ${WIDTH}px; height: ${HEIGHT}px; background: ${TINTS[theme][scene.tint]}; color: ${text};
      font-family: "DejaVu Sans", sans-serif; padding: 80px; box-sizing: border-box; }
    h1 { font-size: 64px; margin: 0 0 32px; }
    li { font-size: 34px; line-height: 2; list-style: none; }
    p { font-size: 30px; }
  </style>
  <h1>${scene.heading}</h1><ul>${rows}</ul>
  <p>Sample data, served from localhost</p>
  <p id="count">Processed 0 of 40</p>
  <script>let n = 0; setInterval(() => { n = (n + 1) % 41; document.getElementById("count").textContent = "Processed " + n + " of 40"; }, 100);</script>`;
}

async function showScene(page: Page, theme: Theme, scene: FakeScene): Promise<void> {
  await page.setContent(pageHtml(theme, scene));
}

async function recordRaw(browser: Awaited<ReturnType<typeof chromium.launch>>, theme: Theme): Promise<Scene[]> {
  const dir = themeDir(theme);
  const videoDir = join(dir, ".video");
  rmSync(videoDir, { recursive: true, force: true });
  const context = await browser.newContext({
    viewport: { width: WIDTH, height: HEIGHT }, recordVideo: { dir: videoDir, size: { width: WIDTH, height: HEIGHT } },
  });
  const page = await context.newPage();
  const began = Date.now();
  const placed: Scene[] = [];
  for (const fake of SCENES) {
    const startMs = Date.now() - began;
    await showScene(page, theme, fake);
    await page.waitForTimeout(fake.seconds * 1000);
    const { seconds: _seconds, heading: _heading, rows: _rows, tint: _tint, ...scene } = fake;
    placed.push({ ...scene, startMs, endMs: Date.now() - began });
  }
  await context.close();
  const [file] = readdirSync(videoDir);
  copyFileSync(join(videoDir, file), join(dir, "raw.webm"));
  rmSync(videoDir, { recursive: true, force: true });
  return placed;
}

async function makeStills(browser: Awaited<ReturnType<typeof chromium.launch>>, theme: Theme): Promise<Timeline["stills"]> {
  const dir = join(themeDir(theme), "stills");
  mkdirSync(dir, { recursive: true });
  const context = await browser.newContext({ viewport: { width: WIDTH, height: HEIGHT }, deviceScaleFactor: 2 });
  const page = await context.newPage();
  const stills = [{ name: "receipts", scene: SCENES[3] }, { name: "inbox", scene: SCENES[4] }];
  for (const still of stills) {
    await showScene(page, theme, still.scene);
    await page.screenshot({ path: join(dir, `${still.name}.png`) });
  }
  await context.close();
  return stills.map((still) => ({ name: still.name, file: `stills/${still.name}.png` }));
}

async function main(): Promise<void> {
  const themes = process.argv.length > 2 ? process.argv.slice(2).map(parseTheme) : THEMES;
  const browser = await chromium.launch();
  try {
    for (const theme of themes) {
      mkdirSync(themeDir(theme), { recursive: true });
      const scenes = await recordRaw(browser, theme);
      const stills = await makeStills(browser, theme);
      const timeline: Timeline = { project: "ops-automation-kit", theme, scenes, stills };
      writeFileSync(join(themeDir(theme), "timeline.json"), JSON.stringify(timeline, null, 2));
      console.log(`fake recording written for ${theme}`);
    }
  } finally {
    await browser.close();
  }
}

await main();
