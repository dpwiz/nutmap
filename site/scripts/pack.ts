import * as path from "node:path";
import { resolveAndParseConfig, resolveAndParseConfigSettings, type ConfigContext } from "@cloudflare/config";
import {
  cleanBuildOutputDir,
  getRootConfigPath,
  readBuildOutput,
  writeAssets,
  writeRootConfig,
  writeWorkerConfig,
} from "@cloudflare/build-output-utils";
import config from "../worker.config.ts";

const root = path.resolve(import.meta.dir, "..");
const dist = path.join(root, "dist");
const mode = process.argv.includes("--mode") ? process.argv[process.argv.indexOf("--mode") + 1] : "production";
const ctx: ConfigContext = { isPreview: false, mode };

const parsed = await resolveAndParseConfig(config, ctx);
if (!parsed.success) {
  console.error("worker.config.ts is invalid:", parsed.error);
  process.exit(1);
}
const settings = await resolveAndParseConfigSettings(config, ctx);
if (!settings.success) {
  console.error("worker.config.ts settings are invalid:", settings.error);
  process.exit(1);
}
const worker = parsed.data.worker;
if (!worker) {
  console.error("worker.config.ts has no worker block");
  process.exit(1);
}

await cleanBuildOutputDir(root);
await writeAssets({ root, sourceDirectory: dist });
await writeWorkerConfig({ root, config: worker });
await writeRootConfig(root, settings.data, ctx);

const output = await readBuildOutput(root);
const w = output.workers.default;
const files = new Bun.Glob("**/*").scanSync({ cwd: w.assetsDir ?? dist, onlyFiles: true });
let count = 0;
for (const _ of files) count++;
console.log(`Build Output ${output.version} for Worker "${w.config.name}" (mode ${output.rootConfig.buildContext.mode}): ${count} asset files`);
console.log(`root config: ${path.relative(root, getRootConfigPath(root))}`);
