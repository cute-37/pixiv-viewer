// 开发者工具：看一份“界面上出现过的文字”清单里，还有哪些没有英文。
// 清单这样得到（会把界面测试跑一遍，把出现过的中文都记下来）：
//   set PV_I18N_COLLECT=seen.jsonl && python -m pytest tests/ui
// 然后： node scripts/check_i18n.mjs seen.jsonl
import fs from "node:fs";
import { translateWith } from "../webui/js/i18n.js";
import EN from "../webui/js/i18n_en.js";

const lines = fs.readFileSync(process.argv[2], "utf-8").split("\n").filter(Boolean).map((l) => JSON.parse(l));
const seen = [...new Set(lines)].sort();
// 数字模板里的 {0} 换回一个数字再查（清单里记的是模板）
const fill = (s) => s.replace(/\{(\d+)\}/g, (m, i) => String(+i + 2));
const miss = seen.filter((s) => translateWith(EN, fill(s)) === null);
console.log(`${seen.length} 条文字，${miss.length} 条没有英文`);
for (const m of miss) console.log(JSON.stringify(m));
