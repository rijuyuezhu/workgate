import { build } from "esbuild";
import { cp, mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const outputRoot = resolve(root, "../src/workgate/ui/static");
const check = process.argv.includes("--check");
const temporary = check ? resolve(root, ".build-check") : outputRoot;
if (check) await rm(temporary, { recursive: true, force: true });
await mkdir(temporary, { recursive: true });

await build({
  entryPoints: [resolve(root, "src/index.js")],
  outfile: resolve(temporary, "xterm_bundle.js"),
  bundle: true,
  minify: true,
  legalComments: "none",
  platform: "browser",
  target: ["es2022"],
  format: "iife",
});
await build({
  entryPoints: [resolve(root, "src/react-files.tsx")],
  outfile: resolve(temporary, "react_files_bundle.js"),
  bundle: true,
  minify: true,
  legalComments: "none",
  platform: "browser",
  target: ["es2022"],
  format: "iife",
});
// SVAR's bundled theme includes CDN @font-face rules; WebUI stays fully
// self-hosted with strict CSP and uses its existing system font stack.
const reactStylePath = resolve(temporary, "react_files_bundle.css");
const reactStyles = (await readFile(reactStylePath, "utf8"))
  .replace(/@font-face\s*\{[^}]*\}/g, "");
if (reactStyles.includes("cdn.svar.dev")) {
  throw new Error("React Files CSS unexpectedly references external assets");
}
await writeFile(reactStylePath, reactStyles);

await cp(
  resolve(root, "node_modules/@xterm/xterm/css/xterm.css"),
  resolve(temporary, "xterm.css"),
);

const licenses = [];
for (const [name, path] of [
  ["@xterm/xterm 5.5.0", "node_modules/@xterm/xterm/LICENSE"],
  ["@xterm/addon-fit 0.10.0", "node_modules/@xterm/addon-fit/LICENSE"],
  ["@xterm/addon-image 0.8.0", "node_modules/@xterm/addon-image/LICENSE"],
]) {
  const license = (await readFile(resolve(root, path), "utf8")).trimEnd();
  licenses.push(`${name}\n${"=".repeat(name.length)}\n${license}`);
}
await writeFile(
  resolve(temporary, "xterm.LICENSE.txt"),
  `${licenses.join("\n\n")}\n`,
  "utf8",
);

const reactLicenses = [];
for (const [name, path] of [
  ["React 19.2.0", "node_modules/react/LICENSE"],
  ["React DOM 19.2.0", "node_modules/react-dom/LICENSE"],
  ["Scheduler", "node_modules/scheduler/LICENSE"],
]) {
  const license = (await readFile(resolve(root, path), "utf8")).trimEnd();
  reactLicenses.push(`${name}\n${"=".repeat(name.length)}\n${license}`);
}
const svarRoot = resolve(root, "node_modules/@svar-ui");
for (const name of (await readdir(svarRoot)).sort()) {
  const folder = resolve(svarRoot, name);
  try {
    const pkg = JSON.parse(await readFile(resolve(folder, "package.json"), "utf8"));
    const licensePath = resolve(folder, "license.txt");
    const title = `${pkg.name} ${pkg.version}`;
    const license = await readFile(licensePath, "utf8").catch((error) => {
      if (error.code !== "ENOENT") throw error;
      if (pkg.license !== "MIT") throw new Error(`Unrecognized license for ${title}`);
      return "SPDX-License-Identifier: MIT";
    });
    reactLicenses.push(`${title}\n${"=".repeat(title.length)}\n${license.trimEnd()}`);
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
}
await writeFile(resolve(temporary, "react_files.LICENSE.txt"),
  `${reactLicenses.join("\n\n")}\n`, "utf8");

if (check) {
  try {
    for (const name of [
      "xterm_bundle.js", "xterm.css", "xterm.LICENSE.txt",
      "react_files_bundle.js", "react_files_bundle.css", "react_files.LICENSE.txt",
    ]) {
      const expected = await readFile(resolve(outputRoot, name));
      const actual = await readFile(resolve(temporary, name));
      if (!expected.equals(actual)) {
        throw new Error(`${name} is stale; run npm run build in ui-terminal`);
      }
    }
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
}
