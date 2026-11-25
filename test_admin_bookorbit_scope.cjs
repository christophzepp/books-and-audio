'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const app = fs.readFileSync(path.join(__dirname,'admin_static/app.js'),'utf8');
const html = fs.readFileSync(path.join(__dirname,'admin_static/index.html'),'utf8');
const start = app.indexOf('function clearScopePreview()');
const end = app.indexOf('function renderProvider()',start);
assert.ok(start >= 0 && end > start);

function form() {
  const nodes = new Map();
  const element = (tag, cls, message) => ({
    type: tag, value: '', children: [], checked: false, hidden: false, textContent: message || '',
    append(...children) { this.children.push(...children); },
    replaceChildren() { this.children=[]; this.textContent=''; },
    addEventListener(name,listener) { this[name]=listener; },
    content() { return [this.textContent,...this.children.map(child=>child.content())].join(' '); },
  });
  const $ = id => {
    assert.ok(html.includes('id="'+id+'"'),'shipped markup missing '+id);
    if (!nodes.has(id)) nodes.set(id,element('div'));
    return nodes.get(id);
  };
  const active={user_id:7,library_ids:[1],ebook_library_id:2,libraries:[{id:1,roots:['/audio/old']},{id:2,roots:['/ebook/old']}]};
  const proposed={user_id:9,library_ids:[3,4],ebook_library_id:null,libraries:[{id:3,roots:['/audio/three']},{id:4,roots:['/audio/<four>']}]};
  const state={status:{bookorbit_scope:{available:true,active,profile_id:'bo-a'}},connections:{revision:5,profiles:[
    {kind:'bookorbit',id:'bo-a',active:true,name:'Anfang'},
    {kind:'bookorbit',id:'bo-b',active:false,name:'Ziel',settings:{username:'another',library_ids:[3,4]}},
    {kind:'jellyfin',id:'jf',active:true,name:'Excluded'},
  ]}};
  const requests=[];
  const preview={review_id:'r1',digest:'digest1',previous:active,proposed};
  const ctx=vm.createContext({$,state,element,text:(id,value)=>$(id).textContent=value,clear:n=>n.replaceChildren(),
    runCommand:async(kind,payload)=>{requests.push([kind,JSON.parse(JSON.stringify(payload))]); return kind.endsWith('preview_scope')?preview:{};}});
  vm.runInContext(app.slice(start,end),ctx);
  $('bo-scope').hidden=true;
  ctx.renderBookScope();
  return {$,state,ctx,requests,preview,
    select:()=>{$('bo-scope-profile').value='bo-b'; $('bo-scope-profile').change();},
    submit:()=>$('bo-scope-form').submit({preventDefault(){}}),
    confirm:()=>{$('bo-scope-confirm').checked=true; $('bo-scope-confirm').change();},
    apply:()=>$('bo-scope-approve').click()};
}

test('saved profile, exact account and paths are visible before explicit approval of the reviewed digest',async()=>{
  const h=form();
  assert.equal(h.$('bo-scope-profile').value,'bo-a');
  assert.deepEqual(h.$('bo-scope-profile').children.map(p=>p.value),['bo-a','bo-b']);
  assert.equal(h.$('bo-scope-audio').value,'1');
  assert.equal(h.$('bo-scope-ebook').value,'2');
  h.select();
  assert.equal(h.$('bo-scope-ebook').value,'');
  await h.submit();
  assert.deepEqual(h.requests,[['bookorbit.preview_scope',{expected_revision:5,profile_id:'bo-b',library_ids:[3,4],ebook_library_id:null}]]);
  assert.match(h.$('bo-scope-previous').content(),/#7.*Hörbuch #1.*\/audio\/old.*E-Book #2.*\/ebook\/old/);
  assert.match(h.$('bo-scope-proposed').content(),/#9.*\/audio\/three.*\/audio\/<four>/);
  assert.equal(h.$('bo-scope-review').hidden,false);
  assert.equal(h.$('bo-scope-approve').disabled,true);
  await h.apply();
  assert.equal(h.requests.length,1);
  h.confirm();
  assert.equal(h.$('bo-scope-approve').disabled,false);
  await h.apply();
  await h.apply();
  assert.deepEqual(h.requests[1],['bookorbit.approve_scope',{review_id:'r1',digest:'digest1',confirm:true}]);
  assert.equal(h.requests.length,2);
});

test('editing, changing a profile, refreshed configuration, and logout invalidate a previous approval',async()=>{
  const h=form();
  h.select(); await h.submit(); h.confirm();
  h.$('bo-scope-ebook').value='2';
  h.$('bo-scope-ebook').input();
  await h.apply();
  assert.equal(h.requests.length,1);
  await h.submit(); h.confirm();
  h.$('bo-scope-profile').change();
  assert.equal(h.$('bo-scope-review').hidden,true);
  await h.submit(); h.confirm();
  h.state.connections.revision++;
  h.ctx.renderBookScope();
  await h.apply();
  assert.equal(h.requests.length,3);
  assert.match(h.$('bo-scope-message').textContent,/erneut/);
  await h.submit(); h.confirm();
  h.ctx.resetBookScope(); await h.apply();
  assert.equal(h.requests.length,4);
  assert.equal(h.$('bo-scope').hidden,true);
  assert.equal(h.$('bo-scope-proposed').content(),'');
});

test('invalid IDs or missing roots or review ID never authorize apply',async()=>{
  const h=form();
  for (const bad of ['0','-1','1.5','1,1','17,abc','','99999999999999999999999']) {
    h.$('bo-scope-audio').value=bad;
    await h.submit();
    assert.equal(h.requests.length,0);
  }
  h.select(); h.preview.proposed.libraries[0].roots=[];
  await h.submit(); h.confirm(); await h.apply();
  assert.equal(h.requests.length,1); assert.equal(h.$('bo-scope-review').hidden,true);
  h.preview.review_id=null;
  await h.submit(); h.confirm(); await h.apply();
  assert.equal(h.requests.length,2); assert.match(h.$('bo-scope-message').textContent,/Freigabekennung/);
});

test('an outstanding preview cannot reappear after BookOrbit scope control becomes unavailable',async()=>{
  const h=form();
  let finish;
  h.ctx.runCommand=()=>new Promise(resolve=>{finish=resolve;});
  const waiting=h.submit();
  h.state.status.bookorbit_scope.available=false;
  h.ctx.renderBookScope();
  finish(h.preview); await waiting;
  assert.equal(h.$('bo-scope').hidden,true); assert.equal(h.$('bo-scope-proposed').content(),'');
  assert.equal(h.$('bo-scope-approve').disabled,true);
});
