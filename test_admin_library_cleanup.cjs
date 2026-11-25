'use strict';
const assert=require('node:assert/strict');
const test=require('node:test');
const fs=require('node:fs');
const vm=require('node:vm');

const html=fs.readFileSync(__dirname+'/admin_static/index.html','utf8');
const app=fs.readFileSync(__dirname+'/admin_static/app.js','utf8');
const start=app.indexOf('async function previewMissing(');
const end=app.indexOf('function renderEbookImports()',start);

function fixture(){
  const nodes=new Map(), calls=[];
  function element(tag,cls,message){
    return {tag,children:[],textContent:message||'',checked:false,open:false,
      addEventListener(name,handler){this['on'+name]=handler;},
      append(...nodes){this.children.push(...nodes);},
      showModal(){this.open=true;},
      close(){this.open=false;this.onclose();}};
  }
  function $(id){
    assert.ok(html.includes('id="'+id+'"'));
    if(!nodes.has(id)) nodes.set(id,element('div'));
    return nodes.get(id);
  }
  const state={busy:false,authenticated:true};
  const rows=[
    {id:'confirmed',local_id:'abs-1',title:'Hörbuch <Titel>',missing:['Audiobookshelf'],approvals:['Mitlese-Freigabe'],source:'Automatisch'},
    {id:'pending',local_id:'abs-2',title:'Weiterer Titel',missing:['BookOrbit'],approvals:[],source:'Konfiguration',pending:true}
  ];
  const ctx=vm.createContext({state,element,$,clear:n=>{n.children=[];},text:(id,s)=>{$(id).textContent=s;},toast(){},
    runCommand:async(kind,payload)=>{
      calls.push([kind,JSON.parse(JSON.stringify(payload))]);
      return kind==='library.preview_missing'?{review_id:'check',scope:['ABS • mein Konto','BookOrbit • Bücher'],rows}:{retired:1};
    },confirmation:async()=>true});
  vm.runInContext(app.slice(start,end),ctx);
  return {$,calls,rows,ctx};
}

test('library selection starts empty and confirms exactly the checked row in the checked scope',async()=>{
  const h=fixture();
  await h.$('audio-missing').onclick();
  assert.deepEqual(h.calls,[['library.preview_missing',{media:'audio'}]]);
  assert.match(h.$('missing-scope').textContent,/mein Konto/);
  const [first,locked]=h.$('missing-rows').children;
  assert.equal(first.children[1].children[0].textContent,'Hörbuch <Titel>');
  assert.match(first.children[1].children[2].textContent,/Mitlese-Freigabe/);
  assert.equal(locked.children[0].disabled,true);
  assert.equal(h.$('missing-apply').disabled,true);
  await h.$('missing-apply').onclick(); assert.equal(h.calls.length,1);
  first.children[0].checked=true;first.children[0].onchange();
  assert.equal(h.$('missing-apply').disabled,false);
  await h.$('missing-apply').onclick();
  assert.deepEqual(h.calls[1],['library.retire_missing',{review_id:'check',selected:['confirmed'],confirm:true}]);
  assert.equal(h.$('missing-dialog').open,false);
});

test('E-Book preview with all entries present offers no deletion',async()=>{
  const h=fixture();h.rows.length=0;
  await h.$('book-missing').onclick();
  assert.deepEqual(h.calls,[['library.preview_missing',{media:'ebook'}]]);
  assert.match(h.$('missing-status').textContent,/vorhanden/);
  assert.equal(h.$('missing-apply').disabled,true);
});
