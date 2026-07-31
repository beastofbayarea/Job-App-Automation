import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

import ExcelJS from "exceljs";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const dataDir = path.resolve(repoRoot, "data");
const backupRoot = path.resolve(repoRoot, "output", "workbook-backups");

const workbookPlans = [
  {
    source: "ashby_roles.xlsx",
    target: "ashby_product_management_jobs.xlsx",
    outputSheets: [{ name: "Product Management Jobs" }],
  },
  {
    source: "greenhouse_roles_all.xlsx",
    target: "greenhouse_all_jobs.xlsx",
    outputSheets: [{ name: "All Jobs" }],
  },
  {
    source: "greenhouse_roles_marketing.xlsx",
    target: "greenhouse_marketing_jobs.xlsx",
    outputSheets: [{ name: "Marketing Jobs" }],
  },
  {
    source: "greenhouse_roles_pm.xlsx",
    target: "greenhouse_product_management_jobs.xlsx",
    outputSheets: [{ name: "Product Management Jobs" }],
  },
  {
    source: "lever_roles.xlsx",
    target: "lever_product_management_jobs.xlsx",
    outputSheets: [{ name: "Product Management Jobs" }],
  },
  {
    source: "smartrecruiters_workable.xlsx",
    target: "smartrecruiters_and_workable_jobs.xlsx",
    splitByPlatform: true,
    outputSheets: [
      { name: "SmartRecruiters Jobs", platform: "smartrecruiters" },
      { name: "Workable Jobs", platform: "workable" },
    ],
  },
];

// These URLs were confirmed closed by a live audit on 2026-08-01. Ambiguous
// responses (CDN blocks, timeouts, and the Waymo route) are intentionally kept.
const confirmedStaleUrls = new Set([
  "https://job-boards.greenhouse.io/snyk/jobs/8044471002",
  "https://www.mongodb.com/careers/job/?gh_jid=7705474",
  "https://instacart.careers/job/?gh_jid=8024428",
  "https://job-boards.greenhouse.io/junglescout/jobs/5802311004",
  "https://job-boards.greenhouse.io/fireworksai/jobs/4260883009",
  "https://jobs.lever.co/360learning/aac4031b-ef0e-4d2f-b85e-67ecb3b1814c",
  "https://jobs.lever.co/BestEgg/b35192ff-641c-424a-8e60-754692b070ef",
  "https://jobs.lever.co/canarytechnologies/d0795f41-1634-4c7a-9363-79a5bd452191",
  "https://jobs.lever.co/captivateiq/72db91f9-0769-46cd-a3d6-d306e7d2d9cb",
  "https://jobs.lever.co/coupa/6b4ad629-87e3-48a0-afcb-72fc329b8fbc",
  "https://jobs.lever.co/digital-matter/96176eeb-e529-49ac-ac66-87a34caed3e5",
  "https://jobs.lever.co/dlocal/decae0d6-2f4e-457d-9c9f-541ca20804dc",
  "https://jobs.lever.co/dnb/703edbf0-26a3-4967-8ce0-410c26ccf170",
  "https://jobs.lever.co/employ/bf3acbd2-1b22-40e7-baf3-89dbf6062ee1",
  "https://jobs.lever.co/field-ai/616afb10-d3fc-4bf8-b5aa-85e60b279a7d",
  "https://jobs.lever.co/gearset/7f7f5735-0003-4707-be39-37ef9dc63137",
  "https://jobs.lever.co/giddyup/54555375-1b3e-4d08-b0a8-9ff40935aae6",
  "https://jobs.lever.co/greenlight/b0617a86-5b6c-4c26-bd02-5063653aa2fc",
  "https://jobs.lever.co/happyco/b44f5bbd-fe1c-49a5-8440-7bf16754cfcd",
  "https://jobs.lever.co/hihello/c5a06753-7506-47dc-a7f0-462b9ae25e01",
  "https://jobs.lever.co/kpler/d4b09f80-2f2f-4b5f-ae51-a65198876412",
  "https://jobs.lever.co/levelai/03d1f252-0676-4214-a3e8-cb08658196c7",
  "https://jobs.lever.co/lwolf/a608be42-7419-4e40-949f-1c38ff67b47c",
  "https://jobs.lever.co/matchgroup/4d76bf9a-b5ab-43b9-8e4b-87de09af799e",
  "https://jobs.lever.co/mendix/0bec0a58-df1f-4234-94eb-70fab1b95495",
  "https://jobs.lever.co/mistral/c08c3a0f-9899-4e6c-8195-8b1cc24c56ff",
  "https://jobs.lever.co/nielsen/04ca7f31-3fe7-481c-a146-c94d1b3e1e19",
  "https://jobs.lever.co/oddin/aa2dc349-5f13-44ca-8fd9-285cd86c87d8",
  "https://jobs.lever.co/patsnap/7a8728d9-89a2-4dac-a536-8bc6b94b1384",
  "https://jobs.lever.co/pigment/8eb17310-20a9-456b-85cd-668c1e6de0db",
  "https://jobs.lever.co/quantcast/730e74bc-5737-478d-8bd2-b8573c7e2dfb",
  "https://jobs.lever.co/sonarsource/1af9d9e2-9d4e-4313-a6cf-c1a1b44ea1d4",
  "https://jobs.lever.co/sugarcrm/077eb635-9d05-4d69-87fc-7a19acc89e5e",
  "https://jobs.lever.co/superside/c6f48275-b841-420a-abf8-49178c966cee",
  "https://jobs.lever.co/system1/74c9d481-20e3-4b71-9c9c-e672b805c8fa",
  "https://jobs.lever.co/tala/50956f82-56a0-4b0f-b780-fbebdc1a94c9",
  "https://jobs.lever.co/terrahq/30629642-5287-4858-a16e-4b48fb1c4551",
  "https://jobs.lever.co/trendyol/5c2577d0-6bcc-4788-99e2-e149c47bfdad",
  "https://jobs.lever.co/trustly/bcd7fc00-b599-437c-b903-4cdc24e3d018",
  "https://jobs.lever.co/unusual/acdcfa20-4e34-441b-abc3-2468c6060d4d",
  "https://jobs.lever.co/upguard/f3092f08-6d0d-4a92-a5df-e4c030e268e4",
  "https://jobs.lever.co/voodoo/6092207f-0d57-44cf-9444-4250795cb176",
  "https://jobs.lever.co/watchguard/7d2a4a83-3487-43d0-853b-e695335e33c8",
  "https://jobs.lever.co/wpromote/42145d39-a889-48b9-8287-fd8f28e550ac",
  "https://jobs.lever.co/yuno/08c6d583-5bba-4c56-81c2-9fd9991d7110",
  "https://jobs.lever.co/zurichinstruments/e5dd626e-d8cc-4dd2-a16c-534066996513",
].map(normalizeUrl));

function assertInside(parent, child) {
  const relative = path.relative(parent, child);
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error(`Refusing to modify a path outside ${parent}: ${child}`);
  }
}

function scalar(value) {
  if (value == null) return "";
  if (value instanceof Date) return value;
  if (typeof value === "object") {
    if (typeof value.text === "string") return value.text.trim();
    if (value.result != null) return scalar(value.result);
    if (Array.isArray(value.richText)) return value.richText.map((part) => part.text ?? "").join("").trim();
  }
  return String(value).trim();
}

function normalizedHeader(value) {
  return scalar(value).toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

function normalizeUrl(value) {
  const raw = scalar(value);
  if (!raw) return "";
  const parsed = new URL(raw);
  parsed.protocol = parsed.protocol.toLowerCase();
  parsed.hostname = parsed.hostname.toLowerCase();
  parsed.hash = "";
  if (parsed.pathname.length > 1) parsed.pathname = parsed.pathname.replace(/\/+$/, "");
  return parsed.toString().replace(/\/$/, "");
}

function parseDate(value) {
  if (value instanceof Date && !Number.isNaN(value.valueOf())) return value;
  const text = scalar(value);
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(text);
  return match ? new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]))) : text;
}

function getHeaderIndexes(sheet) {
  const indexes = new Map();
  sheet.getRow(1).eachCell({ includeEmpty: false }, (cell, columnNumber) => {
    indexes.set(normalizedHeader(cell.value), columnNumber);
  });
  const find = (...names) => {
    for (const name of names) if (indexes.has(name)) return indexes.get(name);
    return null;
  };
  return {
    postingDate: find("posting date", "date"),
    company: find("company"),
    title: find("job title", "title", "role"),
    location: find("location"),
    platform: find("ats platform", "platform"),
    url: find("application url", "job url", "url"),
  };
}

async function extractRows(sourcePath) {
  const workbook = new ExcelJS.Workbook();
  await workbook.xlsx.readFile(sourcePath);
  const rows = [];
  if (workbook.worksheets.length === 0) throw new Error(`No worksheet found in ${sourcePath}`);
  for (const sourceSheet of workbook.worksheets) {
    const indexes = getHeaderIndexes(sourceSheet);
    for (const required of ["company", "title", "location", "url"]) {
      if (!indexes[required]) throw new Error(`Missing ${required} column in ${sourcePath}/${sourceSheet.name}`);
    }

    for (let rowNumber = 2; rowNumber <= sourceSheet.actualRowCount; rowNumber += 1) {
      const row = sourceSheet.getRow(rowNumber);
      const url = scalar(row.getCell(indexes.url).value);
      if (!url) continue;
      const normalized = normalizeUrl(url);
      rows.push({
        postingDate: indexes.postingDate ? parseDate(row.getCell(indexes.postingDate).value) : null,
        company: scalar(row.getCell(indexes.company).value),
        jobTitle: scalar(row.getCell(indexes.title).value),
        location: scalar(row.getCell(indexes.location).value),
        platform: indexes.platform ? scalar(row.getCell(indexes.platform).value) : "",
        applicationUrl: url,
        normalizedUrl: normalized,
        sourceRow: rowNumber,
      });
    }
  }
  return rows;
}

function uniqueActiveRows(rows) {
  const seen = new Set();
  const active = [];
  let staleRemoved = 0;
  let duplicatesRemoved = 0;
  for (const row of rows) {
    if (confirmedStaleUrls.has(row.normalizedUrl)) {
      staleRemoved += 1;
      continue;
    }
    if (seen.has(row.normalizedUrl)) {
      duplicatesRemoved += 1;
      continue;
    }
    seen.add(row.normalizedUrl);
    active.push(row);
  }
  return { active, staleRemoved, duplicatesRemoved };
}

function addJobsSheet(workbook, name, rows, includeDate, tableName) {
  const sheet = workbook.addWorksheet(name, {
    views: [{ state: "frozen", ySplit: 1, showGridLines: false }],
    pageSetup: { orientation: "landscape", fitToPage: true, fitToWidth: 1, fitToHeight: 0 },
  });
  const columns = includeDate
    ? ["Posting Date", "Company", "Job Title", "Location", "Application URL"]
    : ["Company", "Job Title", "Location", "ATS Platform", "Application URL"];
  const values = rows.map((row) =>
    includeDate
      ? [row.postingDate, row.company, row.jobTitle, row.location, { text: row.applicationUrl, hyperlink: row.applicationUrl }]
      : [row.company, row.jobTitle, row.location, row.platform, { text: row.applicationUrl, hyperlink: row.applicationUrl }],
  );

  sheet.addTable({
    name: tableName,
    ref: "A1",
    headerRow: true,
    totalsRow: false,
    style: { theme: "TableStyleMedium2", showRowStripes: true, showFirstColumn: false, showLastColumn: false },
    columns: columns.map((columnName) => ({ name: columnName, filterButton: true })),
    rows: values,
  });

  sheet.getRow(1).height = 24;
  sheet.getRow(1).font = { name: "Aptos", size: 11, bold: true, color: { argb: "FFFFFFFF" } };
  sheet.getRow(1).alignment = { vertical: "middle", horizontal: "left" };
  sheet.eachRow({ includeEmpty: false }, (row, rowNumber) => {
    if (rowNumber === 1) return;
    row.height = 30;
    row.alignment = { vertical: "middle", horizontal: "left" };
    row.font = { name: "Aptos", size: 10 };
  });

  const widths = includeDate ? [14, 26, 50, 42, 70] : [26, 50, 42, 18, 70];
  widths.forEach((width, index) => {
    sheet.getColumn(index + 1).width = width;
  });
  const wrappedColumns = includeDate ? [2, 3, 4] : [1, 2, 3, 4];
  for (const columnNumber of wrappedColumns) {
    sheet.getColumn(columnNumber).alignment = { vertical: "middle", horizontal: "left", wrapText: true };
  }
  if (includeDate && rows.length > 0) sheet.getColumn(1).numFmt = "yyyy-mm-dd";
  const urlColumn = includeDate ? 5 : 5;
  sheet.getColumn(urlColumn).font = { name: "Aptos", size: 10, color: { argb: "FF0563C1" }, underline: true };
  return sheet;
}

async function buildWorkbook(plan, rows, temporaryPath) {
  const workbook = new ExcelJS.Workbook();
  workbook.creator = "Job App Automation";
  workbook.lastModifiedBy = "Job App Automation";
  workbook.created = new Date();
  workbook.modified = new Date();

  const summaries = [];
  for (let index = 0; index < plan.outputSheets.length; index += 1) {
    const outputSheet = plan.outputSheets[index];
    const scopedRows = outputSheet.platform
      ? rows.filter((row) => row.platform.toLowerCase() === outputSheet.platform)
      : rows;
    const { active, staleRemoved, duplicatesRemoved } = uniqueActiveRows(scopedRows);
    addJobsSheet(
      workbook,
      outputSheet.name,
      active,
      !plan.splitByPlatform,
      `JobsTable${workbookPlans.indexOf(plan) + 1}_${index + 1}`,
    );
    summaries.push({ sheet: outputSheet.name, rows: active.length, staleRemoved, duplicatesRemoved });
  }

  await workbook.xlsx.writeFile(temporaryPath);
  return summaries;
}

async function validateWorkbook(filePath, expectedSheets) {
  const workbook = new ExcelJS.Workbook();
  await workbook.xlsx.readFile(filePath);
  const actualNames = workbook.worksheets.map((sheet) => sheet.name);
  const expectedNames = expectedSheets.map((sheet) => sheet.name);
  if (JSON.stringify(actualNames) !== JSON.stringify(expectedNames)) {
    throw new Error(`Unexpected sheets in ${filePath}: ${actualNames.join(", ")}`);
  }
  for (const sheet of workbook.worksheets) {
    if (sheet.actualRowCount < 2 || sheet.actualColumnCount !== 5) {
      throw new Error(`Invalid used range in ${filePath}/${sheet.name}: ${sheet.actualRowCount}x${sheet.actualColumnCount}`);
    }
    const headers = sheet.getRow(1).values.slice(1).map(scalar);
    if (!headers.includes("Application URL")) throw new Error(`Missing URL header in ${filePath}/${sheet.name}`);
    const urlColumn = headers.indexOf("Application URL") + 1;
    const seen = new Set();
    for (let rowNumber = 2; rowNumber <= sheet.actualRowCount; rowNumber += 1) {
      const url = scalar(sheet.getRow(rowNumber).getCell(urlColumn).value);
      if (!/^https:\/\//i.test(url)) throw new Error(`Invalid URL in ${filePath}/${sheet.name}!R${rowNumber}`);
      const normalized = normalizeUrl(url);
      if (seen.has(normalized)) throw new Error(`Duplicate URL in ${filePath}/${sheet.name}!R${rowNumber}`);
      seen.add(normalized);
    }
  }
  return workbook.worksheets.map((sheet) => ({ sheet: sheet.name, rows: sheet.actualRowCount - 1 }));
}

async function checkCanonicalWorkbooks() {
  const report = [];
  for (const plan of workbookPlans) {
    const targetPath = path.resolve(dataDir, plan.target);
    assertInside(dataDir, targetPath);
    await fs.access(targetPath);
    report.push({ workbook: plan.target, sheets: await validateWorkbook(targetPath, plan.outputSheets) });
  }
  console.log(JSON.stringify({ valid: true, workbooks: report }, null, 2));
}

async function main() {
  await fs.mkdir(dataDir, { recursive: true });
  for (const entry of await fs.readdir(dataDir, { withFileTypes: true })) {
    if (entry.isFile() && entry.name.startsWith(".") && entry.name.endsWith(".tmp.xlsx")) {
      const staleTemporaryPath = path.resolve(dataDir, entry.name);
      assertInside(dataDir, staleTemporaryPath);
      await fs.rm(staleTemporaryPath);
    }
  }
  const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
  const backupDir = path.resolve(backupRoot, timestamp);
  const staged = [];

  for (const plan of workbookPlans) {
    const sourcePath = path.resolve(dataDir, plan.source);
    const targetPath = path.resolve(dataDir, plan.target);
    assertInside(dataDir, sourcePath);
    assertInside(dataDir, targetPath);
    const sourceExists = await fs.access(sourcePath).then(() => true).catch(() => false);
    const targetExists = await fs.access(targetPath).then(() => true).catch(() => false);
    const effectiveSource = sourceExists ? sourcePath : targetExists ? targetPath : null;
    if (!effectiveSource) throw new Error(`Missing source workbook: ${plan.source}`);

    const rows = await extractRows(effectiveSource);
    const temporaryPath = path.resolve(dataDir, `.${plan.target}.${process.pid}.tmp.xlsx`);
    assertInside(dataDir, temporaryPath);
    const summaries = await buildWorkbook(plan, rows, temporaryPath);
    await validateWorkbook(temporaryPath, plan.outputSheets);
    staged.push({ plan, sourcePath: effectiveSource, targetPath, temporaryPath, summaries });
  }

  await fs.mkdir(backupDir, { recursive: true });
  for (const item of staged) {
    const pathsToBackUp = new Set([item.sourcePath]);
    if (item.targetPath !== item.sourcePath && await fs.access(item.targetPath).then(() => true).catch(() => false)) {
      pathsToBackUp.add(item.targetPath);
    }
    for (const existingPath of pathsToBackUp) {
      const backupPath = path.resolve(backupDir, path.basename(existingPath));
      assertInside(backupDir, backupPath);
      await fs.rename(existingPath, backupPath);
    }
    await fs.rename(item.temporaryPath, item.targetPath);
    await validateWorkbook(item.targetPath, item.plan.outputSheets);
  }

  const report = staged.map((item) => ({
    source: path.basename(item.sourcePath),
    target: path.basename(item.targetPath),
    sheets: item.summaries,
  }));
  console.log(JSON.stringify({ backupDir, workbooks: report }, null, 2));
}

async function run() {
  const args = new Set(process.argv.slice(2));
  if (args.has("--help")) {
    console.log("Usage: node scripts/cleanup_job_url_workbooks.mjs [--check]");
    console.log("  no arguments  Clean, validate, back up, and rename the private workbooks.");
    console.log("  --check       Validate canonical workbooks without modifying them.");
    return;
  }
  if (args.has("--check")) {
    if (args.size > 1) throw new Error(`Unknown argument(s): ${[...args].filter((arg) => arg !== "--check").join(", ")}`);
    await checkCanonicalWorkbooks();
    return;
  }
  if (args.size > 0) throw new Error(`Unknown argument(s): ${[...args].join(", ")}`);
  await main();
}

run().catch((error) => {
  console.error(error.stack || error.message);
  process.exitCode = 1;
});
