import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { createApiClient } from '../../static/api-client.js';

function setup() {
  const calls=[];
  const context=vm.createContext({ AbortController, AbortSignal, Date, expireRequests:()=>0,
    document:{addEventListener(){}},window:{addEventListener(){}},localStorage:{getItem:()=>null},
    begin: kind=>new Promise(resolve=>calls.push({kind,resolve})),
  });
  const code=readFileSync(new URL('../../static/usage.js',import.meta.url),'utf8')
    .replace(/^import .*;$/mg,'').replace('export function refreshUsageView','function refreshUsageView');
  vm.runInContext(code,context);
  vm.runInContext('loadAll=()=>begin("read"); collectNow=()=>begin("collect");',context);
  return {context,calls};
}

test('manual usage refresh during automatic read collects after that read, only once',async()=>{
  const {context,calls}=setup();
  const automatic=vm.runInContext('refreshUsageView(false)',context);
  const manual=vm.runInContext('refreshUsageView(true)',context);
  assert.strictEqual(vm.runInContext('refreshUsageView(true)',context),manual);
  assert.equal(calls.length,1);
  calls[0].resolve(); await automatic; await new Promise(r=>setImmediate(r));
  assert.equal(calls.length,2);
  assert.equal(calls[1].kind,'collect');
  calls[1].resolve(); await manual;
});

test('automatic read cannot cancel active manual collection',async()=>{
  const {context,calls}=setup();
  const manual=vm.runInContext('refreshUsageView(true)',context);
  assert.strictEqual(vm.runInContext('refreshUsageView(false)',context),manual);
  assert.equal(calls[0].kind,'collect');
  assert.equal(calls.length,1);
  calls[0].resolve(); await manual;
});

function actualSetup() {
  const calls=[];
  const client=createApiClient({fetchFn:(url,options)=>new Promise(resolve=>calls.push({url,options,resolve}))});
  const context=vm.createContext({ AbortController, AbortSignal, Date, URLSearchParams,
    apiJSON:client.json,expireRequests:client.expire,
    document:{addEventListener(){},querySelector:()=>null},
    window:{addEventListener(){}},localStorage:{getItem:()=>null,setItem(){}},
  });
  const code=readFileSync(new URL('../../static/usage.js',import.meta.url),'utf8')
    .replace(/^import .*;$/mg,'').replace('export function refreshUsageView','function refreshUsageView');
  vm.runInContext(code,context);
  vm.runInContext('renderUsage=()=>{}; renderUsageSkeleton=()=>{}; usageState.loaded=true; usageState.overview={requests:10};',context);
  return {context,calls};
}
const respond = (call, data, ok=true) => call.resolve({ok,status:ok?200:500,json:async()=>data});

test('overlapping usage batches never retain half-completed data after failure',async()=>{
  const {context,calls}=actualSetup();
  const first=vm.runInContext('reloadAll()',context);
  respond(calls[0],{requests:20});
  await new Promise(r=>setImmediate(r));
  assert.equal(vm.runInContext('usageState.overview.requests',context),10);
  const second=vm.runInContext('usageState.source="codex"; reloadAll()',context);
  calls.slice(5).forEach((c,i)=>respond(c,{},i!==1));
  await second;
  calls.slice(1,5).forEach(c=>respond(c,{})); await first;
  assert.equal(vm.runInContext('usageState.overview.requests',context),10);
});

test('filter changes do not abort collection and completion reloads latest filters',async()=>{
  const {context,calls}=actualSetup();
  const collection=vm.runInContext('collectNow()',context);
  const read=vm.runInContext('usageState.source="codex"; reloadAll()',context);
  assert.equal(calls[0].options.signal.aborted,false);
  calls.slice(1).forEach(c=>respond(c,{})); await read;
  respond(calls[0],{}); await new Promise(r=>setImmediate(r));
  assert.equal(calls.length,11);
  assert.ok(calls[6].url.includes('source=codex'));
  calls.slice(6).forEach(c=>respond(c,{})); await collection;
});

test('failed filter change restores the labels matching retained data',async()=>{
  const {context,calls}=actualSetup();
  const first=vm.runInContext('reloadAll()',context);
  calls.forEach(c=>respond(c,{requests:12})); await first;
  const second=vm.runInContext('usageState.source="codex"; usageState.days=30; reloadAll()',context);
  calls.slice(5).forEach((c,i)=>respond(c,{},i!==1)); await second;
  assert.equal(vm.runInContext('usageState.overview.requests',context),12);
  assert.equal(vm.runInContext('usageState.source',context),'');
  assert.equal(vm.runInContext('usageState.days',context),14);
});
