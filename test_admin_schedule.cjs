'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, 'admin_static/app.js'), 'utf8');
const formSource = source.slice(source.indexOf('function scheduleInputs()'), source.indexOf('function connectionSchema('));

function form() {
  const input = (type, name) => ({
    type, dataset: {schedule: name}, value: '', checked: false,
    get valueAsNumber() { return Number(this.value); },
    addEventListener(kind, callback) { this[kind] = callback; },
  });
  const fields = [input('checkbox','series_enabled'), input('number','series_interval_seconds')];
  const nodes = new Map();
  const $ = name => {
    if (!nodes.has(name)) nodes.set(name,input());
    return nodes.get(name);
  };
  $('schedule-form').querySelectorAll = () => fields;
  const text = (name, message) => { $(name).textContent = message; };
  const requests = [];
  const state = {busy: false, status: {schedule: {revision: 10, values: {
    series_enabled: false, series_interval_seconds: 3600, integrity_window_utc: null,
  }}}};
  const runCommand = async (kind, payload) => {
    requests.push([kind, JSON.parse(JSON.stringify(payload))]);
    return {revision: 11, values: {...state.status.schedule.values, ...payload.changes}};
  };
  const context = vm.createContext({$,text,state,runCommand,epoch:()=>0,formatDate:String});
  vm.runInContext(formSource,context);
  context.renderSchedule();
  return {$,fields,state,requests,input,refresh:context.renderSchedule,
    change: () => $('schedule-form').input(),
    submit: () => $('schedule-form').submit({preventDefault() {}})};
}

test('scheduled restore edits preserve unrelated settings and show an overdue check in the GUI', async()=>{
  const h=form();
  const enabled=h.input('checkbox','restore_enabled'), days=h.input('number','restore_interval_days');
  h.fields.push(enabled,days);
  Object.assign(h.state.status.schedule.values,{restore_enabled:true,restore_interval_days:7});
  h.state.status.restore_drill={schedule:{state:'not_running',error:'not_running',next_at:123}};
  h.refresh();
  assert.match(h.$('schedule-restore-status').textContent,/überfällig/);
  assert.match(h.$('schedule-restore-status').textContent,/123/);
  assert.equal(enabled.checked,true);
  days.value='14';h.change();await h.submit();
  assert.deepEqual(h.requests,[['schedule.update',{expected_revision:10,changes:{restore_interval_days:14}}]]);
});

test('a background refresh preserves an edit and the revision the operator actually reviewed',async () => {
  const h=form();
  h.fields[0].checked=true;
  h.change();
  h.state.status.schedule={revision:11,values:{series_enabled:false,series_interval_seconds:7200}};
  h.refresh();
  assert.equal(h.fields[0].checked,true);
  assert.equal(h.fields[1].value,3600);
  await h.submit();
  assert.deepEqual(h.requests,[['schedule.update',{expected_revision:10,changes:{series_enabled:true}}]]);
});

test('clearing both EPUB hours clears an existing window, while a single hour cannot be submitted', async () => {
  const h=form();
  h.state.status.schedule.values.integrity_window_utc={start_hour:1,end_hour:3};
  h.refresh();
  h.$('integrity-start').value='';
  h.change();
  await h.submit();
  assert.match(h.$('schedule-message').textContent,/beide Stunden/);
  assert.equal(h.requests.length,0);
  h.$('integrity-end').value='';
  await h.submit();
  assert.deepEqual(h.requests,[['schedule.update',{expected_revision:10,changes:{integrity_window_utc:null}}]]);
});
