const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync('web/index.html','utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1].split('(async()=>{const {data:{session}}')[0];
function app(){
  const elements=new Map();
  const get=id=>{if(!elements.has(id))elements.set(id,{innerHTML:'',textContent:'',style:{},classList:{add(){},remove(){}},addEventListener(){},querySelectorAll(){return []}});return elements.get(id)};
  const context=vm.createContext({document:{getElementById:get,querySelectorAll:()=>[],addEventListener(){}},supabase:{createClient:()=>({})},Intl,Date,setTimeout,clearTimeout});
  vm.runInContext(script,context);
  return {run:code=>vm.runInContext(code,context),get};
}
test('canonical profit preserves zero, negative and numeric strings; no price or status fallback',()=>{
  const a=app();
  for(const [raw,expected] of [[0,0],[-3.2,-3.2],['12.4',12.4],[null,null],['',null],['broken',null],[true,null]]){
    assert.equal(a.run(`canonicalProfit({supporting_evidence:{profit_pct:${JSON.stringify(raw)}}}).value`),expected);
  }
  assert.equal(a.run("profitText({supporting_evidence:{profit_pct:0}})"),'+0.00%');
  assert.equal(a.run("profitText({outcome_status:'TP_HIT',profit_pct:99,entry_price:100,tp1_price:110,supporting_evidence:{profit_pct:null}})"),'-');
  assert.equal(a.run('profitText(null)'),'-');
});
test('global numeric sorting before pagination; null last both ways; filter and stable ties',()=>{
  const a=app();
  a.run(`state.snapshot=[{id:'a',ticker:'AAA',signal_type:'BUY'},{id:'b',ticker:'BBB',signal_type:'BUY'},{id:'c',ticker:'CCC',signal_type:'SELL'},{id:'d',ticker:'DDD',signal_type:'BUY'}];
  state.outcomes=new Map([['a',{supporting_evidence:{profit_pct:2}}],['b',{supporting_evidence:{profit_pct:'10'}}],['c',{supporting_evidence:{profit_pct:-5}}],['d',{supporting_evidence:{profit_pct:null}}]]);
  state.sort='profit_pct';state.pageSize=2;state.ascending=false;applyView();`);
  assert.equal(a.run("state.items.map(x=>x.id).join(',')"),'b,a');
  assert.match(a.get('rows').innerHTML,/\+10.00%/);
  a.run('state.page=2;applyView()');
  assert.equal(a.run("state.items.map(x=>x.id).join(',')"),'c,d');
  a.run('state.ascending=true;state.page=1;applyView()');
  assert.equal(a.run("state.items.map(x=>x.id).join(',')"),'c,a');
  a.run("state.search='BB';applyView()");assert.equal(a.run('state.total'),1);
  a.run("state.search='';state.type='SELL';applyView()");assert.equal(a.run('state.items[0].id'),'c');
  a.run("state.outcomes.set('b',state.outcomes.get('a'))");
  assert.equal(a.run("compareSignals({id:'a'},{id:'b'})"),-1);
});
test('missing outcome is presented and sorted as OPEN while empty profit is a dash',()=>{
  const a=app();
  a.run(`state.snapshot=[{id:'a',ticker:'AAA'},{id:'b',ticker:'BBB'}];
    state.outcomes=new Map([['b',{outcome_status:'TP_HIT',supporting_evidence:{profit_pct:null}}]]);
    state.sources=new Map();state.sort='outcome_status';state.ascending=true;state.pageSize=25;applyView();`);
  assert.equal(a.run("sortValue({id:'a'},'outcome_status')"),'OPEN');
  assert.match(a.get('rows').innerHTML,/data-label="Outcome"><span class="badge muted">OPEN<\/span>/);
  assert.match(a.get('rows').innerHTML,/data-label="Profit"[^>]*>-<\/td>/);
  assert.doesNotMatch(a.get('rows').innerHTML,/Belum ada outcome|Belum tersedia/);
});
test('outcome and source sorting uses hydrated displayed values and source order is normalized',()=>{
  const a=app();
  a.run(`state.outcomes=new Map([['a',{outcome_status:'TP_HIT'}],['b',{outcome_status:'SL_HIT'}]]);state.sources=new Map([['a',[{source_system:'ZETA'},{source_system:'TELEGRAM'},{source_system:'ZETA'}]],['b',[{source_system:'TELEGRAM'}]]]);state.ascending=true;state.sort='outcome_status'`);
  assert.ok(a.run("compareSignals({id:'a'},{id:'b'})")>0);
  a.run("state.ascending=false");assert.ok(a.run("compareSignals({id:'a'},{id:'b'})")<0);
  assert.equal(a.run("sourceText('a')"),'TELEGRAM + ZETA');
  a.run("state.sort='source';state.ascending=true");assert.ok(a.run("compareSignals({id:'a'},{id:'b'})")>0);
  a.run("state.ascending=false");assert.ok(a.run("compareSignals({id:'a'},{id:'b'})")<0);
  assert.ok(a.run("compareSignals({id:'missing'},{id:'a'})")>0);
});
test('range reader continues past server caps and rejects errors',async()=>{
  const a=app();
  assert.equal(await a.run(`readAll(()=>({range:(from,to)=>({data:Array.from({length:Math.min(137,1205-from)},(_,i)=>from+i)})})).then(rows=>rows.length)`),1205);
  await assert.rejects(a.run("readAll(()=>({range:()=>({error:new Error('denied')})}))"),/denied/);
});
test('batch hydration runs with bounded concurrency and preserves batch order',async()=>{
  const a=app();
  const result=await a.run(`(async()=>{let active=0,maxActive=0;
    const rows=await readBatches(Array.from({length:950},(_,i)=>i),batch=>({range:async from=>{
      active++;maxActive=Math.max(maxActive,active);await new Promise(resolve=>setTimeout(resolve,2));active--;
      return {data:from===0?[batch[0]]:[]};
    }}));return {rows,maxActive};})()`);
  assert.equal(result.maxActive,4);
  assert.equal(Array.from(result.rows).join(','),'0,100,200,300,400,500,600,700,800,900');
});
test('cached snapshot renders immediately and refresh starts in background for the same user',async()=>{
  const a=app();
  a.run(`state.user={id:'user-a'};globalThis.indexedDB={};let refreshed=false,cacheUser='';
    readCachedSnapshot=async userId=>{cacheUser=userId;return {savedAt:1,items:[{id:'cached',ticker:'CACHE'}],outcomes:[],sources:[]}};
    refreshSnapshot=async background=>{refreshed=background};`);
  await a.run('loadPage()');
  assert.equal(a.run('state.items[0].id'),'cached');
  assert.equal(a.run('cacheUser'),'user-a');
  assert.equal(a.run('refreshed'),true);
  assert.match(a.get('filterMeta').textContent,/Cache tampil/);
});
test('cached maps are reconstructed and logout cache deletion is user scoped',async()=>{
  const a=app();
  a.run(`publishSnapshot([{id:'s',ticker:'AAA'}],[['s',{outcome_status:'TP_HIT',supporting_evidence:{profit_pct:5}}]],[['s',[{source_system:'ZETA'}]]],1);
    state.user={id:'user-a'};let deleted='';deleteCachedSnapshot=async id=>{deleted=id};sb.auth={signOut:async()=>{}};`);
  assert.equal(a.run("state.outcomes.get('s').outcome_status"),'TP_HIT');
  assert.equal(a.run("state.sources.get('s')[0].source_system"),'ZETA');
  await a.run("document.getElementById('logoutBtn').onclick()");
  assert.equal(a.run('deleted'),'user-a');
});
test('hydration spans link caps, joins source IDs and excludes superseded outcomes',async()=>{
  const a=app();
  a.run(`const requests=[];sb.from=table=>{let ids=[],active=false;return {select(){return this},in(key,values){ids=values;return this},is(key,value){active=key==='superseded_at'&&value===null;return this},order(){return this},range(from,to){requests.push(table);let data=table==='signal_outcomes'?(active?[{id:'o',signal_id:'s',outcome_status:'TP_HIT',supporting_evidence:{profit_pct:0}}]:[]):table==='signal_source_links'?Array.from({length:603},(_,i)=>({signal_id:'s',source_record_id:String(i)})):ids.map(id=>({id,source_system:Number(id)%2?'ZETA':'TELEGRAM'}));return {data:data.slice(from,Math.min(to+1,from+71))}}}}`);
  assert.equal(await a.run("hydratePage(['s']).then(x=>x.sources.get('s').length)"),603);
  assert.equal(await a.run("hydratePage(['s']).then(x=>canonicalProfit(x.outcomes.get('s')).value)"),0);
});
test('cached interactions do not fetch and logout discards snapshot',async()=>{
  const a=app();
  a.run("state.user={id:'u'};state.snapshot=[{id:'a',ticker:'AAA'}];sb.from=()=>{throw new Error('unexpected network')}");
  await a.run('loadPage()');assert.equal(a.run('state.total'),1);
  a.run('showAuth()');assert.equal(a.run('state.snapshot'),null);assert.equal(a.get('rows').innerHTML,'');
});
test('failed hydration is not published as an empty successful snapshot',async()=>{
  const a=app();
  a.run("state.user={id:'u'};readAll=async()=>[{id:'s'}];hydratePage=async()=>{throw new Error('hydration failed')}");
  await a.run('loadPage()');assert.equal(a.run('state.snapshot'),null);assert.match(a.get('tableState').textContent,/hydration failed/);
});
test('filters changed during loading are applied when full snapshot arrives',async()=>{
  const a=app();
  a.run("state.user={id:'u'};let finish;readAll=()=>new Promise(resolve=>finish=resolve);hydratePage=async()=>({outcomes:new Map(),sources:new Map()});const pending=loadPage();state.search='BBB';finish([{id:'a',ticker:'AAA'},{id:'b',ticker:'BBB'}])");
  await a.run('pending');assert.equal(a.run('state.items[0].id'),'b');
});
test('detail uses the same canonical profit formatter as the table',async()=>{
  const a=app();
  a.run(`sb.from=table=>{const response={data:table==='signals'?{id:'s',ticker:'AAA'}:table==='signal_outcomes'?{outcome_status:'TP_HIT',supporting_evidence:{profit_pct:0}}:[]};return {select(){return this},eq(){return this},is(){return this},single(){return response},maybeSingle(){return response},order(){return response},then(resolve){resolve(response)}}}`);
  await a.run("openDetail('s')");
  assert.match(a.get('modalBody').innerHTML,/>Profit<\/div><div class="v">\+0.00%/);
});
test('duplicate active outcomes fail hydration rather than picking an arbitrary row',async()=>{
  const a=app();
  a.run("readBatches=async(ids,makeQuery)=>[{signal_id:'s'},{signal_id:'s'}]");
  await assert.rejects(a.run("hydratePage(['s'])"),/Lebih dari satu outcome aktif/);
});
test('logout invalidates in-flight snapshot; refresh replaces the cached data',async()=>{
  const a=app();
  a.run("state.user={id:'u'};let finish;readAll=()=>new Promise(resolve=>finish=resolve);hydratePage=async()=>({outcomes:new Map(),sources:new Map()});const pending=loadPage();showAuth();finish([{id:'old',ticker:'OLD'}])");
  await a.run('pending');assert.equal(a.run('state.snapshot'),null);
  a.run("state.user={id:'u'};state.snapshot=[{id:'old'}];readAll=async()=>[{id:'new',ticker:'NEW'}]");
  await a.run('loadPage(true)');assert.equal(a.run('state.items[0].id'),'new');
});
test('audited legacy outcomes read the unique same-source Zeta assertion profit',()=>{
  const a=app();
  for(const [ticker,profit,status] of [['GULA',-5.625,'SL_HIT'],['IRSX',11.5,'TP_HIT'],['KRAS',5,'TP_HIT'],['CDIA',5.0359712230215825,'TP_HIT'],['PPRE',5.607476635514018,'TP_HIT']]){
    a.run(`globalThis.o={signal_id:'${ticker}',authority:'ZETA',outcome_status:'${status}',resolved_at:'2026-09-03T03:00:00Z',supporting_evidence:{source_record_id:'source'}};globalThis.assertion={id:'assertion',signal_id:'${ticker}',source_record_id:'source',outcome_status:'${status}',source_resolved_at:'2026-09-03T03:00:00+00:00',profit_pct:${profit}};attachLegacyProfit(o,[assertion])`);
    assert.equal(a.run('canonicalProfit(o).value'),profit);
    assert.equal(a.run('profitOrigin(o)'),'ZETA');
  }
});
test('legacy lookup rejects conflicting identities, statuses, timestamps and ambiguity',()=>{
  const a=app();
  const base="globalThis.o={signal_id:'s',authority:'ZETA',outcome_status:'TP_HIT',resolved_at:'2026-09-03T03:00:00Z',supporting_evidence:{source_record_id:'source'}};globalThis.assertion={id:'a',signal_id:'s',source_record_id:'source',outcome_status:'TP_HIT',source_resolved_at:'2026-09-03T03:00:00Z',profit_pct:5};";
  for(const [key,value] of [['signal_id','other'],['source_record_id','other'],['outcome_status','SL_HIT'],['source_resolved_at','2026-09-04T03:00:00Z']]){
    a.run(base+`assertion.${key}='${value}';attachLegacyProfit(o,[assertion])`);assert.equal(a.run('canonicalProfit(o).value'),null);
  }
  a.run(base+'attachLegacyProfit(o,[assertion,{...assertion,id:"b"}])');assert.equal(a.run('canonicalProfit(o).value'),null);
  a.run(base+'o.supporting_evidence.assertion_id="b";attachLegacyProfit(o,[assertion])');assert.equal(a.run('canonicalProfit(o).value'),null);
  a.run(base+'o.authority="TELEGRAM";attachLegacyProfit(o,[assertion])');assert.equal(a.run('canonicalProfit(o).value'),null);
});
test('explicit null, invalid and zero evidence are never overwritten by assertion fallback',()=>{
  const a=app();
  for(const raw of [null,0,'bad']){
    a.run(`globalThis.o={signal_id:'s',authority:'ZETA',outcome_status:'TP_HIT',supporting_evidence:{source_record_id:'source',profit_pct:${JSON.stringify(raw)}}};attachLegacyProfit(o,[{id:'a',signal_id:'s',source_record_id:'source',outcome_status:'TP_HIT',profit_pct:99}])`);
    assert.equal(a.run('o.legacyProfit'),undefined);
    assert.equal(a.run('canonicalProfit(o).value'),raw===0?0:null);
  }
});
test('list and detail both hydrate legacy assertion fields',async()=>{
  const a=app();
  a.run(`globalThis.calls=[];sb.from=table=>{calls.push(table);const data=table==='signals'?{id:'s',ticker:'GULA'}:table==='signal_outcomes'?{id:'o',signal_id:'s',authority:'ZETA',outcome_status:'SL_HIT',resolved_at:'2026-09-03T03:00:00Z',supporting_evidence:{source_record_id:'src'}}:table==='signal_outcome_assertions'?[{id:'a',signal_id:'s',source_record_id:'src',outcome_status:'SL_HIT',profit_pct:-5.625,source_resolved_at:'2026-09-03T03:00:00Z'}]:[];return {select(){return this},eq(){return this},in(){return this},is(){return this},order(){return this},single(){return {data}},maybeSingle(){return {data}},range(from){return {data:from?[]:(Array.isArray(data)?data:[data])}},then(resolve){resolve({data})}}}`);
  await a.run("openDetail('s')");assert.match(a.get('modalBody').innerHTML,/-5.63%/);
  assert.equal(await a.run("hydratePage(['s']).then(x=>canonicalProfit(x.outcomes.get('s')).value)"),-5.625);
});
