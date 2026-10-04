// Drives the seeded stack in replay mode and records one video per theme, plus a timeline of
// scenes (what is on screen, when, and whether it is a wait that edit.ts may speed up).
//
//   THEME=light|dark KIT_OWNER_PASSWORD=... KIT_APPROVER_PASSWORD=... KIT_WEBHOOK_TOKEN=... node walkthrough.ts
//
// The secrets come from record.sh (it reads them with `kit-login --env` and never prints them).
// Every login happens in a throwaway context before recording starts, so no password is typed
// on camera and the recorded context starts with a session cookie only.
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium, request } from "playwright";
import type { APIRequestContext, Browser, BrowserContext, Page } from "playwright";
import { crmPage, crmRows, newestExport, readSheet, sheetPage, writePage } from "./pages.ts";
import type { Theme } from "./pages.ts";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..");
const themeName = process.env.THEME ?? "light";
if (themeName !== "light" && themeName !== "dark") throw new Error(`THEME must be light or dark, got ${JSON.stringify(themeName)}`);
const theme: Theme = themeName;
const N8N = `http://127.0.0.1:${process.env.KIT_N8N_PORT ?? 4300}`;
const API = `http://127.0.0.1:${process.env.KIT_API_PORT ?? 4301}`;
const MAILPIT = `http://127.0.0.1:${process.env.KIT_MAILPIT_WEB_PORT ?? 4303}`;
const secret = (name: string) => {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is not set (run through record.sh)`);
  return value;
};

const outDir = join(here, "out", theme);
const VIEWPORT = { width: 1440, height: 900 };

interface Scene {
  id: string;
  workflow: "intro" | "receipts" | "inbox" | "leads" | "outro";
  startMs: number;
  endMs: number;
  kind: "show" | "wait";
  speed: number;
  caption: string;
  loop?: string;
  gif?: boolean;
}
const scenes: Scene[] = [];
// A scene starts when its page has settled, not when navigation began: settle() moves the mark, so
// the edit never shows a loading spinner or a half-drawn page.
let sceneStart = 0;
const stills: { name: string; file: string }[] = [];
let videoStart = 0;
const now = () => Date.now() - videoStart;

/** Run `body` as one scene: the timeline records when it started and ended on the video clock. */
async function scene(
  meta: Pick<Scene, "id" | "workflow" | "caption"> & Partial<Pick<Scene, "kind" | "speed" | "loop" | "gif">>,
  body: () => Promise<void>,
): Promise<void> {
  sceneStart = now();
  await body();
  scenes.push({ kind: "show", speed: 1, ...meta, startMs: sceneStart, endMs: now() });
}
const pause = (page: Page, ms: number) => page.waitForTimeout(ms);

/** Wait until the page is quiet (network idle where the app allows it, no visible spinner), then
 *  mark the scene's start. `ready` is the element that proves the right content is there. */
async function settle(page: Page, ready?: ReturnType<Page["locator"]>): Promise<void> {
  await ready?.waitFor({ timeout: 30000 });
  await page.waitForLoadState("networkidle", { timeout: 8000 }).catch(() => {}); // n8n keeps a socket open
  await page
    .locator("[class*='spinner' i]:visible, [class*='loading' i]:visible")
    .first()
    .waitFor({ state: "hidden", timeout: 15000 })
    .catch(() => {});
  await pause(page, 400);
  sceneStart = now();
}

async function still(page: Page, name: string): Promise<void> {
  const file = `stills/${name}.png`;
  await page.screenshot({ path: join(outDir, file) });
  stills.push({ name, file });
}

// A cursor for the video: Playwright records none, and a click with no visible pointer reads badly.
const CURSOR_SCRIPT = `
  window.addEventListener('DOMContentLoaded', () => {
    const c = document.createElement('div');
    c.setAttribute('aria-hidden', 'true');
    c.style.cssText = 'position:fixed;z-index:2147483647;width:20px;height:20px;pointer-events:none;left:-40px;top:-40px;';
    c.innerHTML = '<svg width="20" height="20" viewBox="0 0 20 20"><path d="M3 2l12 7-5.2 1.4L7.4 16z" fill="#15181B" stroke="#FFFFFF" stroke-width="1.5" stroke-linejoin="round"/></svg>';
    document.documentElement.appendChild(c);
    window.addEventListener('mousemove', (e) => { c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px'; }, true);
  });`;

// The roadmap rule is no third-party logos on screen: Mailpit draws its own in the top bar. n8n's
// first-visit "Production Checklist" popover comes back on every load and covers the canvas.
const HIDE_THIRD_PARTY_LOGOS = `
  window.addEventListener('DOMContentLoaded', () => {
    const style = document.createElement('style');
    style.textContent = '.navbar-brand img, .navbar-brand svg { visibility: hidden; } '
      + "[class*='_popoverContent_']:has([data-test-id='suggested-actions-close']) { display: none !important; }";
    document.head.appendChild(style);
  });`;

async function click(page: Page, target: ReturnType<Page["locator"]>): Promise<void> {
  const box = await target.boundingBox();
  if (!box) throw new Error("click target has no box");
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 18 });
  await pause(page, 250);
  await target.click();
}

// Theme: the approver page and the demo pages follow prefers-color-scheme; n8n and Mailpit keep
// their own preference in localStorage, set before the first paint.
async function themedContext(browser: Browser, storageState: object | undefined, video: boolean): Promise<BrowserContext> {
  const context = await browser.newContext({
    viewport: VIEWPORT,
    deviceScaleFactor: 2,
    colorScheme: theme,
    storageState: storageState as never,
    ...(video ? { recordVideo: { dir: join(outDir, "video-tmp"), size: VIEWPORT } } : {}),
  });
  await context.addInitScript(`try {
    localStorage.setItem('N8N_THEME', '${theme}');
    localStorage.setItem('theme', '${theme}');
  } catch {}`);
  await context.addInitScript(CURSOR_SCRIPT);
  await context.addInitScript(HIDE_THIRD_PARTY_LOGOS);
  return context;
}

/** Boot can answer `healthy` a moment before the first real request is served; retry a few times. */
async function retrying<T>(what: string, attempt: () => Promise<T>): Promise<T> {
  for (let tries = 1; ; tries++) {
    try {
      return await attempt();
    } catch (error) {
      if (tries >= 6) throw new Error(`${what} failed after ${tries} tries: ${String(error).split("\n")[0]}`);
      await new Promise((resolve) => setTimeout(resolve, 3000));
    }
  }
}

async function n8nSession(api: APIRequestContext): Promise<void> {
  const res = await api.post(`${N8N}/rest/login`, {
    data: { emailOrLdapLoginId: "owner@kit.example", password: secret("KIT_OWNER_PASSWORD") },
  });
  if (!res.ok()) throw new Error(`n8n login failed: ${res.status()}`);
}

async function approverSession(api: APIRequestContext): Promise<void> {
  const page = await api.get(`${API}/approver/login`);
  const csrf = /name="csrf_token" value="([^"]*)"/.exec(await page.text())?.[1] ?? "";
  const res = await api.post(`${API}/approver/login`, {
    form: { password: secret("KIT_APPROVER_PASSWORD"), csrf_token: csrf },
    maxRedirects: 0,
  });
  if (res.status() >= 400) throw new Error(`approver login failed: ${res.status()}`);
}

// Playwright's request errors list the request headers, and one of them is the webhook token, so a
// failure here is rethrown without them.
async function hook(api: APIRequestContext, path: string) {
  try {
    return await api.post(`${N8N}/webhook/${path}`, { headers: { "X-Kit-Token": secret("KIT_WEBHOOK_TOKEN") } });
  } catch (error) {
    throw new Error(`POST /webhook/${path} failed: ${String(error).split("\n")[0]}`);
  }
}

/** Newest execution of a workflow that has finished, found through the editor's own REST API. */
async function finishedExecution(api: APIRequestContext, workflowId: string, since: number): Promise<string | null> {
  const res = await api.get(`${N8N}/rest/executions`, { params: { filter: JSON.stringify({ workflowId }), limit: "10" } });
  const results = ((await res.json()).data?.results ?? []) as { id: string; status: string; startedAt: string; mode: string }[];
  const done = results.find((e) => e.mode === "webhook" && e.status === "success" && Date.parse(e.startedAt) >= since);
  return done?.id ?? null;
}

async function untilTruthy<T>(page: Page, what: string, seconds: number, probe: () => Promise<T | null | false>): Promise<T> {
  const deadline = Date.now() + seconds * 1000;
  while (Date.now() < deadline) {
    const value = await probe();
    if (value) return value;
    await pause(page, 2000);
  }
  throw new Error(`timed out waiting for ${what}`);
}

async function openCanvas(page: Page, workflowId: string): Promise<void> {
  await page.goto(`${N8N}/workflow/${workflowId}`);
  await settle(page, page.locator("[data-test-id='canvas-node']").first());
  await page.keyboard.press("1"); // zoom to fit
  await pause(page, 800);
}

/** Zoom in on the left end of the canvas and pan right along it, so every node label is legible. */
async function tourCanvas(page: Page, panSteps: number): Promise<void> {
  await page.mouse.move(150, 330); // empty canvas: no hover toolbar on a node
  await page.keyboard.down("Control");
  for (let i = 0; i < 5; i++) {
    await page.mouse.wheel(0, -120);
    await pause(page, 80);
  }
  await page.keyboard.up("Control");
  await pause(page, 1800);
  for (let i = 0; i < panSteps; i++) {
    await page.mouse.wheel(40, 0);
    await pause(page, 60);
  }
  await pause(page, 1200);
}

async function openMail(page: Page, query: string): Promise<void> {
  await page.goto(`${MAILPIT}/search?q=${encodeURIComponent(query)}`);
  const first = page.locator("a[href^='/view/']").first();
  await first.waitFor({ timeout: 20000 });
  await click(page, first);
  await settle(page, page.getByRole("tab", { name: "Link Check" }));
}

async function runWorkflow(
  page: Page,
  api: APIRequestContext,
  { workflowId, webhook, workflow, label }: { workflowId: string; webhook: string; workflow: Scene["workflow"]; label: string },
): Promise<string> {
  let executionId = "";
  await scene({ id: `${workflow}-run`, workflow, caption: `${label}: running in replay, no key and no spend`, kind: "wait", speed: 8 }, async () => {
    const since = Date.now() - 1000;
    await page.goto(`${N8N}/workflow/${workflowId}/executions`);
    await settle(page);
    const res = await hook(api, webhook);
    if (!res.ok()) throw new Error(`${webhook} returned ${res.status()}`);
    executionId = await untilTruthy(page, `${webhook} to finish`, 300, () => finishedExecution(api, workflowId, since));
  });
  return executionId;
}

async function main(): Promise<void> {
  rmSync(outDir, { recursive: true, force: true });
  mkdirSync(join(outDir, "stills"), { recursive: true });

  const browser = await chromium.launch();
  const api = await request.newContext();
  await retrying("n8n login", () => n8nSession(api));
  await retrying("approver login", () => approverSession(api));
  const state = await api.storageState();
  const context = await themedContext(browser, state, true);

  videoStart = Date.now();
  const page = await context.newPage();
  const demoPages = join(outDir, "pages");

  // ---- Receipts
  await scene({ id: "receipts-canvas", workflow: "receipts", caption: "Receipts: n8n orchestrates, the helper API does the work", loop: "receipts-canvas", gif: true }, async () => {
    await openCanvas(page, "receipts00000001");
    await still(page, "canvas-receipts");
    await pause(page, 1500);
    await tourCanvas(page, 34);
  });
  const receiptsExecution = await runWorkflow(page, api, { workflowId: "receipts00000001", webhook: "receipts-run", workflow: "receipts", label: "Receipts" });
  await scene({ id: "receipts-execution", workflow: "receipts", caption: "30 receipts extracted, then reconciled against the bank file" }, async () => {
    await page.goto(`${N8N}/workflow/receipts00000001/executions/${receiptsExecution}`);
    await settle(page, page.locator("[data-test-id='canvas-node']").first());
    await page.keyboard.press("1");
    await pause(page, 800);
    await tourCanvas(page, 34);
  });
  const sheetFile = newestExport(join(repoRoot, "exports"));
  await scene({ id: "receipts-sheet", workflow: "receipts", caption: "The reconciled spreadsheet: mismatches flagged first", gif: true, loop: "receipts-sheet" }, async () => {
    await page.goto(writePage(demoPages, "sheet.html", sheetPage(theme, readSheet(sheetFile), sheetFile.split("/").pop() ?? "")));
    await pause(page, 1000);
    await still(page, "reconciliation");
    await pause(page, 4500);
  });
  await scene({ id: "receipts-mail", workflow: "receipts", caption: "The summary email lands in Mailpit" }, async () => {
    await openMail(page, "subject:\"Receipts reconciled\"");
    await pause(page, 4000);
  });

  // ---- Inbox
  await scene({ id: "inbox-canvas", workflow: "inbox", caption: "Inbox: every email is triaged, drafts wait for a person", loop: "inbox-canvas" }, async () => {
    await openCanvas(page, "inbox00000000001");
    await tourCanvas(page, 44);
  });
  const inboxExecution = await runWorkflow(page, api, { workflowId: "inbox00000000001", webhook: "inbox-run", workflow: "inbox", label: "Inbox" });
  await scene({ id: "inbox-execution", workflow: "inbox", caption: "Injection attempts take the held branch and are never drafted" }, async () => {
    await page.goto(`${N8N}/workflow/inbox00000000001/executions/${inboxExecution}`);
    await settle(page, page.locator("[data-test-id='canvas-node']").first());
    await page.keyboard.press("1");
    await pause(page, 800);
    await tourCanvas(page, 44);
  });
  await scene({ id: "inbox-mail", workflow: "inbox", caption: "Three injection emails quarantined, with the evidence" }, async () => {
    await openMail(page, "subject:\"Inbox:\"");
    await still(page, "inbox-summary");
    await pause(page, 5000);
  });
  await scene({ id: "inbox-queue", workflow: "inbox", caption: "Drafted replies wait on the approver page. Nothing is sent yet", gif: true, loop: "inbox-approver" }, async () => {
    await untilTruthy(page, "two reply drafts", 180, async () => {
      await page.goto(`${API}/approver/`);
      return (await page.locator("a[href^='/approver/approvals/']").count()) >= 2;
    });
    await pause(page, 1000);
    await still(page, "approver-queue");
    await pause(page, 3000);
  });
  await scene({ id: "inbox-approve", workflow: "inbox", caption: "A person approves one draft, and exactly that text is sent", gif: true }, async () => {
    const links = page.locator("a[href^='/approver/approvals/']");
    const count = await links.count();
    let chosen = -1;
    for (let i = 0; i < count && chosen < 0; i++) {
      await click(page, links.nth(i));
      await pause(page, 600);
      if ((await page.getByRole("button", { name: /Approve and send/ }).count()) > 0) chosen = i;
      else await page.goBack();
    }
    if (chosen < 0) throw new Error("no draft offered 'Approve and send'");
    await pause(page, 2500);
    await still(page, "approver-detail");
    await page.mouse.wheel(0, 300);
    await pause(page, 1500);
    await click(page, page.getByRole("button", { name: /Approve and send/ }));
    await pause(page, 2500);
  });
  await scene({ id: "inbox-sent", workflow: "inbox", caption: "The approved reply arrives in Mailpit", kind: "show" }, async () => {
    await untilTruthy(page, "the approved reply", 120, async () => {
      await page.goto(`${MAILPIT}/search?q=${encodeURIComponent("from:inbox@kit.example -subject:\"Inbox:\"")}`);
      return (await page.locator("a[href^='/view/']").count()) > 0;
    });
    await click(page, page.locator("a[href^='/view/']").first());
    await settle(page, page.getByRole("tab", { name: "Link Check" }));
    await pause(page, 4000);
  });

  // ---- Leads
  await scene({ id: "leads-canvas", workflow: "leads", caption: "Leads: company names in, researched CRM records out", loop: "leads-canvas" }, async () => {
    await openCanvas(page, "leads00000000001");
    await tourCanvas(page, 24);
  });
  const leadsExecution = await runWorkflow(page, api, { workflowId: "leads00000000001", webhook: "leads-run", workflow: "leads", label: "Leads" });
  await scene({ id: "leads-execution", workflow: "leads", caption: "17 companies researched, 3 reported as having no website" }, async () => {
    await page.goto(`${N8N}/workflow/leads00000000001/executions/${leadsExecution}`);
    await settle(page, page.locator("[data-test-id='canvas-node']").first());
    await page.keyboard.press("1");
    await pause(page, 800);
    await tourCanvas(page, 24);
  });
  await scene({ id: "leads-crm", workflow: "leads", caption: "Every CRM field carries the quoted text and source it came from", gif: true, loop: "leads-crm" }, async () => {
    await page.goto(writePage(demoPages, "crm.html", crmPage(theme, crmRows(repoRoot))));
    await pause(page, 1000);
    await still(page, "crm-records");
    await pause(page, 6000);
  });
  await scene({ id: "leads-mail", workflow: "leads", caption: "The summary email: added, updated, no website" }, async () => {
    await openMail(page, "subject:\"Leads:\"");
    await pause(page, 4000);
  });

  const video = page.video();
  await context.close();
  await video?.saveAs(join(outDir, "raw.webm"));
  rmSync(join(outDir, "video-tmp"), { recursive: true, force: true });
  await browser.close();
  await api.dispose();

  writeFileSync(
    join(outDir, "timeline.json"),
    JSON.stringify({ project: "ops-automation-kit", theme, viewport: VIEWPORT, scenes, stills }, null, 2),
  );
  console.log(`recorded ${scenes.length} scenes, ${stills.length} stills -> ${outDir}`);
}

main().catch((error) => {
  console.error(String(error instanceof Error ? error.message : error).split("\n")[0]);
  process.exit(1);
});
