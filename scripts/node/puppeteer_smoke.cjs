#!/usr/bin/env node
"use strict";

const fs = require("node:fs");

const puppeteer = require("puppeteer-extra");
const puppeteerCorePkg = require("puppeteer-core/package.json");
const StealthPlugin = require("puppeteer-extra-plugin-stealth");

const COMMON_CHROME_PATHS = [
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Chromium.app/Contents/MacOS/Chromium",
  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
  "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
];

function firstExistingPath(paths) {
  return paths.find((candidate) => candidate && fs.existsSync(candidate)) || "";
}

function chromeExecutablePath() {
  return firstExistingPath([
    process.env.PUPPETEER_EXECUTABLE_PATH,
    process.env.CHROME_PATH,
    ...COMMON_CHROME_PATHS,
  ]);
}

async function maybeLaunchSmoke() {
  if (process.env.RED_NODE_BROWSER_SMOKE_LAUNCH !== "1") {
    console.log(
      "[node-smoke] imports OK; set RED_NODE_BROWSER_SMOKE_LAUNCH=1 to launch local Chrome",
    );
    return;
  }

  const executablePath = chromeExecutablePath();
  if (!executablePath) {
    throw new Error(
      "RED_NODE_BROWSER_SMOKE_LAUNCH=1 but no Chrome executable was found. " +
        "Set PUPPETEER_EXECUTABLE_PATH or CHROME_PATH.",
    );
  }

  const browser = await puppeteer.launch({
    executablePath,
    headless: true,
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
  });
  try {
    const page = await browser.newPage();
    await page.goto(
      "data:text/html,<title>RED Node Smoke</title><main>ok</main>",
      { waitUntil: "domcontentloaded" },
    );
    const title = await page.title();
    if (title !== "RED Node Smoke") {
      throw new Error(`Unexpected smoke page title: ${title}`);
    }
    console.log(`[node-smoke] launch OK via ${executablePath}`);
  } finally {
    await browser.close();
  }
}

async function main() {
  puppeteer.use(StealthPlugin());
  console.log(`[node-smoke] puppeteer-core ${puppeteerCorePkg.version}`);
  await maybeLaunchSmoke();
}

main().catch((error) => {
  console.error(`[node-smoke] FAILED: ${error.stack || error}`);
  process.exit(1);
});
