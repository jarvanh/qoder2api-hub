"use strict";
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
const html = fs.readFileSync(require("node:path").join(__dirname, "../dashboard.html"), "utf8");
const source = html.slice(html.indexOf("let SYSTEM_PROMPT_READ_ID"), html.indexOf("// —— 面板关闭网关 ——"));
const form = { disabled: true }, box = { checked: false }, reads = [], writes = [], toasts = [];
const ctx = { document: {getElementById: id => ({ setSystemPromptForm: form, setOmitDefaultSystemPrompt: box }[id] || null)},
  window:{}, getJSON: () => new Promise(r=>reads.push(r)), postJSON: () => new Promise(r=>writes.push(r)),
  toast: (...x) => toasts.push(x), t: x=>x };
for(const key of ["renderWebToolsSettings","renderKeyRows","updateUI","adminRenderApiExamples","renderLimits","renderShutdownState"]) ctx[key] = ()=>{};
vm.createContext(ctx); vm.runInContext(source, ctx);
(async()=>{
  const init = ctx.loadSettings(false); reads[0]({omit_default_system_prompt:false}); await init;
  assert.equal(form.disabled,false);
  const stale=ctx.loadSettings(false); box.checked=true; const save=ctx.saveSystemPromptSettings();
  assert.equal(form.disabled,true);
  const during=ctx.loadSettings(false); reads[2]({omit_default_system_prompt:false}); await during;
  assert.equal(form.disabled,true);
  writes[0]({omit_default_system_prompt:true}); await save;
  reads[1]({omit_default_system_prompt:false}); await stale;
  assert.equal(box.checked,true); assert.equal(toasts.at(-1)[1],"ok");
  box.checked=false; const failed=ctx.saveSystemPromptSettings(); writes[1]({}); await failed;
  assert.equal(box.checked,true); assert.equal(toasts.at(-1)[1],"bad");
  const fresh=ctx.loadSettings(false); reads[3]({omit_default_system_prompt:false}); await fresh;
  assert.equal(box.checked,false);
  console.log("SUMMARY: TOTAL 8 checks, 8 passed, 0 failed, 0 skipped");
})().catch(err=>{console.error(err);process.exitCode=1;});
