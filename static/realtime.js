/* Small scheduler shared by live UI and deterministic clock tests. */
(function(root){
  'use strict';
  class RefreshLoop {
    constructor({tasks,now=()=>Date.now(),visible=()=>true,interval=()=>30000,onStatus=()=>{},setTimer=(fn,ms)=>root.setInterval(fn,ms),clearTimer=id=>root.clearInterval(id)}) {
      Object.assign(this,{tasks,now,visible,interval,onStatus,setTimer,clearTimer});
      this.last=new Map();this.running=new Map();this.epoch=0;this.timer=null;
    }
    reset(){this.epoch++;this.last.clear();this.running.clear();this.onStatus();}
    async run(name,{force=false}={}){
      const task=this.tasks[name];if(!task)return;
      if(this.running.has(name))return this.running.get(name).promise;
      const delay=task.delay(this.interval());
      if(!force&&(!this.interval()||!this.visible()||!task.eligible()||this.now()-(this.last.get(name)??-Infinity)<delay))return;
      const epoch=this.epoch;
      const entry={promise:null};this.running.set(name,entry);
      entry.promise=Promise.resolve().then(()=>task.run()).catch(error=>{if(task.onError)task.onError(error);}).finally(()=>{
        if(epoch===this.epoch&&this.running.get(name)===entry){this.last.set(name,this.now());this.running.delete(name);this.onStatus();}
      });
      this.onStatus();return entry.promise;
    }
    tick(){for(const name of Object.keys(this.tasks))this.run(name);this.onStatus();}
    start(){if(this.timer===null)this.timer=this.setTimer(()=>this.tick(),1000);this.tick();}
    stop(){if(this.timer!==null)this.clearTimer(this.timer);this.timer=null;}
    secondsUntil(name){const task=this.tasks[name];if(!task)return 0;return Math.max(0,Math.ceil((task.delay(this.interval())-(this.now()-(this.last.get(name)??-Infinity)))/1000));}
  }
  root.RefreshLoop=RefreshLoop;
  if(typeof module!=='undefined'&&module.exports)module.exports={RefreshLoop};
})(typeof window!=='undefined'?window:globalThis);
