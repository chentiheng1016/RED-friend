#!/usr/bin/env node
"use strict";

// npm audit gate with scoped advisory exemptions.
//
// `npm audit` has no native per-advisory ignore, so exempting one known,
// risk-assessed advisory (the pip-audit `--ignore-vuln` pattern in the
// Makefile) means parsing `npm audit --json` ourselves. This script fails on
// any advisory at or above the threshold severity unless every path to it
// goes through an explicitly exempted GHSA id.
//
// Usage:
//   node scripts/node/npm_audit_gate.cjs [--audit-level=moderate] \
//     [--ignore GHSA-xxxx-xxxx-xxxx]...

const { spawnSync } = require("node:child_process");

const SEVERITY_RANK = { info: 0, low: 1, moderate: 2, high: 3, critical: 4 };

function parseArgs(argv) {
  const opts = { auditLevel: "moderate", ignore: new Set() };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg.startsWith("--audit-level=")) {
      opts.auditLevel = arg.split("=", 2)[1];
    } else if (arg === "--ignore") {
      const id = argv[++i];
      if (!id) throw new Error("--ignore requires a GHSA id");
      opts.ignore.add(id);
    } else {
      throw new Error(`unknown argument: ${arg}`);
    }
  }
  if (!(opts.auditLevel in SEVERITY_RANK)) {
    throw new Error(`unknown audit level: ${opts.auditLevel}`);
  }
  return opts;
}

function ghsaIdsOf(via) {
  // Object entries in `via` are concrete advisories; their `url` ends with
  // the GHSA id ("https://github.com/advisories/GHSA-....").
  const match = /GHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}/i.exec(via.url || "");
  return match ? match[0] : null;
}

// A package is "explained" when every advisory hitting it is exempted and
// every vulnerable dependency it inherits from is itself explained. Transitive
// `via` entries are package-name strings, so resolve to a fixpoint.
function unexplainedPackages(vulnerabilities, ignore) {
  const names = Object.keys(vulnerabilities);
  const explained = new Set();
  let changed = true;
  while (changed) {
    changed = false;
    for (const name of names) {
      if (explained.has(name)) continue;
      const ok = vulnerabilities[name].via.every((via) => {
        if (typeof via === "string") return explained.has(via);
        const id = ghsaIdsOf(via);
        return id !== null && ignore.has(id);
      });
      if (ok) {
        explained.add(name);
        changed = true;
      }
    }
  }
  return names.filter((name) => !explained.has(name));
}

function main() {
  const opts = parseArgs(process.argv.slice(2));

  const result = spawnSync("npm", ["audit", "--json"], {
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  });
  if (result.error) throw result.error;

  // npm exits 1 whenever vulnerabilities exist, so ignore the exit code and
  // judge the JSON. Anything unparseable (registry outage, npm crash) fails
  // loudly — an unverifiable audit must stay red.
  let report;
  try {
    report = JSON.parse(result.stdout);
  } catch {
    console.error("npm audit did not return parseable JSON:");
    console.error(result.stdout);
    console.error(result.stderr);
    process.exit(1);
  }
  if (report.error) {
    console.error("npm audit reported an error:");
    console.error(JSON.stringify(report.error, null, 2));
    process.exit(1);
  }

  const vulnerabilities = report.vulnerabilities || {};
  const threshold = SEVERITY_RANK[opts.auditLevel];
  const offending = unexplainedPackages(vulnerabilities, opts.ignore).filter(
    (name) => SEVERITY_RANK[vulnerabilities[name].severity] >= threshold,
  );

  if (offending.length > 0) {
    console.error(
      `npm audit gate: ${offending.length} package(s) with non-exempted ` +
        `advisories at or above "${opts.auditLevel}":`,
    );
    for (const name of offending) {
      const vuln = vulnerabilities[name];
      const advisories = vuln.via
        .filter((via) => typeof via === "object")
        .map((via) => `${ghsaIdsOf(via) || via.source}: ${via.title}`);
      console.error(`  - ${name}@${vuln.range} [${vuln.severity}]`);
      for (const line of advisories) console.error(`      ${line}`);
    }
    console.error("Full report: npm audit");
    process.exit(1);
  }

  const total = Object.keys(vulnerabilities).length;
  console.log(
    `npm audit gate: OK (${total} vulnerable package(s), all covered by ` +
      `exemptions: ${[...opts.ignore].join(", ") || "none"})`,
  );
}

main();
