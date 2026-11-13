const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('static/terminal.js','utf8'),fn=source.slice(source.indexOf('async function api('),source.indexOf('function navigate('));
let response;const ctx=vm.createContext({AbortController,setTimeout,clearTimeout,fetch:async()=>response});vm.runInContext(fn,ctx);
(async()=>{
 for(const status of [401,502,504]){response={status,headers:{get:()=> 'text/html'},json:()=>{throw Error('JSON should not be parsed')}};await assert.rejects(vm.runInContext("api('/test')",ctx),status===401?/重新登录/:new RegExp('HTTP '+status));}
 response={ok:false,status:503,headers:{get:()=> 'application/json'},json:async()=>({ok:false,error:'数据源暂不可用'})};await assert.rejects(vm.runInContext("api('/test')",ctx),/数据源暂不可用/);
 response={ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({ok:true,data:{price:10}})};assert.equal((await vm.runInContext("api('/test')",ctx)).price,10);
 console.log('API responses: login expiry, HTML gateway failures and JSON API results handled clearly.');
})().catch(e=>{console.error(e);process.exitCode=1;});
