import { build } from "vite";
import { writeFileSync, mkdirSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
process.chdir(root);

const outDir = path.resolve(root, "../output");
mkdirSync(outDir, { recursive: true });

const log = [];
try {
  await build({ logLevel: "info" });
  log.push("BUILD_OK");
} catch (e) {
  log.push("BUILD_FAIL");
  log.push(String((e && e.stack) || e));
}
writeFileSync(path.join(outDir, "vite_build.txt"), log.join("\n"));
console.log(log.join("\n").slice(-3000));
