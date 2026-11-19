const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const source=fs.readFileSync(path.join(__dirname,'../dashboard.html'),'utf8');
const fn=source.slice(source.indexOf('function usageModelCell('),source.indexOf('function effortCell('));
const ctx={esc:s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')};vm.createContext(ctx);vm.runInContext(fn,ctx);
let pass=0;function check(name,value){assert.ok(value,name);pass++;console.log('[PASS] '+name);}
check('legacy model unchanged',ctx.usageModelCell({model:'OLD'})==='OLD');
check('same model not duplicated',ctx.usageModelCell({model:'A',requested_model:'A',resolved_model:'A'})==='A');
check('request to upstream fallback visible',ctx.usageModelCell({model:'CUSTOM',requested_model:'CUSTOM',resolved_model:'auto'})==='CUSTOM → auto');
check('unknown actual explicit',ctx.usageModelCell({model:'CUSTOM',resolved_model:null}).includes('→ <span'));
check('model names escaped',!ctx.usageModelCell({requested_model:'<script>',resolved_model:'<img>'}).includes('<script>'));
check('usage table wired',source.includes('usageModelCell(r)'));
console.log('SUMMARY: TOTAL '+pass+' checks, '+pass+' passed, 0 failed, 0 skipped');
