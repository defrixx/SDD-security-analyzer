import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
globalThis.localStorage = { getItem: () => process.env.SDD_TEST_LANG || "en" };
const { inspectSkill } = await import("../spec_enricher/static/skill-import.js");
const text = '---\nname: security-review\ndescription: Review a specification.\n---\n# Review\n[guide](references/guide.md)\n[tool](scripts/check.py)\n```bash\npython scripts/check.py\n```\n<script>alert(1)</script>';
const parsed = inspectSkill(text);
const bomSource = "\uFEFF" + text;
assert.equal(inspectSkill(bomSource).text, bomSource);
assert.equal(inspectSkill(bomSource).name, "security-review");
assert.equal(parsed.text, text);
assert.equal(parsed.name, "security-review");
assert.equal(parsed.metadataRecognized, true);
assert.deepEqual(parsed.links, ["references/guide.md", "scripts/check.py"]);
assert.ok(parsed.commands.length >= 2);
assert.equal(inspectSkill("# hello").metadataRecognized, false);
assert.equal(inspectSkill("{{untrusted}}").variablesUnsupported, true);
assert.equal(inspectSkill("---\r\nname: 'review'\r\ndescription: test\r\n---\r\nText").name,"review");
assert.equal(inspectSkill("---\nname: !execute bad\ndescription: >\n---\ntext").metadataRecognized,false);
for (const bad of ["", "\0", "x".repeat(30001)]) assert.throws(() => inspectSkill(bad));
assert.throws(() => inspectSkill("text", "script.py"));
if (process.env.HUB_SKILLS_DIR) {
  let count = 0;
  for (const name of readdirSync(process.env.HUB_SKILLS_DIR)) {
    const original = readFileSync(`${process.env.HUB_SKILLS_DIR}/${name}/SKILL.md`, "utf8");
    const result = inspectSkill(original);
    assert.equal(result.name, name); assert.equal(result.text, original);
    assert.equal(result.metadataRecognized, true); assert.ok(result.links.length);
    count++;
  }
  assert.equal(count, 5);
  console.log("PASS: all five real Hub entries preserved; dependencies reported");
}
console.log(`PASS: safe skill text inspection, limits, metadata, dependency/command hints, template conflicts (${process.env.SDD_TEST_LANG || "en"})`);
