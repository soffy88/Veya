// adapter-node 运行时 BODY_SIZE_LIMIT 默认 512K — 大文件上传 (≤100MB) 会 413。
// systemd 服务无法注入 env (sudo 不可用) → build 后 patch 产物默认值。
// 部署环境仍可用 BODY_SIZE_LIMIT 覆盖。
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const dir = join(process.cwd(), "build", "server", "chunks");
let patched = 0;
for (const f of readdirSync(dir)) {
  if (!f.endsWith(".js")) continue;
  const p = join(dir, f);
  let src = readFileSync(p, "utf8");
  if (src.includes("'512K'") && src.includes("BODY_SIZE_LIMIT")) {
    src = src.replaceAll("'512K'", "'200m'");
    writeFileSync(p, src);
    patched++;
  }
}
console.log(`patch-body-limit: ${patched} handler 文件默认值 → 200m`);
if (patched === 0) process.exitCode = 1;
