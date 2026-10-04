// The README canvas stills: each workflow fitted to the frame and cropped to its nodes and sticky note.
//
//   THEME=light|dark KIT_OWNER_PASSWORD=... node canvas-stills.ts
//
// Needs the stack up with its workflows imported (canvas-stills.sh boots it). Nothing is run, so
// no data is needed. Writes out/<theme>/stills-web/canvas-<workflow>.png at deviceScaleFactor 2.
import { mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium, request } from "playwright";
import type { Page } from "playwright";

const here = dirname(fileURLToPath(import.meta.url));
const theme = process.env.THEME ?? "light";
if (theme !== "light" && theme !== "dark") throw new Error(`THEME must be light or dark, got ${JSON.stringify(theme)}`);
const N8N = `http://127.0.0.1:${process.env.KIT_N8N_PORT ?? 4300}`;
const password = process.env.KIT_OWNER_PASSWORD;
if (!password) throw new Error("KIT_OWNER_PASSWORD is not set");

// A wide viewport makes the fitted workflow larger on screen, so the node names are larger in the crop.
const VIEWPORT = { width: 1920, height: 1080 };
const MARGIN = 24; // CSS pixels of canvas kept around the nodes and the sticky note
// A node's own box leaves out its name and sublabel (below it) and its "+" stub (to the right).
const EXTRA_BELOW = 56;
const EXTRA_RIGHT = 40;
const WORKFLOWS = [
  { name: "receipts", id: "receipts00000001" },
  { name: "inbox", id: "inbox00000000001" },
  { name: "leads", id: "leads00000000001" },
];
// Everything on the canvas that is not the workflow: the zoom buttons and the minimap, bottom left,
// and the add/search/sticky buttons, top right.
const HIDE_CHROME = "[data-test-id='canvas-controls'], [data-test-id='canvas-minimap'], [data-test-id='canvas-controls'] ~ *, .vue-flow__panel { visibility: hidden !important; }";

const logsOpen = async (page: Page) => ((await page.locator("[data-test-id='logs-panel']").boundingBox())?.height ?? 0) > 100;

async function captureCanvas(page: Page, workflowId: string, file: string): Promise<void> {
  await page.goto(`${N8N}/workflow/${workflowId}`);
  await page.locator("[data-test-id='canvas-node']").first().waitFor({ timeout: 30000 });
  await page.waitForLoadState("networkidle", { timeout: 8000 }).catch(() => {});
  if (await logsOpen(page)) {
    await page.locator("[data-test-id='logs-overview-header']").click();
    await page.waitForFunction(() => (document.querySelector("[data-test-id='logs-panel']")?.getBoundingClientRect().height ?? 0) < 100);
  }
  await page.locator("[data-test-id='zoom-to-fit']").click();
  await page.mouse.move(VIEWPORT.width - 4, 4); // off the canvas and off every node, so no cursor and no hover state
  await page.waitForTimeout(1500);
  await page.addStyleTag({ content: HIDE_CHROME });

  // The box that holds every node and the sticky note. A node's own box includes its name.
  const box = await page.evaluate(() => {
    const parts = [...document.querySelectorAll("[data-test-id='canvas-node'], [data-test-id='sticky'], [data-test-id='canvas-handle-plus-wrapper']")];
    const rects = parts.map((el) => el.getBoundingClientRect()).filter((r) => r.width > 0 && r.height > 0);
    return {
      count: rects.length,
      left: Math.min(...rects.map((r) => r.left)),
      top: Math.min(...rects.map((r) => r.top)),
      right: Math.max(...rects.map((r) => r.right)),
      bottom: Math.max(...rects.map((r) => r.bottom)),
    };
  });
  const expected = await page.locator("[data-test-id='canvas-node']").count();
  if (box.count < expected) throw new Error(`${workflowId}: only ${box.count} of the page's elements have a size`);
  const clip = {
    x: Math.max(0, box.left - MARGIN),
    y: Math.max(0, box.top - MARGIN),
    width: 0,
    height: 0,
  };
  clip.width = Math.min(VIEWPORT.width, box.right + EXTRA_RIGHT) - clip.x;
  clip.height = Math.min(VIEWPORT.height, box.bottom + EXTRA_BELOW) - clip.y;
  if (box.right + EXTRA_RIGHT > VIEWPORT.width || box.bottom + EXTRA_BELOW > VIEWPORT.height) {
    throw new Error(`${workflowId}: the fitted workflow runs to the edge of the viewport; the crop would cut it`);
  }
  await page.screenshot({ path: file, clip });
  console.log(`${file.split("/").pop()}: ${expected} nodes, ${Math.round(clip.width)}x${Math.round(clip.height)} css px`);
}

const outDir = join(here, "out", theme, "stills-web");
mkdirSync(outDir, { recursive: true });
const api = await request.newContext();
const login = await api.post(`${N8N}/rest/login`, { data: { emailOrLdapLoginId: "owner@kit.example", password } });
if (!login.ok()) throw new Error(`n8n login failed: ${login.status()}`);
const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: VIEWPORT,
  deviceScaleFactor: 2,
  colorScheme: theme,
  storageState: (await api.storageState()) as never,
});
await context.addInitScript(`try { localStorage.setItem('N8N_THEME', '${theme}'); localStorage.setItem('theme', '${theme}'); } catch {}`);
// The roadmap rule is no third-party logos on screen, and n8n's first-visit popover can cover the canvas.
await context.addInitScript(`window.addEventListener('DOMContentLoaded', () => {
  const style = document.createElement('style');
  style.textContent = "[class*='_popoverContent_']:has([data-test-id='suggested-actions-close']) { display: none !important; }";
  document.head.appendChild(style);
});`);
const page = await context.newPage();
try {
  for (const { name, id } of WORKFLOWS) await captureCanvas(page, id, join(outDir, `canvas-${name}.png`));
} finally {
  await browser.close();
  await api.dispose();
}
