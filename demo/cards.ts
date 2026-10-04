// Renders the title card, end card and social preview from templates/card.html.
// Usage: node cards.ts [light|dark ...]   Output: out/<theme>/cards/{title,end}.png (1440x900), social.png (1280x640)
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { chromium } from "playwright";
import { DEMO_DIR, HEIGHT, THEMES, WIDTH, parseTheme, readJson, themeDir } from "./lib.ts";
import type { Theme } from "./lib.ts";

interface WorkflowLine {
  id: string;
  name: string;
  line: string;
}

export interface Config {
  eyebrow: string;
  title: string;
  subtitle: string;
  workflows: WorkflowLine[];
  sampleLabel: string;
  runLabel: string;
  runCommand: string;
  repoUrl: string;
  logos: Record<Theme, string>;
  titleCardSeconds: number;
  endCardSeconds: number;
  check: { allowedPromptPrefixes: string[] };
}

type Layout = "title" | "end" | "social";

const SIZES: Record<Layout, { width: number; height: number }> = {
  title: { width: WIDTH, height: HEIGHT },
  end: { width: WIDTH, height: HEIGHT },
  social: { width: 1280, height: 640 },
};

export function readConfig(): Config {
  return readJson<Config>(join(DEMO_DIR, "config.json"));
}

function escapeHtml(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function workflowCells(config: Config): string {
  return config.workflows
    .map(
      (w) => `<div class="workflow"><span class="workflow-id">${escapeHtml(w.id)}</span>` +
        `<span class="workflow-name">${escapeHtml(w.name)}</span>` +
        `<span class="workflow-line">${escapeHtml(w.line)}</span></div>`,
    )
    .join("");
}

function workflowChips(config: Config): string {
  return config.workflows.map((w) => `<span class="chip">${escapeHtml(w.id)} ${escapeHtml(w.name)}</span>`).join("");
}

function logoPath(config: Config, theme: Theme): string {
  const path = resolve(DEMO_DIR, config.logos[theme]);
  if (!existsSync(path)) {
    throw new Error(`The ${theme} logo is missing: ${path}. Add it to the tree or fix "logos" in demo/config.json.`);
  }
  return path;
}

function renderHtml(config: Config, theme: Theme, layout: Layout): string {
  const values: Record<string, string> = {
    theme,
    layout,
    width: String(SIZES[layout].width),
    height: String(SIZES[layout].height),
    fonts: pathToFileURL(join(DEMO_DIR, "node_modules", "@fontsource")).href,
    logo: pathToFileURL(logoPath(config, theme)).href,
    eyebrow: escapeHtml(config.eyebrow),
    title: escapeHtml(config.title),
    subtitle: escapeHtml(config.subtitle),
    sampleLabel: escapeHtml(config.sampleLabel),
    runLabel: escapeHtml(config.runLabel),
    runCommand: escapeHtml(config.runCommand),
    repoUrl: escapeHtml(config.repoUrl),
    workflowCells: workflowCells(config),
    workflowChips: workflowChips(config),
  };
  const template = readFileSync(join(DEMO_DIR, "templates", "card.html"), "utf8");
  return template.replace(/\{\{(\w+)\}\}/g, (_, key: string) => {
    if (!(key in values)) throw new Error(`card.html uses an unknown placeholder: ${key}`);
    return values[key];
  });
}

export async function renderCards(themes: Theme[]): Promise<string[]> {
  const config = readConfig();
  const browser = await chromium.launch();
  const written: string[] = [];
  try {
    for (const theme of themes) {
      const dir = join(themeDir(theme), "cards");
      mkdirSync(dir, { recursive: true });
      for (const layout of Object.keys(SIZES) as Layout[]) {
        // Written next to the PNG because file:// fonts do not load into an about:blank page.
        const htmlPath = join(dir, `_${layout}.html`);
        writeFileSync(htmlPath, renderHtml(config, theme, layout));
        const page = await browser.newPage({ viewport: SIZES[layout] });
        await page.goto(pathToFileURL(htmlPath).href);
        await page.evaluate(() => document.fonts.ready);
        const png = join(dir, `${layout}.png`);
        await page.screenshot({ path: png });
        await page.close();
        written.push(png);
      }
    }
  } finally {
    await browser.close();
  }
  return written;
}

if (import.meta.main) {
  const themes = process.argv.length > 2 ? process.argv.slice(2).map(parseTheme) : THEMES;
  for (const path of await renderCards(themes)) console.log(path);
}
