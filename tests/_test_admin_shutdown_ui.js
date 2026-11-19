'use strict';
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const src=fs.readFileSync(path.join(__dirname,'../dashboard.html'),'utf8');
const block=src.slice(src.indexOf('async function shutdownGateway(btn){'),src.indexOf('// 本地 web 工具开关',src.indexOf('async function shutdownGateway(btn){')));
const logBlock=src.slice(src.indexOf('function startLogPolling(){'),src.indexOf('async function fetchNewLogs('));
const watchBlock=src.slice(src.indexOf('async function adminWatchJob('),src.indexOf('async function adminRenderOverview('));
const overview=src.split(String.fromCharCode(10)).find(line=>line.includes("document.addEventListener('DOMContentLoaded'")&&line.includes('cpLoadOverview'));
let passed=0;
function check(label,value){assert.ok(value,label);passed++;console.log('[PASS] '+label);}
async function scenario(mode){
 const active=new Map(),messages=[];let id=0,overviewCalls=0,logCalls=0,jobCalls=0;
 const ctx={SHUTDOWN_STATE:null,PANEL_TIMERS:[],PANEL_READY:true,currentMainTab:'settings',RT_TIMER:99,
 MutationObserver:undefined,document:{hidden:false,addEventListener:(event,fn)=>fn()},
 setInterval:(fn)=>{const key=++id;active.set(key,fn);return key},clearInterval:key=>active.delete(key),clearTimeout:()=>{},
 confirm:()=>true,postJSON:async()=>{if(mode==='http')throw Error('shutdown is loopback-only');if(mode==='network')throw Error('Failed to fetch');if(mode==='malformed')return {};return {ok:true,stopping:true}},
 toast:(msg,kind)=>messages.push({msg,kind}),cpLoadOverview:()=>overviewCalls++,logAutoRefresh:true,logPollTimer:null,fetchNewLogs:()=>logCalls++,getJSON:async()=>{jobCalls++;return {job:{status:'completed'}}}};
 vm.createContext(ctx);vm.runInContext(overview+String.fromCharCode(10)+logBlock+watchBlock+block,ctx);
 ctx.PANEL_TIMERS=[ctx.setInterval(()=>{},5000)];ctx.startLogPolling();const before=active.size;
 await ctx.shutdownGateway({});
 if(mode==='ok'){
  check('success gates independent overview interval',ctx.PANEL_READY===false);
  ctx.currentMainTab='gateway';for(const fn of active.values())fn();
  ctx.currentMainTab='logs';for(const fn of active.values())fn();
  await ctx.adminWatchJob('fixture',{});
  check('success removes core and log timers',active.size===1&&ctx.PANEL_TIMERS.length===0);
  check('late overview/log/job callbacks make no requests',overviewCalls===0&&logCalls===0&&jobCalls===0);
  check('accepted is stopping not stopped',messages[0].kind==='ok'&&messages[0].msg.includes('正在关闭'));
 }else{
  check(mode+' failure preserves polling',active.size===before&&ctx.PANEL_READY===true&&ctx.PANEL_TIMERS.length===1);
  check(mode+' failure never claims success',messages.every(m=>m.kind==='bad'));
 }
}
(async()=>{for(const mode of ['ok','http','network','malformed'])await scenario(mode);console.log('SUMMARY: TOTAL '+passed+' checks, '+passed+' passed, 0 failed, 0 skipped');})().catch(e=>{console.error(e);process.exitCode=1});
