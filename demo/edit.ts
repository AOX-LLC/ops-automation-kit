// Edits out/<theme>/raw.webm into the published media, driven by out/<theme>/timeline.json.
// Usage: node edit.ts <light|dark> [--only full|gif|loops|stills]
// Outputs (all under out/<theme>/): walkthrough.mp4 .srt .vtt, readme.gif, loops/<name>.{mp4,webm}, stills-web/<name>.png
import { spawnSync } from "node:child_process";
import { mkdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { renderCards, readConfig } from "./cards.ts";
import type { Config } from "./cards.ts";
import {
  FPS, HEIGHT, WIDTH, ffmpeg, kilobytes, parseTheme, probeDurationSeconds, probeWidth, readTimeline, themeDir,
} from "./lib.ts";
import type { Scene, Theme, Timeline } from "./lib.ts";

type Part = "full" | "gif" | "loops" | "stills";
const PARTS: Part[] = ["full", "gif", "loops", "stills"];

const CROSSFADE_SECONDS = 0.3;
const GIF_LIMIT_BYTES = 4 * 1024 * 1024;
const LOOP_LIMIT_BYTES = 700 * 1024;
const LOOP_MIN_SECONDS = 5;
const LOOP_MAX_SECONDS = 8;
const STILL_LIMIT_BYTES = 250 * 1024;
const STILL_MIN_SSIM = 0.995;

interface Clip {
  scene: Scene;
  path: string;
  seconds: number;
}

interface Context {
  theme: Theme;
  dir: string;
  work: string;
  raw: string;
  timeline: Timeline;
  config: Config;
}

function sizeOf(path: string): number {
  return statSync(path).size;
}

function seconds(ms: number): string {
  return (ms / 1000).toFixed(3);
}

// Intermediate clips are near-lossless so the one lossy encode happens at the end.
function encodeIntermediate(inputArgs: string[], filter: string, out: string): void {
  ffmpeg([
    ...inputArgs, "-vf", filter, "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "14",
    "-pix_fmt", "yuv420p", "-r", String(FPS), out,
  ]);
}

function sceneFilter(speed: number): string {
  return `setpts=(PTS-STARTPTS)/${speed},fps=${FPS},scale=${WIDTH}:${HEIGHT}:flags=lanczos,format=yuv420p`;
}

function buildSceneClip(context: Context, scene: Scene): Clip {
  const path = join(context.work, `scene-${scene.id}.mp4`);
  const length = (scene.endMs - scene.startMs) / 1000;
  encodeIntermediate(
    ["-ss", seconds(scene.startMs), "-t", String(length), "-i", context.raw], sceneFilter(scene.speed), path,
  );
  return { scene, path, seconds: probeDurationSeconds(path) };
}

function buildCardClip(context: Context, name: "title" | "end", length: number): Clip {
  const path = join(context.work, `card-${name}.mp4`);
  const png = join(context.dir, "cards", `${name}.png`);
  encodeIntermediate(
    ["-loop", "1", "-framerate", String(FPS), "-t", String(length), "-i", png],
    `scale=${WIDTH}:${HEIGHT}:flags=lanczos,format=yuv420p`, path,
  );
  const scene: Scene = {
    id: name, workflow: name, startMs: 0, endMs: length * 1000, kind: "show", speed: 1, caption: "",
  };
  return { scene, path, seconds: probeDurationSeconds(path) };
}

// Concat demuxer with stream copy: every clip shares codec, size and frame rate.
function concatClips(clips: Clip[], out: string, work: string): void {
  const list = join(work, `${out.split("/").pop()}.txt`);
  writeFileSync(list, clips.map((clip) => `file '${clip.path}'\n`).join(""));
  ffmpeg(["-f", "concat", "-safe", "0", "-i", list, "-c", "copy", out]);
}

function cueTime(totalSeconds: number, separator: "," | "."): string {
  const totalMs = Math.round(totalSeconds * 1000);
  const pad = (value: number, width: number) => String(value).padStart(width, "0");
  const hours = Math.floor(totalMs / 3_600_000);
  const minutes = Math.floor((totalMs % 3_600_000) / 60_000);
  const secs = Math.floor((totalMs % 60_000) / 1000);
  return `${pad(hours, 2)}:${pad(minutes, 2)}:${pad(secs, 2)}${separator}${pad(totalMs % 1000, 3)}`;
}

interface Cue {
  start: number;
  end: number;
  text: string;
}

// The body starts one crossfade before the title card ends, so scene time = bodyStart + running total.
function buildCues(scenes: Clip[], bodyStart: number): Cue[] {
  const cues: Cue[] = [];
  let cursor = bodyStart;
  for (const clip of scenes) {
    if (clip.scene.caption.trim() !== "") {
      cues.push({ start: cursor, end: cursor + clip.seconds, text: clip.scene.caption.trim() });
    }
    cursor += clip.seconds;
  }
  return cues;
}

function writeCaptionFiles(cues: Cue[], basePath: string): void {
  const srt = cues
    .map((cue, i) => `${i + 1}\n${cueTime(cue.start, ",")} --> ${cueTime(cue.end, ",")}\n${cue.text}\n`)
    .join("\n");
  const vtt = cues
    .map((cue) => `${cueTime(cue.start, ".")} --> ${cueTime(cue.end, ".")}\n${cue.text}\n`)
    .join("\n");
  writeFileSync(`${basePath}.srt`, srt);
  writeFileSync(`${basePath}.vtt`, `WEBVTT\n\n${vtt}`);
}

function encodeFull(context: Context, title: Clip, body: string, bodySeconds: number, end: Clip, cues: Cue[]): string {
  const out = join(context.dir, "walkthrough.mp4");
  const fade = CROSSFADE_SECONDS;
  const firstOffset = title.seconds - fade;
  const secondOffset = title.seconds + bodySeconds - fade - fade;
  const normalise = (index: number) => `[${index}:v]settb=1/${FPS},fps=${FPS}[v${index}]`;
  const filter = [
    normalise(0), normalise(1), normalise(2),
    `[v0][v1]xfade=transition=fade:duration=${fade}:offset=${firstOffset.toFixed(3)}[a]`,
    `[a][v2]xfade=transition=fade:duration=${fade}:offset=${secondOffset.toFixed(3)},format=yuv420p[v]`,
  ].join(";");
  const hasCues = cues.length > 0;
  const subtitleInput = hasCues ? ["-i", join(context.dir, "walkthrough.srt")] : [];
  const subtitleMap = hasCues ? ["-map", "3:0", "-c:s", "mov_text", "-metadata:s:s:0", "language=eng"] : [];
  ffmpeg([
    "-i", title.path, "-i", body, "-i", end.path, ...subtitleInput,
    "-filter_complex", filter, "-map", "[v]", ...subtitleMap,
    "-c:v", "libx264", "-preset", "slow", "-crf", "27", "-pix_fmt", "yuv420p", "-r", String(FPS),
    "-an", "-movflags", "+faststart", out,
  ]);
  return out;
}

function makeFull(context: Context): void {
  const scenes = context.timeline.scenes.map((scene) => buildSceneClip(context, scene));
  if (scenes.length === 0) throw new Error("timeline has no scenes");
  const title = buildCardClip(context, "title", context.config.titleCardSeconds);
  const end = buildCardClip(context, "end", context.config.endCardSeconds);
  const body = join(context.work, "body.mp4");
  concatClips(scenes, body, context.work);
  const bodySeconds = scenes.reduce((sum, clip) => sum + clip.seconds, 0);
  const cues = buildCues(scenes, title.seconds - CROSSFADE_SECONDS);
  writeCaptionFiles(cues, join(context.dir, "walkthrough"));
  const out = encodeFull(context, title, body, bodySeconds, end, cues);
  console.log(`full: ${out} ${kilobytes(sizeOf(out))}, ${cues.length} caption cues`);
}

interface GifStep {
  width: number;
  fps: number;
  colors: number;
}

const GIF_STEPS: GifStep[] = [
  { width: 800, fps: 12, colors: 256 },
  { width: 800, fps: 10, colors: 192 },
  { width: 720, fps: 10, colors: 128 },
  { width: 640, fps: 8, colors: 96 },
  { width: 560, fps: 8, colors: 64 },
];

function encodeGif(source: string, step: GifStep, palette: string, out: string): void {
  const scale = `fps=${step.fps},scale=${step.width}:-1:flags=lanczos`;
  ffmpeg([
    "-i", source, "-vf", `${scale},palettegen=max_colors=${step.colors}:stats_mode=diff`, palette,
  ]);
  ffmpeg([
    "-i", source, "-i", palette, "-lavfi",
    `${scale}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle`, "-loop", "0", out,
  ]);
}

function makeGif(context: Context): void {
  const scenes = context.timeline.scenes.filter((scene) => scene.gif);
  if (scenes.length === 0) return console.log("gif: no scene has gif:true, skipped");
  const clips = scenes.map((scene) => buildSceneClip(context, scene));
  const source = join(context.work, "gif-source.mp4");
  concatClips(clips, source, context.work);
  const out = join(context.dir, "readme.gif");
  const palette = join(context.work, "palette.png");
  for (const step of GIF_STEPS) {
    encodeGif(source, step, palette, out);
    const bytes = sizeOf(out);
    console.log(`gif: ${step.width}px ${step.fps}fps ${step.colors} colours -> ${kilobytes(bytes)}`);
    if (bytes <= GIF_LIMIT_BYTES) return;
  }
  console.warn(`gif: still over ${kilobytes(GIF_LIMIT_BYTES)} at the smallest step; shorten the gif scenes`);
}

// Tries each quality in turn and stops at the first result under the limit; keeps the last otherwise.
function encodeUnder(limit: number, qualities: number[], build: (quality: number) => void, out: string): number {
  let bytes = Infinity;
  for (const quality of qualities) {
    build(quality);
    bytes = sizeOf(out);
    if (bytes <= limit) break;
  }
  return bytes;
}

function loopFilter(scene: Scene): string {
  const natural = (scene.endMs - scene.startMs) / 1000 / scene.speed;
  const extraSpeed = natural > LOOP_MAX_SECONDS ? natural / LOOP_MAX_SECONDS : 1;
  // A scene shorter than the minimum holds its last frame so the loop is never a blink.
  const pad = natural < LOOP_MIN_SECONDS ? `,tpad=stop_mode=clone:stop_duration=${LOOP_MIN_SECONDS - natural}` : "";
  return `setpts=(PTS-STARTPTS)/${scene.speed * extraSpeed},fps=${FPS},scale=960:600:flags=lanczos${pad},format=yuv420p`;
}

function makeLoop(context: Context, scene: Scene, name: string): void {
  if (!/^[A-Za-z0-9_-]+$/.test(name)) throw new Error(`loop name "${name}" must be letters, digits, - or _`);
  const dir = join(context.dir, "loops");
  mkdirSync(dir, { recursive: true });
  const input = ["-ss", seconds(scene.startMs), "-t", String((scene.endMs - scene.startMs) / 1000), "-i", context.raw];
  const common = ["-vf", loopFilter(scene), "-t", String(LOOP_MAX_SECONDS), "-an"];
  const mp4 = join(dir, `${name}.mp4`);
  const webm = join(dir, `${name}.webm`);
  const mp4Bytes = encodeUnder(LOOP_LIMIT_BYTES, [30, 33, 36, 39], (crf) => ffmpeg([
    ...input, ...common, "-c:v", "libx264", "-preset", "slow", "-crf", String(crf), "-pix_fmt", "yuv420p",
    "-movflags", "+faststart", mp4,
  ]), mp4);
  const webmBytes = encodeUnder(LOOP_LIMIT_BYTES, [38, 41, 44, 47], (crf) => ffmpeg([
    ...input, ...common, "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", String(crf), "-row-mt", "1", "-pix_fmt", "yuv420p", webm,
  ]), webm);
  console.log(`loop ${name}: mp4 ${kilobytes(mp4Bytes)}, webm ${kilobytes(webmBytes)}`);
}

function makeLoops(context: Context): void {
  const scenes = context.timeline.scenes.filter((scene) => scene.loop);
  if (scenes.length === 0) return console.log("loops: no scene has a loop name, skipped");
  for (const scene of scenes) makeLoop(context, scene, scene.loop as string);
}

function ssimBetween(a: string, b: string): number {
  const result = spawnSync("ffmpeg", ["-hide_banner", "-i", a, "-i", b, "-lavfi", "ssim", "-f", "null", "-"], {
    encoding: "utf8",
  });
  const match = /All:([0-9.]+)/.exec(result.stderr);
  return match ? Number(match[1]) : 0;
}

function optimizeStill(input: string, out: string, work: string): string {
  const scale = probeWidth(input) > WIDTH ? `scale=${WIDTH}:-1:flags=lanczos` : "null";
  ffmpeg(["-i", input, "-vf", scale, "-compression_level", "9", "-pred", "mixed", out]);
  if (sizeOf(out) <= STILL_LIMIT_BYTES) return "downscaled";
  const quantized = join(work, "still-quantized.png");
  ffmpeg([
    "-i", out, "-vf", "split[a][b];[a]palettegen=max_colors=256:stats_mode=full[p];[b][p]paletteuse=dither=none",
    "-compression_level", "9", quantized,
  ]);
  if (sizeOf(quantized) >= sizeOf(out) || ssimBetween(quantized, out) < STILL_MIN_SSIM) return "downscaled";
  ffmpeg(["-i", quantized, "-c", "copy", out]);
  return "256-colour";
}

// Written beside the originals, never over them: the 2x source is the only copy of the high-resolution still.
function makeStills(context: Context): void {
  const dir = join(context.dir, "stills-web");
  mkdirSync(dir, { recursive: true });
  for (const still of context.timeline.stills) {
    const out = join(dir, `${still.name}.png`);
    const method = optimizeStill(join(context.dir, still.file), out, context.work);
    console.log(`still ${still.name}: ${method}, ${kilobytes(sizeOf(out))}${sizeOf(out) > STILL_LIMIT_BYTES ? " (over target)" : ""}`);
  }
}

function parseArgs(argv: string[]): { theme: Theme; parts: Part[] } {
  const theme = parseTheme(argv[0]);
  const flag = argv.indexOf("--only");
  if (flag === -1) return { theme, parts: PARTS };
  const part = argv[flag + 1] as Part;
  if (!PARTS.includes(part)) throw new Error(`--only must be one of ${PARTS.join(", ")}`);
  return { theme, parts: [part] };
}

async function main(): Promise<void> {
  const { theme, parts } = parseArgs(process.argv.slice(2));
  const dir = themeDir(theme);
  const work = join(dir, ".work");
  rmSync(work, { recursive: true, force: true });
  mkdirSync(work, { recursive: true });
  const context: Context = {
    theme, dir, work, raw: join(dir, "raw.webm"), timeline: readTimeline(theme), config: readConfig(),
  };
  if (parts.includes("full")) {
    await renderCards([theme]);
    makeFull(context);
  }
  if (parts.includes("gif")) makeGif(context);
  if (parts.includes("loops")) makeLoops(context);
  if (parts.includes("stills")) makeStills(context);
  rmSync(work, { recursive: true, force: true });
}

await main();
