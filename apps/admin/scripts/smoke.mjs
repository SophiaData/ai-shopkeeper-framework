/**
 * 构建产物冒烟：检查 index.html 与入口资源存在，且不含外网 CDN。
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(fileURLToPath(import.meta.url));
const dist = path.resolve(root, "../../../src/shopkeeper/admin/dist");
const index = path.join(dist, "index.html");

if (!fs.existsSync(index)) {
  console.error("FAIL: missing", index);
  process.exit(1);
}
const html = fs.readFileSync(index, "utf8");
const cdnHit = /https?:\/\/(cdn\.|unpkg\.|jsdelivr\.|googleapis\.|gstatic\.)/i.test(html);
if (cdnHit) {
  console.error("FAIL: external CDN reference in index.html");
  process.exit(1);
}
if (!html.includes("AI 店长") && !html.includes("/admin/assets/")) {
  // vite injects script tags under /admin/assets/
  console.error("FAIL: index.html does not look like built admin SPA");
  process.exit(1);
}
const assetsDir = path.join(dist, "assets");
if (!fs.existsSync(assetsDir) || fs.readdirSync(assetsDir).length === 0) {
  console.error("FAIL: empty assets dir");
  process.exit(1);
}
console.log("admin smoke ok", dist);
