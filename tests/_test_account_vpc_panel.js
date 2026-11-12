/* 离线行为测试：执行 dashboard.html 发布函数；不启动网关，不访问任何端点。
 * node tests/_test_account_vpc_panel.js
 */
const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard.html'), 'utf8');
const blocks = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m=>m[1]);
let passed = 0;
function check(label, fn){ fn(); passed++; console.log('  [PASS] ' + label); }
function slice(start, end){
  const a = html.indexOf(start), b = html.indexOf(end, a);
  assert.ok(a >= 0 && b > a, start);
  return html.slice(a, b);
}
function harness(){
  const els = {}, posts = [], gets = [], notices = [], timers = new Map(), later = [];
  let tid = 0, loads = 0, answer = null;
  const el = id=>els[id] || (els[id]={id, value:'', checked:false, hidden:false, disabled:false, textContent:'', innerHTML:'', className:'', classList:{add(){},remove(){}}});
  let post = async url=>url.endsWith('/start') ? {state:'state-target', authUrl:'https://HOST/authorize', vpcEndpoint:'target'} : {imported:[{nickname:'TARGET',vpcEndpoint:'target'}]};
  let get = async()=>({status:'pending'});
  const ctx = vm.createContext({
    window:{VIEW_REALM:'cn',ACCOUNTS:[]},
    document:{getElementById:el,querySelector:()=>({value:el('radioIntl').checked?'intl':'cn'})},
    postJSON:async(url,body)=>{posts.push({url,body:JSON.parse(JSON.stringify(body))});return post(url,body);},
    getJSON:async url=>{gets.push(url);return get(url);},
    loadAccounts:async()=>{loads++;}, refresh:async()=>{}, toast:(...args)=>notices.push(args),
    prompt:()=>answer,
    USAGE_BY_ACCOUNT:{}, fmt:x=>String(x || 0), teamBadge:()=>'', checkinBadge:()=>'', accountGuardBadge:()=>'', creditsDeltaChip:()=>'',
    esc:value=>String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
    setInterval:fn=>{timers.set(++tid,fn);return tid;},clearInterval:id=>timers.delete(id),
    setTimeout:fn=>{later.push(fn);return later.length;},console
  });
  vm.runInContext('let loginState=null, loginTimer=null, loginGeneration=0;\n' +
    slice('function accountVpcValue(', 'window.ACTIVE_GATEWAY_REALM') +
    slice('function accountVpcRow(', '// issue #69：账号备注') +
    slice('function openPatImport(){', '/* ------------------------------- dashboard'), ctx);
  el('loginVpcMode').value='inherit';el('patVpcMode').value='inherit';
  return {ctx,el,posts,gets,notices,timers,later,setPost:fn=>post=fn,setGet:fn=>get=fn,setAnswer:v=>answer=v,loads:()=>loads,
    run:expression=>vm.runInContext(expression,ctx)};
}
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
async function main(){
  check('恰好三个脚本块，均可解析',()=>{assert.equal(blocks.length,3);blocks.forEach(b=>new vm.Script(b));});
  const h=harness(), a=h.ctx;
  check('OAuth 打开先显示配置，不自动申请链接',()=>{a.openLoginModal();assert.equal(h.posts.length,0);assert.equal(h.el('loginVpcFields').hidden,false);assert.match(h.el('loginStatus').textContent,/生成授权链接/);});
  check('继承为 null，与显式公网空串不同',()=>{assert.equal(a.accountVpcValue('login','cn'),null);h.el('loginVpcMode').value='public';assert.equal(a.accountVpcValue('login','cn'),'');});
  check('指定实例读取并冻结输入值',()=>{h.el('loginVpcMode').value='instance';h.el('loginVpcEndpoint').value=' TARGET ';assert.equal(a.accountVpcValue('login','cn'),'TARGET');});
  check('实例模式空输入报错而非降为公网',()=>{h.el('loginVpcEndpoint').value=' ';assert.throws(()=>a.accountVpcValue('login','cn'),/请填写/);});
  check('国际版隐藏且不读取无效 VPC 字段',()=>{a.accountVpcSync('login','intl');assert.equal(h.el('loginVpcFields').hidden,true);assert.equal(h.el('loginVpcEndpoint').disabled,true);assert.equal(a.accountVpcValue('login','intl'),undefined);});
  h.el('loginVpcEndpoint').value='target';
  await a.restartLogin();
  check('CN start 发送实例并显示服务端实际入口',()=>{assert.deepEqual(h.posts[0],{url:'/accounts/login/start',body:{platform:'CLI',realm:'cn',vpc:'target'}});assert.match(h.el('step1Text').textContent,/VPC · target/);assert.match(h.el('loginLinkBox').innerHTML,/HOST/);assert.equal(h.timers.size,1);});
  h.el('loginVpcEndpoint').value='other';a.accountVpcChanged('login');
  check('修改实例立即移除旧链接、停止轮询并取消旧状态',()=>{assert.equal(h.el('loginLinkBox').innerHTML,'');assert.equal(h.timers.size,0);assert.ok(h.posts.some(p=>p.url.endsWith('/cancel')&&p.body.state==='state-target'));assert.match(h.el('loginStatus').textContent,/重新生成/);});
  const late=deferred();h.setPost(url=>url.endsWith('/start')?late.promise:Promise.resolve({}));
  const pending=a.restartLogin();
  check('生成中请求体不随输入变更',()=>{assert.equal(h.posts.at(-1).body.vpc,'other');assert.equal(h.el('loginGenerate').disabled,true);});
  h.el('loginVpcEndpoint').value='new';a.accountVpcChanged('login');
  late.resolve({state:'stale',authUrl:'https://HOST/stale',vpcEndpoint:'other'});await pending;
  check('迟到 start 不挂旧链接且取消迟到状态',()=>{assert.equal(h.el('loginLinkBox').innerHTML,'');assert.ok(h.posts.some(p=>p.body.state==='stale'));assert.equal(h.run('loginState'),null);assert.equal(h.el('loginGenerate').disabled,false);});
  const race=harness(), d1=deferred(),d2=deferred();let n=0;
  race.setPost(url=>url.endsWith('/start')?(++n===1?d1.promise:d2.promise):Promise.resolve({}));
  const p1=race.ctx.restartLogin(),p2=race.ctx.restartLogin();
  d2.resolve({state:'new-state',authUrl:'https://HOST/new',vpcEndpoint:''});await p2;
  d1.resolve({state:'old-state',authUrl:'https://HOST/old',vpcEndpoint:'old'});await p1;
  check('并发重新生成：后发请求胜出，旧响应不覆盖',()=>{assert.match(race.el('loginLinkBox').innerHTML,/HOST\/new/);assert.doesNotMatch(race.el('loginLinkBox').innerHTML,/HOST\/old/);assert.equal(race.run('loginState'),'new-state');assert.equal(race.timers.size,1);});
  const pol=deferred();race.setGet(()=>pol.promise);const poll=race.ctx.pollLogin();race.ctx.closeLogin();
  pol.resolve({status:'ok',account:{nickname:'OLD'}});await poll;
  check('关闭后迟到 poll 不报告成功或刷新列表',()=>{assert.equal(race.loads(),0);assert.equal(race.el('loginLinkBox').innerHTML,'');assert.equal(race.timers.size,0);});
  const shut=harness(), slow=deferred();shut.setPost(url=>url.endsWith('/start')?slow.promise:Promise.resolve({}));const start=shut.ctx.restartLogin();shut.ctx.closeLogin();slow.resolve({state:'closed',authUrl:'https://HOST/closed',vpcEndpoint:'target'});await start;
  check('关闭后迟到 start 不重新开启授权',()=>{assert.equal(shut.el('loginLinkBox').innerHTML,'');assert.equal(shut.timers.size,0);assert.ok(shut.posts.some(p=>p.url.endsWith('/cancel')));});
  const intl=harness();intl.el('radioIntl').checked=true;intl.el('loginVpcMode').value='instance';await intl.ctx.restartLogin();
  check('国际版 start 不携带 vpc 字段',()=>assert.deepEqual(intl.posts[0].body,{platform:'CLI',realm:'intl'}));
  const pub=harness();pub.el('loginVpcMode').value='public';pub.setPost(async()=>({state:'public',authUrl:'https://HOST/public',vpcEndpoint:''}));await pub.ctx.restartLogin();
  check('显式公网保留空串，不误变 null，链接标明公网',()=>{assert.equal(pub.posts[0].body.vpc,'');assert.equal(pub.el('step1Text').textContent,'本次授权入口：公网');});
  const invalid=harness();invalid.el('loginVpcMode').value='instance';await invalid.ctx.restartLogin();
  check('空实例不发 start、显示错误并恢复按钮',()=>{assert.equal(invalid.posts.length,0);assert.match(invalid.el('loginStatus').textContent,/请填写/);assert.equal(invalid.el('loginGenerate').disabled,false);});
  const bad=harness();bad.setPost(async()=>{throw new Error('TARGET rejected');});await bad.ctx.restartLogin();
  check('start 失败不残留链接或定时器',()=>{assert.equal(bad.timers.size,0);assert.equal(bad.el('loginLinkBox').innerHTML,'');assert.match(bad.el('loginStatus').textContent,/TARGET rejected/);assert.equal(bad.el('loginGenerate').disabled,false);});
  for(const [mode,value,expected] of [['inherit','',null],['public','',''],['instance','target','target']]){
    const pat=harness();pat.ctx.openPatImport();pat.el('patInput').value='pt-TOKEN';pat.el('patVpcMode').value=mode;pat.el('patVpcEndpoint').value=value;const button=pat.el('submit');await pat.ctx.doImportPat(button);
    check('PAT CN '+mode+' 正确发送三态并恢复按钮',()=>{assert.deepEqual(pat.posts[0],{url:'/accounts/import-pat',body:{pat:'pt-TOKEN',realm:'cn',vpc:expected}});assert.equal(button.disabled,false);assert.equal(pat.loads(),1);});
  }
  const patIntl=harness();patIntl.ctx.window.VIEW_REALM='intl';patIntl.ctx.openPatImport();patIntl.el('patInput').value='pt-TOKEN';await patIntl.ctx.doImportPat(patIntl.el('submit'));
  check('PAT 国际版隐藏配置且不发送 vpc',()=>{assert.equal(patIntl.el('patVpcFields').hidden,true);assert.deepEqual(patIntl.posts[0].body,{pat:'pt-TOKEN',realm:'intl'});});
  const patBad=harness();patBad.el('patInput').value='pt-TOKEN';patBad.el('patVpcMode').value='instance';await patBad.ctx.doImportPat(patBad.el('submit'));
  check('PAT 空实例不发导入请求',()=>{assert.equal(patBad.posts.length,0);assert.match(patBad.el('patBody').innerHTML,/请填写/);assert.equal(patBad.el('submit').disabled,false);});
  for(const [source,label]of [['account','账号覆盖'],['domain','凭证域推断'],['default','环境默认'],['public','公网']]){
    check('账号行展示实际入口与来源 '+source,()=>{const row=a.accountVpcRow({realm:'cn',uid:'TARGET',vpcEndpoint:'target',vpcOverride:null,vpcSource:source});assert.match(row,/VPC · target/);assert.ok(row.includes(label));assert.match(row,/继承/);});
  }
  check('显式公网可见且国际版无 VPC 编辑入口',()=>{assert.match(a.accountVpcRow({realm:'cn',vpcEndpoint:'',vpcOverride:'',vpcSource:'account'}),/显式公网/);assert.equal(a.accountVpcRow({realm:'intl'}),'');});
  check('VPC 行转义服务端字段防止注入',()=>{const row=a.accountVpcRow({realm:'cn',uid:'"<TARGET>',vpcEndpoint:'<img>',vpcOverride:'target',vpcSource:'account'});assert.doesNotMatch(row,/<img>/);assert.match(row,/&lt;img&gt;/);assert.match(row,/&quot;&lt;TARGET&gt;/);});
  const edit=harness();edit.ctx.window.ACCOUNTS=[{uid:'TARGET',realm:'cn',vpcEndpoint:'old',vpcOverride:null}];
  for(const [answer,expected]of [['继承',null],['',''],[' target ','target']]){
    edit.setAnswer(answer);await edit.ctx.editAccountVpc('TARGET',edit.el('button'));
    check('逐行编辑保存 '+JSON.stringify(expected),()=>{assert.deepEqual(edit.posts.at(-1),{url:'/accounts/set',body:{uid:'TARGET',vpcEndpoint:expected}});assert.equal(edit.el('button').disabled,false);});
  }
  check('保存后重新加载服务端列表而不猜测生效入口',()=>{assert.equal(edit.loads(),3);assert.equal(edit.ctx.window.ACCOUNTS[0].vpcEndpoint,'old');});
  edit.setAnswer(null);const before=edit.posts.length;await edit.ctx.editAccountVpc('TARGET',edit.el('button'));
  check('取消编辑无请求',()=>assert.equal(edit.posts.length,before));
  edit.setAnswer('target');edit.setPost(async()=>{throw new Error('bad instance');});await edit.ctx.editAccountVpc('TARGET',edit.el('button'));
  check('保存失败不改本地状态，恢复按钮并显示失败',()=>{assert.equal(edit.loads(),3);assert.equal(edit.el('button').disabled,false);assert.match(edit.notices.at(-1)[0],/bad instance/);});
  check('实际 CN 账号行挂载入口和逐行编辑按钮',()=>{
    const row=a.accountRow({uid:'TARGET',nickname:'TARGET',realm:'cn',enabled:true,source:'oauth',vpcEndpoint:'target',vpcSource:'default',vpcOverride:null});
    assert.match(row,/account-vpc-row/);assert.match(row,/VPC · target/);assert.match(row,/editAccountVpc/);
  });
  const inherited=harness();await inherited.ctx.restartLogin();
  check('OAuth 继承请求显式发送 null',()=>assert.equal(inherited.posts[0].body.vpc,null));
  inherited.el('radioIntl').checked=true;inherited.ctx.switchLoginRealm('intl');
  check('版本切换立即清除链接且不自动重发 start',()=>{assert.equal(inherited.el('loginLinkBox').innerHTML,'');assert.equal(inherited.posts.filter(p=>p.url.endsWith('/start')).length,1);assert.equal(inherited.el('loginVpcFields').hidden,true);});
  const success=harness();await success.ctx.restartLogin();success.setGet(async()=>({status:'ok',account:{nickname:'TARGET'}}));await success.ctx.pollLogin();
  check('当前授权成功刷新列表、停止轮询并清空状态',()=>{assert.equal(success.loads(),1);assert.equal(success.timers.size,0);assert.equal(success.run('loginState'),null);assert.match(success.el('loginStatus').textContent,/登录成功/);});
  success.ctx.openLoginModal();success.later.forEach(fn=>fn());
  check('旧成功自动关闭不会关闭新一轮登录',()=>assert.match(success.el('loginStatus').textContent,/确认版本和入口/));
  console.log('SUMMARY: TOTAL '+passed+' checks, '+passed+' passed, 0 failed');
}
main().catch(e=>{console.error(e);process.exitCode=1;});
