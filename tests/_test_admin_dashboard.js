/* Local admin contract and executable helper regressions, no browser dependencies. */
const fs=require('fs'),path=require('path'),vm=require('vm');
const root=path.join(__dirname,'..'),html=fs.readFileSync(path.join(root,'dashboard.html'),'utf8');
let passed=0,failed=0;
function check(name,condition){if(condition){passed++;console.log('  [PASS] '+name)}else{failed++;console.log('  [FAIL] '+name)}}
console.log('SUITE: '+path.basename(__filename)+' md5='+require('crypto').createHash('md5').update(fs.readFileSync(__filename)).digest('hex'));
for(const [i,s]of [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].entries()){
 try{new vm.Script(s[1]);check('inline script '+i+' compiles',true)}catch(e){check('inline script '+i+' compiles',false)}
}
const source=html.slice(html.indexOf('function persistPanelToken'),html.indexOf('function openLogin(){'));
const local=new Map(),session=new Map(),elements={adminApiExamples:{innerHTML:''}};
const storage=m=>({getItem:k=>m.get(k)||null,setItem:(k,v)=>m.set(k,v),removeItem:k=>m.delete(k)});
const sandbox={localStorage:storage(local),sessionStorage:storage(session),PANEL_STORE:'test-token',
  window:{ACCOUNTS:[{uid:'a',group:'red',nickname:'Account A'},{uid:'b',group:'blue'},{uid:'c',group:''}]},
  API_KEY_ROWS:[{account_ids:[],account_groups:[]}],document:{addEventListener(){},querySelectorAll(){return[]},getElementById:id=>elements[id]},
  location:{origin:'http://127.0.0.1:8790'},esc:s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
  console,Set,toast(){}};
vm.createContext(sandbox);vm.runInContext(source,sandbox);
sandbox.persistPanelToken('opaque',true);
check('remember saves token only in local storage',local.get('test-token')==='opaque'&&!session.has('test-token'));
sandbox.persistPanelToken('ephemeral',false);
check('session login removes remembered token',!local.has('test-token')&&session.get('test-token')==='ephemeral');
sandbox.adminKeySelectGroup(0,'red',true);
check('group selection adds matching accounts',sandbox.API_KEY_ROWS[0].account_ids.join()==='a');
sandbox.adminKeySelectGroup(0,'red',false);
check('group removal removes only group members',sandbox.API_KEY_ROWS[0].account_ids.length===0);
sandbox.adminKeySelectGroup(0,'',true);
check('ungrouped selector preserves empty group semantics',sandbox.API_KEY_ROWS[0].account_ids.join()==='c');
vm.runInContext("ADMIN_ROUTING_RULES=[{groups:[],accounts:[]}];adminRuleScope(0,'groups',{value:'',checked:true})",sandbox);
check('routing checkbox retains ungrouped scope',vm.runInContext("ADMIN_ROUTING_RULES[0].groups.length===1&&ADMIN_ROUTING_RULES[0].groups[0]===''",sandbox));
sandbox.adminRenderApiExamples();
check('examples include all supported protocols',['/v1/chat/completions','/v1/responses','/v1/messages','YOUR_API_KEY'].every(s=>elements.adminApiExamples.innerHTML.includes(s)));
check('curl JSON is not double-encoded',!elements.adminApiExamples.innerHTML.includes('\\&quot;model'));
const gw=html.slice(html.indexOf('<div id="pageGateway"'),html.indexOf('<div id="pageModels"'));
check('overview retains exit and scheduler but not model table',gw.includes('id="cardGatewayCtrl"')&&gw.includes('id="schedulerBar"')&&!gw.includes('id="modelsTable"'));
check('standalone model and routing navigation',['btnNavModels','pageModels','sectionModels','btnNavRouting','pageRouting'].every(s=>html.includes('id="'+s+'"')));
check('group import supported',html.includes('id="importGroup"')&&html.includes('group: (document.getElementById(\'importGroup\')'));
check('logout clears persistent and session tokens',html.includes('localStorage.removeItem(PANEL_STORE)')&&html.includes('sessionStorage.removeItem(PANEL_STORE)'));
check('motion honors reduced preference',html.includes('prefers-reduced-motion'));
console.log('\nSUMMARY: TOTAL '+(passed+failed)+' checks, '+passed+' passed, '+failed+' failed');
process.exitCode=failed?1:0;
