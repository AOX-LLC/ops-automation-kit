// Shared helpers for the edit pipeline: paths, the timeline contract, and process wrappers.
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

export type Theme = "light" | "dark";
export const THEMES: Theme[] = ["light", "dark"];

export interface Scene {
  id: string;
  workflow: string;
  startMs: number;
  endMs: number;
  kind: "show" | "wait";
  speed: number;
  caption: string;
  loop?: string;
  gif?: boolean;
}

export interface Still {
  name: string;
  file: string;
}

export interface Timeline {
  project: string;
  theme: Theme;
  scenes: Scene[];
  stills: Still[];
}

export const DEMO_DIR = dirname(fileURLToPath(import.meta.url));
export const REPO_DIR = join(DEMO_DIR, "..");
export const OUT_DIR = join(DEMO_DIR, "out");
export const WIDTH = 1440;
export const HEIGHT = 900;
export const FPS = 30;

export function themeDir(theme: Theme): string {
  return join(OUT_DIR, theme);
}

export function parseTheme(value: string | undefined): Theme {
  if (value === "light" || value === "dark") return value;
  throw new Error(`theme must be "light" or "dark", got ${JSON.stringify(value)}`);
}

export function readJson<T>(path: string): T {
  return JSON.parse(readFileSync(path, "utf8")) as T;
}

export function readTimeline(theme: Theme): Timeline {
  const timeline = readJson<Timeline>(join(themeDir(theme), "timeline.json"));
  for (const scene of timeline.scenes) {
    if (!(scene.endMs > scene.startMs)) throw new Error(`scene ${scene.id}: endMs must be after startMs`);
    if (!(scene.speed >= 1)) throw new Error(`scene ${scene.id}: speed must be 1 or more`);
  }
  return timeline;
}

export function run(command: string, args: string[], env: Record<string, string> = {}): string {
  const result = spawnSync(command, args, {
    encoding: "utf8",
    maxBuffer: 256 * 1024 * 1024,
    env: { ...process.env, ...env },
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    const tail = (result.stderr || "").trim().split("\n").slice(-12).join("\n");
    throw new Error(`${command} exited ${result.status}\n${tail}`);
  }
  return result.stdout;
}

export function ffmpeg(args: string[]): void {
  run("ffmpeg", ["-hide_banner", "-loglevel", "error", "-y", ...args]);
}

export function probeDurationSeconds(path: string): number {
  const out = run("ffprobe", [
    "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path,
  ]);
  return Number(out.trim());
}

export function probeWidth(path: string): number {
  const out = run("ffprobe", [
    "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width", "-of", "default=nw=1:nk=1", path,
  ]);
  return Number(out.trim());
}

export function kilobytes(bytes: number): string {
  return `${(bytes / 1024).toFixed(0)} KB`;
}
