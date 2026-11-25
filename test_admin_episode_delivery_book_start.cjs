'use strict';
const assert=require('node:assert/strict');
const test=require('node:test');
const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync(__dirname+'/admin_static/index.html','utf8');
const app=fs.readFileSync(__dirname+'/admin_static/app.js','utf8');

function fixture() {
  const elements=new Map(), calls=[], requests=[];
  const state={authenticated:true,busy:false};
  function element(tag,cls,value) {
    return {tag,children:[],textContent:String(value??''),value:'',disabled:false,open:false,
      addEventListener(event,handler){this['on'+event]=handler;},
      replaceChildren(){this.children=[];this.textContent='';},
      append(...nodes){this.children.push(...nodes);},
      showModal(){this.open=true;},
      close(){this.open=false;this.onclose?.();},
      content(){return [this.textContent,...this.children.map(child=>child.content())].join(' ');}
    };
  }
  const $=id=>{
    assert.ok(html.includes('id="'+id+'"'),'missing '+id);
    if (!elements.has(id)) elements.set(id,element('div'));
    return elements.get(id);
  };
  const ctx=vm.createContext({state,$,element,clear:node=>node.replaceChildren(),
    button:(label,cls,handler)=>Object.assign(element('button',cls,label),{onclick:handler}),
    text:(id,value)=>{$(id).textContent=String(value);},formatDate:String,toast(){},
    runCommand:(kind,payload)=>new Promise((resolve,reject)=>calls.push({kind,payload:JSON.parse(JSON.stringify(payload)),resolve,reject})),
    api:path=>new Promise((resolve,reject)=>requests.push({path,resolve,reject}))});
  for (const [from,to] of [
    ['function bookPosition(','function renderLibrary('],['function durationLabel(','function episodeReviewButton('],
    ['let bookStart = null','async function previewMissing('],['let deliverySearch=null','const episodePeerNames=']
  ]) vm.runInContext(app.slice(app.indexOf(from),app.indexOf(to)),ctx);
  return {$,ctx,calls,requests,state};
}
const ebook={title:'EPUB <1>',type:'ebook',id:'ebook:14'};
const review={review_id:'review',digest:'digest',states:[
  {source:'bo',unit:'percent',status:'in_progress',position:33.3,selectable:true},
  {source:'jf',unit:'percent',status:'not_started',position:0,selectable:false}
]};
const choose=(node)=>{node.checked=true;node.onchange();};

test('book detail requires selecting a supported reviewed source and sends its digest once',async()=>{
  const h=fixture();
  const preview=h.ctx.bookStartButton(ebook).onclick();
  assert.equal(h.$('book-start-dialog').open,true);
  assert.deepEqual(h.calls[0].payload,{media:'ebook',id:'ebook:14'});
  assert.equal(h.calls[0].kind,'bookstart.preview');
  h.calls[0].resolve(review);await preview;
  const [bo,jf]=h.$('book-start-states').children;
  assert.match(bo.content(),/BookOrbit.*33,3 %/);
  assert.equal(jf.children[0].disabled,true);
  await h.$('book-start-apply').onclick(); assert.equal(h.calls.length,1);
  choose(bo.children[0]);
  assert.match(h.$('book-start-impact').content(),/Jellyfin/);
  const apply=h.$('book-start-apply').onclick();
  await h.$('book-start-apply').onclick();
  assert.equal(h.calls.length,2);
  assert.equal(h.calls[1].kind,'bookstart.apply');
  assert.deepEqual(h.calls[1].payload,{review_id:'review',digest:'digest',source:'bo',confirm:true});
  h.calls[1].resolve({applied:true});await apply;
  assert.equal(h.$('book-start-dialog').open,false);
});
test('changed audiobook states require a fresh preview, and closed previews cannot leak into another dialog',async()=>{
  const h=fixture();
  const audio={id:'a',type:'audiobook',title:'Audio'};
  const preview=h.ctx.previewBookStart(audio);
  const states=[{source:'abs',unit:'seconds',status:'in_progress',position:78,selectable:true}];
  h.calls[0].resolve({...review,states});await preview;
  const label=h.$('book-start-states').children[0];
  assert.match(label.content(),/Audiobookshelf.*0:01:18/);
  choose(label.children[0]);
  const apply=h.$('book-start-apply').onclick();
  h.calls[1].reject(new Error('Stände haben sich geändert'));await apply;
  assert.equal(h.$('book-start-retry').hidden,false);
  await h.$('book-start-apply').onclick();assert.equal(h.calls.length,2);
  const retry=h.$('book-start-retry').onclick();
  assert.equal(h.calls[2].kind,'bookstart.preview');
  h.$('book-start-dialog').close();
  h.calls[2].resolve(review);await retry;
  assert.equal(h.$('book-start-states').children.length,0);
});
test('native search includes episodes outside ABS, encodes queries and pages, and separates cursor receipts from target import',async()=>{
  const h=fixture();
  h.$('delivery-query').value='Ä & B';
  const first=h.ctx.findDeliveries();
  assert.equal(h.requests[0].path,'/api/podcast-delivery/search?q=%C3%84%20%26%20B&offset=0');
  const row={episode_id:'2:3:4',show:'Show',title:'Folge <1>',published_at:1640000000};
  h.requests[0].resolve({state:'ok',rows:[row],has_more:true});await first;
  const second=h.$('delivery-more').onclick();
  assert.match(h.requests[1].path,/offset=1$/);
  h.requests[1].resolve({state:'ok',rows:[{...row,episode_id:'2:3:5'}],has_more:false});await second;
  assert.equal(h.$('delivery-results').children.length,2);
  assert.equal(h.$('delivery-more').hidden,true);
  const detail=h.$('delivery-results').children[0].children[1].onclick();
  assert.equal(h.requests[2].path,'/api/podcast-delivery?native_id=2%3A3%3A4');
  h.requests[2].resolve({state:'ok',source:{position:92,total:100,completed:true,observed_at:200},
    event:{stage:'acknowledged',ready_at:198,offered_at:199,acknowledged_at:null},target:{state:'unverified'}});
  await detail;
  assert.match(h.$('delivery-stages').content(),/GPodder-Client.*Cursor bestätigt/);
  assert.match(h.$('delivery-stages').content(),/Floppy-Bibliothek.*keine Bestätigung/);
  assert.doesNotMatch(h.$('delivery-stages').content(),/null|undefined|Abruf bestätigt/);
});
test('Floppy confirmation cites its own observation separately from the transport receipt',()=>{
  const h=fixture();
  h.ctx.describeDelivery({source:{position:128,completed:false,observed_at:20},event:{stage:'offered'},
    target:{state:'verified',completed:false,position:128,observed_at:28,synced_at:27}});
  const text=h.$('delivery-stages').content();
  assert.match(text,/Zielstand geprüft 28/);
  assert.match(text,/Floppy.*2 min 08 s.*Hörstand in der Floppy-Bibliothek bestätigt/);
  assert.match(text,/Empfangsbestätigung steht aus/);
  assert.doesNotMatch(text,/keine Bestätigung/);
});
test('stale search and detail responses cannot populate the UI after another selection or logout',async()=>{
  const h=fixture();
  const old=h.ctx.openDelivery('episode_id','abcd /',{title:'Folge'});
  assert.equal(h.requests[0].path,'/api/podcast-delivery?episode_id=abcd%20%2F');
  h.$('delivery-dialog').close();
  const current=h.ctx.openDelivery('native_id','2:3:4',{title:'Titel'});
  h.requests[1].resolve({state:'scope_mismatch'});await current;
  h.requests[0].reject(new Error('alte Fehlermeldung'));await old;
  assert.match(h.$('delivery-status').content(),/aktuellen PinePods-Verbindung/);
  const pending=h.ctx.findDeliveries();
  h.state.authenticated=false;h.ctx.resetDelivery();
  h.requests[2].resolve({state:'ok',rows:[{title:'privat'}],has_more:false});await pending;
  assert.equal(h.$('delivery-results').children.length,0);
  assert.equal(h.$('delivery-stages').children.length,0);
  assert.equal(h.$('delivery-episode').content(),'');
});
