'use strict';
const fs=require('node:fs');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const test=require('node:test');
const src=fs.readFileSync(__dirname+'/admin_static/app.js','utf8');
const html=fs.readFileSync(__dirname+'/admin_static/index.html','utf8');

function page(){
  const nodes=new Map(),requests=[];
  const element=(tag,cls,message)=>({tag,children:[],textContent:String(message??''),value:'',
    append(...children){this.children.push(...children);},
    addEventListener(kind,cb){this['on'+kind]=cb;},
    replaceChildren(){this.children=[];this.textContent='';},
    content(){return [this.textContent,...this.children.map(n=>n.content())].join(' ');}});
  const $=id=>{
    assert.ok(html.includes('id="'+id+'"'),'shipped markup missing '+id);
    if(!nodes.has(id)) nodes.set(id,element('div'));
    return nodes.get(id);
  };
  $('activity-range').value='1';
  const state={page:'overview',authenticated:false};
  const ctx=vm.createContext({$,state,pages:{activity:['Medienzugriffe',''],overview:['Übersicht','']},
    history:{replaceState(){}},location:{hash:'#activity'},window:{scrollTo(){},addEventListener(){}},
    document:{querySelectorAll(){return[];}},element,text:(id,value)=>{$(id).textContent=value;},clear:n=>n.replaceChildren(),
    formatDate:String,bytes:v=>v+' B',toast(){},refresh:async()=>{},session:async()=>{state.authenticated=true;return true;},
    api:(url)=>new Promise((resolve,reject)=>requests.push({url,resolve,reject}))});
  const area=src.slice(src.indexOf('let activityGeneration'),src.indexOf('function eventSeverity'));
  vm.runInContext(area,ctx);
  vm.runInContext(src.slice(src.indexOf('function navigate('),src.indexOf('function badge(')),ctx);
  return {ctx,$,state,requests};
}
const empty={totals:{},first_cycle:null,last_cycle:null,next:null,details:[],watcher:[]};
const tick=()=>new Promise(resolve=>setImmediate(resolve));

test('direct reload of #activity fetches private data only after session, empty period shows gaps',async()=>{
  const h=page();
  assert.ok(html.includes('data-page="activity"'));
  const boot=vm.runInContext(src.slice(src.lastIndexOf('(async()=>{navigate(')),h.ctx);
  assert.equal(h.requests.length,0);
  await tick();
  assert.deepEqual(h.requests.map(r=>r.url),['/api/media-activity/1']);
  h.requests[0].resolve(empty);await boot;
  assert.match(h.$('activity-coverage').content(),/kein protokollierter/);
  assert.deepEqual(h.$('activity-totals').children.filter(n=>n.tag==='dd').map(n=>n.textContent),Array(6).fill('—'));
  assert.match(h.$('activity-watcher').content(),/keine Messung/);
  assert.match(h.$('activity-kernel-watcher').content(),/keine Messung des Linux-Kernels/);
});

test('changing period invalidates the day request; paging, navigation and refresh keep scopes separate',async()=>{
  const h=page();h.state.authenticated=true;
  h.ctx.navigate('activity');
  h.$('activity-range').value='7';
  const week=h.$('activity-range').onchange();
  assert.deepEqual(h.requests.map(r=>r.url),['/api/media-activity/1','/api/media-activity/7']);
  h.requests[1].resolve({...empty,first_cycle:600,last_cycle:660,totals:{cycles:2},next:[600,99],
    details:[{kind:'ebook',reason:'index',title:'<neuer Titel>',at:650}],watcher:[{at:600,reads:0,writes:0}],kernel_watcher:[{at:600,reads:17,writes:9}]});
  await week;
  h.requests[0].resolve({...empty,details:[{title:'Veraltet'}]});await tick();
  assert.match(h.$('activity-items').content(),/<neuer Titel>/);
  assert.doesNotMatch(h.$('activity-items').content(),/Veraltet/);
  assert.match(h.$('activity-watcher').content(),/0 Minuten mit Festplattenaktivität/);
  assert.match(h.$('activity-kernel-watcher').content(),/1 Minute mit Festplattenaktivität.*17 Lesevorgänge/);
  assert.doesNotMatch(h.$('activity-watcher').content(),/17 Lesevorgänge/);
  const next=h.$('activity-more').onclick();
  assert.equal(h.requests[2].url,'/api/media-activity/7/600-99');
  h.requests[2].resolve({...empty,details:[{kind:'series',reason:'series_target',title:'Älter',at:599}]});
  await next;
  assert.match(h.$('activity-items').content(),/neuer Titel.*Älter/);
  h.ctx.navigate('overview');h.ctx.navigate('activity');
  assert.equal(h.requests[3].url,'/api/media-activity/7');
  assert.doesNotMatch(h.$('activity-items').content(),/Älter/);
  h.state.authenticated=false;h.ctx.resetActivity();
  h.requests[3].resolve({...empty,details:[{title:'Privat'}]});await tick();
  assert.equal(h.$('activity-items').content(),'');
});
