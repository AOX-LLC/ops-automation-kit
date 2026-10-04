// Media hygiene check: OCRs frames of every published video, gif and png and flags text that must not ship.
// Usage: node check-frames.ts <paths...> | --all | --selftest
// Exit code is 1 when anything is flagged. Contact sheets for a human eyeball land in out/<theme>/contact/.
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { extname, join, relative, resolve } from "node:path";
import { promisify } from "node:util";
import { chromium } from "playwright";
import { readConfig } from "./cards.ts";
import { OUT_DIR, REPO_DIR, ffmpeg, probeWidth } from "./lib.ts";

const execFileAsync = promisify(execFile);

const MEDIA_EXTENSIONS = [".mp4", ".gif", ".webm", ".png"];
const SKIPPED_DIRECTORIES = ["contact", ".work", ".video", ".selftest"];
const PARALLEL_OCR = 4;
const SHEET_COLUMNS = 6;
const SHEET_ROWS = 4;
const OCR_UPSCALE_BELOW_WIDTH = 2000;

const IPV4 = /\b(?:\d{1,3}\.){3}\d{1,3}\b/g;
const TLDS = "com|net|org|io|dev|ai|app|co|us|uk|de|local|lan|internal|home|arpa|xyz|info|biz|cloud|me|tv|ly";
const HOSTNAME = new RegExp(
  `\\b(?:https?:\\/\\/)?(?:[a-z0-9-]+\\.)+(?:${TLDS})\\b(?::\\d+)?(?:\\/[^\\s"'<>)]*)?`, "gi",
);
const URL_WITH_SCHEME = /\bhttps?:\/\/[^\s"'<>)]+/gi;
const ALLOWED_REPO = /^(?:https?:\/\/)?github\.com\/aox-llc\/ops-automation-kit/i;
// Only `$ `: a line starting with `# ` or `%` is mostly UI text and icons read wrongly by OCR (n8n's
// "% Auto refresh", a magnifier read as `# Q`). A root or zsh prompt also carries user@host or a
// path, which SHELL_PROMPT_CONTAINS catches.
const SHELL_PROMPT_START = /^\$ /;
const SHELL_PROMPT_CONTAINS = /user@host|~\/|\/home\/|\/Users\//;

export interface Finding {
  file: string;
  time: string;
  rule: string;
  line: string;
}

interface Frame {
  time: string;
  path: string;
}

function loadDenylist(): string[] {
  const path = join(REPO_DIR, ".denylist.local");
  if (!existsSync(path)) return [];
  return readFileSync(path, "utf8")
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line !== "" && !line.startsWith("#"))
    .map((line) => line.toLowerCase())
    .concat((process.env.CHECK_EXTRA_TERMS ?? "").split(",").map((t) => t.trim().toLowerCase()).filter(Boolean));
}

function isAllowedHost(token: string): boolean {
  const host = token.replace(/^https?:\/\//i, "").split(/[/:?]/)[0].toLowerCase();
  // `api` is the Compose service name, public in compose.yaml and shown in n8n's node subtitles.
  if (host === "localhost" || host === "127.0.0.1" || host === "api" || host.endsWith(".example")) return true;
  return ALLOWED_REPO.test(token);
}

function addressRules(line: string): string[] {
  const rules: string[] = [];
  for (const address of line.match(IPV4) ?? []) {
    if (address !== "127.0.0.1") rules.push("IPv4 address");
  }
  const hosts = [...(line.match(HOSTNAME) ?? []), ...(line.match(URL_WITH_SCHEME) ?? [])];
  if (hosts.some((token) => !isAllowedHost(token))) rules.push("hostname or URL");
  return rules;
}

function promptRules(line: string, allowedPrefixes: string[]): string[] {
  const text = line.trim();
  if (allowedPrefixes.some((prefix) => text.startsWith(prefix))) return [];
  return SHELL_PROMPT_START.test(text) || SHELL_PROMPT_CONTAINS.test(text) ? ["shell prompt or path"] : [];
}

function denylistRules(line: string, denylist: string[]): string[] {
  const lower = line.toLowerCase();
  return denylist.flatMap((term, index) => (lower.includes(term) ? [`denylist term #${index + 1}`] : []));
}

export function judgeLine(line: string, allowedPrefixes: string[], denylist: string[]): string[] {
  return [
    ...addressRules(line), ...promptRules(line, allowedPrefixes), ...denylistRules(line, denylist),
  ];
}

async function ocrLines(image: string): Promise<string[]> {
  const lines = new Set<string>();
  for (const mode of ["11", "6"]) {
    const { stdout } = await execFileAsync("tesseract", [image, "-", "--psm", mode], {
      env: { ...process.env, OMP_THREAD_LIMIT: "1" },
      maxBuffer: 32 * 1024 * 1024,
    });
    for (const line of stdout.split("\n")) if (line.trim() !== "") lines.add(line.trim());
  }
  return [...lines];
}

async function mapLimit<T, R>(items: T[], limit: number, task: (item: T) => Promise<R>): Promise<R[]> {
  const results: R[] = new Array(items.length);
  let next = 0;
  async function worker(): Promise<void> {
    while (next < items.length) {
      const index = next++;
      results[index] = await task(items[index]);
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return results;
}

function extractPngFrame(file: string, work: string): Frame[] {
  const path = join(work, "png-frame.png");
  const filter = probeWidth(file) < OCR_UPSCALE_BELOW_WIDTH ? "scale=iw*2:ih*2:flags=lanczos" : "null";
  ffmpeg(["-i", file, "-vf", filter, path]);
  return [{ time: "image", path }];
}

// One frame per second, plus the first (t=0 comes with fps=1) and the last.
function extractVideoFrames(file: string, work: string): Frame[] {
  ffmpeg(["-i", file, "-vf", "fps=1", join(work, "f%04d.png")]);
  const frames = readdirSync(work)
    .filter((name) => /^f\d{4}\.png$/.test(name))
    .sort()
    .map((name, index) => ({ time: `${index}s`, path: join(work, name) }));
  const lastPath = join(work, "last.png");
  try {
    ffmpeg(["-sseof", "-0.2", "-i", file, "-update", "1", "-frames:v", "1", lastPath]);
    if (existsSync(lastPath)) frames.push({ time: "last", path: lastPath });
  } catch {
    // Some webm files carry no duration, so seeking from the end is impossible; the 1 fps frames still cover the clip.
  }
  return frames;
}

function dropRepeatedFrames(frames: Frame[]): Frame[] {
  let previous = "";
  return frames.filter((frame) => {
    const hash = createHash("md5").update(readFileSync(frame.path)).digest("hex");
    const repeated = hash === previous;
    previous = hash;
    return !repeated;
  });
}

function contactSheetPath(file: string): string {
  const inTheme = relative(OUT_DIR, file).split("/");
  const underOut = !inTheme[0].startsWith("..") && inTheme.length > 1;
  const theme = underOut && (inTheme[0] === "light" || inTheme[0] === "dark") ? inTheme[0] : "";
  const rest = theme ? inTheme.slice(1) : inTheme;
  const name = rest.join("-").replace(/\./g, "-");
  const dir = join(OUT_DIR, theme, "contact");
  mkdirSync(dir, { recursive: true });
  return join(dir, `${name}.png`);
}

function writeContactSheet(file: string, frames: Frame[], work: string): void {
  const capacity = SHEET_COLUMNS * SHEET_ROWS;
  const step = Math.max(1, frames.length / capacity);
  const chosen = Array.from({ length: Math.min(capacity, frames.length) }, (_, i) => frames[Math.floor(i * step)]);
  const sheetDir = join(work, "sheet");
  mkdirSync(sheetDir);
  chosen.forEach((frame, i) => copyFileSync(frame.path, join(sheetDir, `s${String(i).padStart(3, "0")}.png`)));
  ffmpeg([
    "-i", join(sheetDir, "s%03d.png"), "-vf",
    `scale=320:-1,tile=${SHEET_COLUMNS}x${SHEET_ROWS}:padding=4:color=black`, "-frames:v", "1", contactSheetPath(file),
  ]);
}

export async function checkFile(file: string, options: { sheet: boolean } = { sheet: true }): Promise<Finding[]> {
  const config = readConfig();
  const denylist = loadDenylist();
  const work = mkdtempSync(join(tmpdir(), "check-frames-"));
  try {
    const isImage = extname(file).toLowerCase() === ".png";
    const frames = isImage ? extractPngFrame(file, work) : extractVideoFrames(file, work);
    if (options.sheet) writeContactSheet(file, frames, work);
    const found: Finding[] = [];
    const perFrame = await mapLimit(dropRepeatedFrames(frames), PARALLEL_OCR, async (frame) => ({
      frame, lines: await ocrLines(frame.path),
    }));
    for (const { frame, lines } of perFrame) {
      for (const line of lines) {
        for (const rule of judgeLine(line, config.check.allowedPromptPrefixes, denylist)) {
          // The denylist is private, so its matches never print the OCR line.
          const shown = rule.startsWith("denylist") ? "(withheld)" : line;
          found.push({ file: relative(REPO_DIR, file), time: frame.time, rule, line: shown });
        }
      }
    }
    return found;
  } finally {
    rmSync(work, { recursive: true, force: true });
  }
}

function walk(dir: string): string[] {
  if (!existsSync(dir)) return [];
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return SKIPPED_DIRECTORIES.includes(name) ? [] : walk(path);
    return MEDIA_EXTENSIONS.includes(extname(name).toLowerCase()) ? [path] : [];
  });
}

function printFindings(findings: Finding[]): void {
  const clip = (text: string) => (text.length > 90 ? `${text.slice(0, 87)}...` : text);
  const rows = findings.map((f) => [f.file, f.time, f.rule, clip(f.line)]);
  const header = ["file", "frame", "rule", "ocr line"];
  const widths = header.map((h, i) => Math.max(h.length, ...rows.map((row) => row[i].length)));
  const format = (row: string[]) => row.map((cell, i) => cell.padEnd(widths[i])).join("  ");
  console.log([format(header), ...rows.map(format)].join("\n"));
}

async function renderTextPng(path: string, text: string): Promise<void> {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1200, height: 300 } });
  await page.setContent(
    `<body style="margin:0;background:#fff;color:#000;font:48px 'DejaVu Sans Mono',monospace;padding:80px">${text}</body>`,
  );
  await page.screenshot({ path });
  await browser.close();
}

async function selfTest(): Promise<void> {
  const dir = join(OUT_DIR, ".selftest");
  mkdirSync(dir, { recursive: true });
  const dirty = join(dir, "dirty.png");
  const clean = join(dir, "clean.png");
  process.env.CHECK_EXTRA_TERMS = "zzz-internal-name";
  await renderTextPng(dirty, "user@host:~$ ssh 100.64.0.1 zzz-internal-name");
  await renderTextPng(clean, "Sample data on localhost:4301 at github.com/AOX-LLC/ops-automation-kit");
  const dirtyRules = new Set((await checkFile(dirty, { sheet: false })).map((f) => f.rule));
  const cleanFindings = await checkFile(clean, { sheet: false });
  const expected = ["IPv4 address", "shell prompt or path"];
  const missing = expected.filter((rule) => !dirtyRules.has(rule));
  if (missing.length > 0) throw new Error(`selftest: the dirty image did not trigger: ${missing.join(", ")}`);
  if (cleanFindings.length > 0) {
    printFindings(cleanFindings);
    throw new Error("selftest: the clean image was flagged");
  }
  console.log("selftest passed: dirty image flagged by IPv4 and shell prompt rules (and the extra term); clean image passed");
}

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  if (args.includes("--selftest")) return selfTest();
  const files = args.includes("--all")
    ? [...walk(OUT_DIR), ...walk(join(REPO_DIR, "docs", "media"))]
    : args.map((arg) => resolve(arg));
  if (files.length === 0) throw new Error("usage: node check-frames.ts <paths...> | --all | --selftest");
  const findings: Finding[] = [];
  for (const file of files) {
    const fileFindings = await checkFile(file);
    console.log(`${fileFindings.length === 0 ? "clean  " : "FLAGGED"} ${relative(REPO_DIR, file)}`);
    findings.push(...fileFindings);
  }
  if (findings.length > 0) {
    console.log("");
    printFindings(findings);
    process.exitCode = 1;
  }
}

if (import.meta.main) await main();
