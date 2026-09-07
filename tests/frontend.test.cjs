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
  assert.equal(a.run("profitText({outcome_status:'TP_HIT',profit_pct:99,entry_price:100,tp1_price:110,supporting_evidence:{profit_pct:null}})"),'Belum tersedia');
  assert.equal(a.run('profitText(null)'),'Belum ada outcome');
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
