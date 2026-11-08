/* 运行真实账号额度计算、渲染、排序与翻译，防止共享账号主数字再次显示个人 0/0。 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const {extract, fnBody} = require('./_tools/dash_extract.js');
const htmlPath = process.env.QD_SHARED_TEST_DASH || path.join(__dirname, '..', 'dashboard.html');
const {code, html} = extract(htmlPath);
console.log('SUITE: ' + path.basename(__filename) + ' md5=' + crypto.createHash('md5').update(fs.readFileSync(__filename)).digest('hex'));
console.log('FILE: dashboard.html md5=' + crypto.createHash('md5').update(html).digest('hex') + ' bytes=' + Buffer.byteLength(html) + ' as-of=' + new Date().toISOString());
let pass = 0, fail = 0;
function check(label, cond, detail){
  if(cond){pass++; console.log('  [PASS] ' + label);}
  else{fail++; console.log('  [FAIL] ' + label + ' ' + JSON.stringify(detail));}
}
const quotaBody = fnBody(code, 'accountQuota');
const mainBody = fnBody(code, 'accountCreditMain');
check('额度计算和展示函数都来自发布页面', !!quotaBody && !!mainBody);
if(quotaBody && mainBody){
  const fmt = x => x == null ? '—' : Number(x).toLocaleString('en-US');
  const api = new Function('fmt', quotaBody + '\n' + mainBody + '\nreturn {quota:accountQuota,render:accountCreditMain};')(fmt);
  const strip = h => h.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
  const main = c => strip((api.render(c).match(/<div class="account-quota-numbers">([\s\S]*?)<\/div>/) || [,''])[1]);
  const shared = {remain:0, size:0, used:0, shared:{remain:5746, size:6000, used:254, available:true}};
  const q = api.quota(shared);
  check('仅共享：主数字合并为 5746 / 6000', q.remaining === 5746 && q.limit === 6000 && q.used === 254, q);
  check('用户可见主数字不再显示个人 0/0', main(shared) === '5,746 / 6,000', main(shared));
  check('已用与共享来源直接可见', /已用\s*254/.test(strip(api.render(shared))) && strip(api.render(shared)).includes('共享额度'));
  const mixed = {remain:100, size:200, used:100, shared:shared.shared};
  const mixedQ = api.quota(mixed);
  check('个人加共享：剩余、总额和已用一致合计', mixedQ.remaining === 5846 && mixedQ.limit === 6200 && mixedQ.used === 354, mixedQ);
  check('混合额度显示来源分项', strip(api.render(mixed)).includes('个人 100 / 200') && strip(api.render(mixed)).includes('共享 5,746 / 6,000'));
  check('个人账号仍按原额度展示', main({remain:80,size:100,used:20}) === '80 / 100');
  const empty = {remain:0,size:0,used:0,shared:{remain:0,size:6000,used:6000,available:false}};
  check('耗尽共享池显示 0/6000 和真实已用', main(empty) === '0 / 6,000' && /已用\s*6,000/.test(strip(api.render(empty))));
  const unavailable = {...shared, shared:{...shared.shared, available:false}};
  check('不可用共享池不计可用余额，但保留限额和已用', api.quota(unavailable).remaining === 0 && api.quota(unavailable).limit === 6000 && strip(api.render(unavailable)).includes('共享不可用'));
  const unlimited = {remain:0,size:0,used:0,shared:{remain:1000000,size:-1,used:254,unlimited:true}};
  check('不限量不显示负上限或占位余额', main(unlimited) === '不限量' && !strip(api.render(unlimited)).includes('1,000,000'));
  check('没有快照不能编造 0 余额', api.quota(null) === null && api.quota({}) === null && strip(api.render(null)) === '未查询');
  const partial = api.quota({remain:25});
  check('缺少总额时保留未知而不编造', partial.remaining === 25 && partial.limit === null && partial.used === null, partial);
  check('零余额也不意味着总额已知', api.quota({remain:0}).limit === null);
  check('共享已用缺失时不能编造成 0', api.quota({remain:0,size:0,used:0,shared:{remain:50}}).used === null);
  const sort = new Function('USAGE_BY_ACCOUNT', quotaBody + '\n' + fnBody(code,'acctSortValue') + '\nreturn acctSortValue;')({});
  check('剩余与已用排序使用合并口径', sort({uid:'one',credits:shared},'credits') === 5746 && sort({uid:'one',credits:shared},'used') === 254);
  check('不限量按可用额度排序时不当作 0 或未知', sort({uid:'one',credits:unlimited},'credits') === Number.MAX_VALUE);
  const row = new Function('fmt','esc','accountCreditMain','USAGE_BY_ACCOUNT','teamBadge','checkinBadge','accountGuardBadge','creditsDeltaChip',fnBody(code,'accountRow')+'\nreturn accountRow;')(
    fmt, x=>String(x), api.render, {}, ()=>'', ()=>'', ()=>'', ()=>''
  );
  const rowHtml = row({uid:'demo-account',nickname:'示例账号',realm:'intl',source:'oauth',enabled:true,credits:shared});
  const cell = (rowHtml.match(/<td class="account-credit-cell"[^>]*>([\s\S]*?)<\/td>/) || [,''])[1];
  check('实际账号行使用紧凑额度单元格', cell.includes('account-quota-remaining') && !/0\s*\/\s*0/.test(strip(cell)), strip(cell));
  const caption = html.match(/<style id="account-quota-layout">([\s\S]*?)<\/style>/);
  check('布局 CSS 仅限额度组件，不覆盖全局主题', !!caption && !/:root|\[data-theme|\bbody\s*\{|\bhtml\s*\{/.test(caption[1]));
  const begin = code.indexOf('/* BEGIN-PURE-I18N-CORE'), end = code.indexOf('/* END-PURE-I18N-CORE */');
  if(begin < 0 || end <= begin) throw new Error('缺少真实翻译引擎锚点');
  const t = new Function('"use strict";'+code.slice(begin,end)+'\nreturn {t,load};')();
  t.load(JSON.parse(fs.readFileSync(path.join(__dirname,'..','i18n/en.json'),'utf8')));
  for(const c of [shared,mixed,empty,unlimited,unavailable]){
    const texts = [...api.render(c).matchAll(/>([^<>]+)</g)].map(m=>m[1].trim()).filter(Boolean);
    const english = texts.map(v=>t.t(v)).join(' ');
    check('实际额度文本节点都可译为英文', !/[\u4e00-\u9fff]/.test(english), english);
  }
}
console.log('\nSUMMARY: TOTAL '+(pass+fail)+' checks, '+pass+' passed, '+fail+' failed, 0 skipped');
console.log('RESULT: '+(fail?'RED':'GREEN'));
process.exit(fail?1:0);
