/* Backend/provider form: save payloads, masked keys, drafts and failure recovery. */
const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname,'..','dashboard.html'),'utf8');
const source = html.slice(html.indexOf('const WEB_EXTERNAL_FIELDS ='), html.indexOf('async function savePanelPassword('));
let pass=0, fail=0;
function check(name, ok){ if(ok){pass++;console.log('  PASS '+name);}else{fail++;console.log('  FAIL '+name);} }
const elements={};
const doc={getElementById(id){return elements[id] || (elements[id]={value:'',checked:false,disabled:false,textContent:'',placeholder:'',style:{}});}};
const messages=[], requests=[];
let rejectSave=false;
const api=new Function('document','postJSON','toast',source+';return {render:renderWebToolsSettings,change:changeWebExternalProvider,read:readWebExternalProfile,save:saveWebTools,update:updateWebToolsBackend};')(
  doc, async(url,payload)=>{requests.push({url,payload});if(rejectSave)throw new Error('save rejected');return data;},
  (message,type)=>messages.push({message,type}));
const base={method:'GET',endpoint:'http://search:8080/search',query_field:'q',limit_field:'',params:{format:'json'},
  results_path:'results',title_field:'title',url_field:'url',snippet_field:'content',auth_header:'Authorization',auth_prefix:'Bearer'};
const profiles={searxng:{...base,provider:'searxng',api_key_set:false},
  tavily:{...base,provider:'tavily',endpoint:'https://api.tavily.com/search',method:'POST',query_field:'query',api_key_set:true}};
const data={web_tools:true,web_tools_backend:'external',web_tools_external:{provider:'searxng'},web_tools_external_profiles:profiles};
(async()=>{
  check('form contains all three backends', ['local','qoder','external'].every(x=>html.includes('option value="'+x+'"')));
  check('external provider presets and custom option exist', ['searxng','tavily','brave','serper','custom'].every(x=>html.includes('option value="'+x+'"')));
  api.render(data);
  check('effective configuration rendered',elements.setWebTools.checked && elements.setWebToolsBackend.value==='external' && elements.setWebExternal.style.display==='');
  check('external endpoint and parameters rendered',elements.setWebExternalEndpoint.value===base.endpoint && JSON.parse(elements.setWebExternalParams.value).format==='json');
  elements.setWebExternalEndpoint.value='http://draft:8080/search';
  api.change('tavily');
  check('provider switch loads its own preset',elements.setWebExternalMethod.value==='POST' && elements.setWebExternalQueryField.value==='query');
  check('saved API key is masked, not filled',elements.setWebExternalKey.value==='' && elements.setWebExternalKey.placeholder.includes('留空保留'));
  elements.setWebExternalKey.value='new-tavily-key';
  api.change('searxng');
  check('provider endpoint draft survives switching',elements.setWebExternalEndpoint.value==='http://draft:8080/search');
  check('another provider never receives draft API key',!Object.hasOwn(api.read(),'api_key'));
  api.change('tavily');
  check('unsaved key draft retained only for correct provider',api.read().api_key==='new-tavily-key');
  elements.setWebExternalKey.value='';
  await api.save();
  check('save endpoint and backend submitted',requests.at(-1).url==='/settings/web-tools' && requests.at(-1).payload.backend==='external');
  check('blank masked key omitted from payload',!Object.hasOwn(requests.at(-1).payload.external,'api_key'));
  check('form re-enabled after save',elements.setWebToolsForm.disabled===false);
  api.change('tavily');elements.setWebExternalClearKey.checked=true;
  await api.save();
  check('explicit key clear submits empty string',requests.at(-1).payload.external.api_key==='');
  api.render(data);elements.setWebExternalParams.value='not JSON';
  const before=requests.length;await api.save();
  check('invalid JSON does not send a save request',requests.length===before && messages.at(-1).type==='bad');
  api.render(data);rejectSave=true;await api.save();rejectSave=false;
  check('failed save displays error and restores form',messages.at(-1).message.includes('save rejected') && !elements.setWebToolsForm.disabled);
  api.render({...data,web_tools_backend:'qoder',web_tools_backend_env:'qoder',web_tools_enabled_env:'on'});
  check('environment overrides are visible and locked',elements.setWebToolsBackend.disabled && elements.setWebTools.disabled && elements.setWebToolsEnv.textContent.includes('环境变量'));
  check('non-external backend hides service fields',elements.setWebExternal.style.display==='none');
  await api.save();
  check('non-external save omits unrelated service drafts',!Object.hasOwn(requests.at(-1).payload,'external'));
  check('locked search and enable fields are omitted on save',
    !Object.hasOwn(requests.at(-1).payload,'backend') && !Object.hasOwn(requests.at(-1).payload,'enabled'));
  api.render({...data,web_tools_backend:'local',web_fetch_backend:'external',web_fetch_external:{
    endpoint:'http://fetcher:8080/fetch', method:'POST', url_field:'url', text_path:'data.markdown', api_key_set:true
  }});
  check('custom fetch UI visible independently of search',elements.setWebFetchExternal.style.display==='' && elements.setWebExternal.style.display==='none');
  check('fetch credential is masked separately',elements.setWebFetchExternalKey.value==='' && elements.setWebFetchExternalKey.placeholder.includes('留空保留'));
  await api.save();
  check('fetch save contains mapping and omits the existing key',requests.at(-1).payload.fetch_backend==='external' &&
    requests.at(-1).payload.external_fetch.text_path==='data.markdown' && !Object.hasOwn(requests.at(-1).payload.external_fetch,'api_key'));
  elements.setWebFetchBackend.value='external';elements.setWebFetchExternalClearKey.checked=true;
  await api.save();
  check('fetch clear submits an explicit empty key',requests.at(-1).payload.external_fetch.api_key==='');
  console.log('\nSUMMARY: TOTAL '+(pass+fail)+' checks, '+pass+' passed, '+fail+' failed');
  process.exitCode=fail?1:0;
})().catch(e=>{console.error(e);process.exitCode=1;});
