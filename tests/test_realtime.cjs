const assert=require('node:assert/strict');
const {RefreshLoop}=require('../static/realtime.js');
(async()=>{
 let time=0,visible=true,interval=30000,market=0,scan=0,page='main',release;
 const loop=new RefreshLoop({now:()=>time,visible:()=>visible,interval:()=>interval,tasks:{
  market:{delay:n=>n,eligible:()=>page==='main',run:async()=>{market++;}},
  scan:{delay:n=>Math.max(60000,n*2),eligible:()=>page==='scan',run:async()=>{scan++;await new Promise(r=>release=r);}}
 }});
 await loop.run('market');assert.equal(market,1);
 time=29999;await loop.run('market');assert.equal(market,1);
 time=30000;await loop.run('market');assert.equal(market,2);
 visible=false;time=60000;await loop.run('market');assert.equal(market,2);
 visible=true;interval=0;await loop.run('market');assert.equal(market,2);
 await loop.run('market',{force:true});assert.equal(market,3); // manual works while paused
 interval=30000;page='scan';const pending=loop.run('scan');await Promise.resolve();assert.equal(scan,1);
 time+=90000;loop.run('scan');await Promise.resolve();assert.equal(scan,1); // never overlaps
 release();await pending;await loop.run('scan');assert.equal(scan,1); // interval measured after completion
 time+=60000;const second=loop.run('scan');await Promise.resolve();assert.equal(scan,2);release();await second;
 // An old request finishing after a mode change cannot advance the new mode's clock.
 time+=60000;const old=loop.run('scan');await Promise.resolve();const oldRelease=release;
 loop.reset();oldRelease();await old;assert.equal(loop.last.has('scan'),false);
 page='main';await loop.run('market');assert.equal(market,4);
 let failures=0;const errorLoop=new RefreshLoop({now:()=>time,tasks:{quote:{delay:()=>30000,eligible:()=>true,run:()=>{failures++;throw Error('offline');}}}});
 await errorLoop.run('quote');await errorLoop.run('quote');assert.equal(failures,1); // retry waits; no request storm
 const started=new RefreshLoop({tasks:{}});started.start();assert.notEqual(started.timer,null);started.stop();assert.equal(started.timer,null);
 console.log('Refresh scheduler: cadence, pause, visibility, overlap, mode switch, and retry tests passed.');
})().catch(e=>{console.error(e);process.exit(1);});
