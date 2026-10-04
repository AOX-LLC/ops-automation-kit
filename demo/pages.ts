// Demo-only pages: a browser cannot open the reconciled .xlsx or run SQL, so these render the same
// data in the portfolio tokens. Each page says where its data came from; nothing is invented.
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const font = (pkg: string, file: string) =>
  `file://${resolve(here, "node_modules/@fontsource", pkg, "files", file)}`;

export type Theme = "light" | "dark";
type Row = Record<string, string>;

const escapeHtml = (text: string) =>
  text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

function shell(theme: Theme, eyebrow: string, title: string, sub: string, body: string): string {
  const css = readFileSync(join(here, "templates/page.css"), "utf8")
    .replace("PLEX_SANS_400", font("ibm-plex-sans", "ibm-plex-sans-latin-400-normal.woff2"))
    .replace("PLEX_SANS_600", font("ibm-plex-sans", "ibm-plex-sans-latin-600-normal.woff2"))
    .replace("PLEX_MONO_400", font("ibm-plex-mono", "ibm-plex-mono-latin-400-normal.woff2"))
    .replace("SPACE_500", font("space-grotesk", "space-grotesk-latin-500-normal.woff2"));
  return `<!doctype html><html lang="en" data-theme="${theme}"><head><meta charset="utf-8"><title>${escapeHtml(title)}</title><style>${css}</style></head>
<body><header><span class="name">Ops automation kit</span><span class="badge">Sample data</span></header>
<main><p class="eyebrow">${escapeHtml(eyebrow)}</p><h1>${escapeHtml(title)}</h1><p class="sub">${escapeHtml(sub)}</p>${body}</main></body></html>`;
}

/** Rows of the first worksheet of an .xlsx, keyed by the header row. Inline strings and numbers only. */
export function readSheet(xlsxPath: string): Row[] {
  // python3 is already required by the repo; unzip is not installed everywhere.
  const xml = execFileSync(
    "python3",
    ["-c", "import sys, zipfile; sys.stdout.write(zipfile.ZipFile(sys.argv[1]).read('xl/worksheets/sheet1.xml').decode())", xlsxPath],
    { encoding: "utf8", maxBuffer: 16 * 1024 * 1024 },
  );
  const decode = (s: string) =>
    s.replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&apos;/g, "'").replace(/&amp;/g, "&");
  const rows: Record<string, string>[] = [];
  for (const rowMatch of xml.matchAll(/<row [^>]*>(.*?)<\/row>/gs)) {
    const cells: Record<string, string> = {};
    for (const cell of rowMatch[1].matchAll(/<c r="([A-Z]+)\d+"[^>]*>(?:<v>(.*?)<\/v>)?<\/c>/gs)) {
      cells[cell[1]] = decode(cell[2] ?? "");
    }
    rows.push(cells);
  }
  const header = rows[0];
  return rows.slice(1).map((cells) =>
    Object.fromEntries(Object.entries(header).map(([column, name]) => [name, cells[column] ?? ""])),
  );
}

export function newestExport(exportsDir: string): string {
  const files = readdirSync(exportsDir).filter((f) => /^reconciliation-.*\.xlsx$/.test(f)).sort();
  if (files.length === 0) throw new Error(`no reconciliation-*.xlsx in ${exportsDir}`);
  return join(exportsDir, files[files.length - 1]);
}

const dollars = (cents: string) => (cents === "" ? "" : (Number(cents) / 100).toFixed(2));

export function sheetPage(theme: Theme, rows: Row[], fileName: string): string {
  const flagged = rows.filter((r) => !["matched", "out_of_scope"].includes(r.status));
  const body = `<table><thead><tr><th>Receipt</th><th>Vendor</th><th>Date</th><th class="num">Receipt</th><th>Bank line</th><th class="num">Bank</th><th>Status</th><th>Why</th></tr></thead><tbody>
${[...flagged, ...rows.filter((r) => ["matched", "out_of_scope"].includes(r.status))]
  .slice(0, 16)
  .map((r) => {
    const isFlag = !["matched", "out_of_scope"].includes(r.status);
    const cls = r.status === "matched" ? "matched" : isFlag ? "flag" : "other";
    return `<tr class="${isFlag ? "flagged" : ""}"><td class="mono">${escapeHtml(r.receipt.replace(/^receipts\/inbox\//, ""))}</td><td>${escapeHtml(r.vendor)}</td><td class="mono">${escapeHtml(r.receipt_date)}</td><td class="num">${dollars(r.receipt_total_cents)}</td><td>${escapeHtml(r.bank_description)}</td><td class="num">${dollars(r.bank_amount_cents)}</td><td><span class="status ${cls}">${escapeHtml(r.status.replace(/_/g, " "))}</span></td><td class="quote">${escapeHtml(r.match_reason)}</td></tr>`;
  })
  .join("\n")}</tbody></table>`;
  return shell(theme, "01 / Receipts", "Reconciliation", `${rows.length} rows, ${flagged.length} flagged. Mismatches first. Read from exports/${fileName}.`, body);
}

export function crmRows(composeDir: string): Row[] {
  const sql = `select coalesce(json_agg(r order by r.name), '[]') from (
    select a.name, a.domain, a.industry, a.employee_band, a.hq_city,
           (select string_agg(s.field || ': ' || s.excerpt || ' [' || s.source_ref || ']', E'\\n' order by s.field)
              from crm.account_sources s where s.account_id = a.id and s.field in ('employee_band', 'industry')) as sources
      from crm.accounts a) r`;
  const out = execFileSync(
    "docker",
    ["compose", "exec", "-T", "postgres", "psql", "-U", "postgres", "-d", "opskit", "-tAc", sql],
    { cwd: composeDir, encoding: "utf8", maxBuffer: 16 * 1024 * 1024 },
  );
  return JSON.parse(out);
}

export function crmPage(theme: Theme, rows: Row[]): string {
  const body = `<table><thead><tr><th>Company</th><th>Industry</th><th>Size</th><th>City</th><th>Sources (field: quoted text [source])</th></tr></thead><tbody>
${rows
  .filter((r) => r.sources)
  .slice(0, 8)
  .map(
    (r) =>
      `<tr><td><strong>${escapeHtml(r.name)}</strong><br><span class="src">${escapeHtml(r.domain ?? "")}</span></td><td>${escapeHtml(r.industry ?? "")}</td><td class="mono">${escapeHtml(r.employee_band ?? "")}</td><td>${escapeHtml(r.hq_city ?? "")}</td><td class="quote">${escapeHtml(r.sources).replace(/\n/g, "<br>")}</td></tr>`,
  )
  .join("\n")}</tbody></table>`;
  return shell(theme, "02 / Leads", "CRM records", `${rows.length} accounts. Every enriched field carries the quoted text it came from. Read from the crm tables.`, body);
}

export function writePage(outDir: string, name: string, html: string): string {
  mkdirSync(outDir, { recursive: true });
  const file = join(outDir, name);
  writeFileSync(file, html);
  return `file://${file}`;
}
