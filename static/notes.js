/* Server-backed notebook; intentionally independent of quote mode and trading. */
const notesState={data:null,current:null,dirty:false,busy:false};
const noteFields=['title','category','summary','conditions','actions','invalidation','risk','source_url','time_range','verification','tags'];
function notesMessage(text,error=false){const el=$('notesMessage');el.textContent=text;el.classList.toggle('notes-error',error);}
function notesSetDirty(value){notesState.dirty=value;$('notesDirty').textContent=value?'未保存':'';}
function notesCanLeave(){return !notesState.dirty||window.confirm('有未保存的笔记，放弃本次修改？');}
function noteSourceLink(){const el=$('notesSource'),value=$('notesForm').elements.source_url.value.trim();try{const url=new URL(value);if(!['http:','https:'].includes(url.protocol)||url.username||url.password)throw Error();el.href=url.href;el.hidden=false;}catch{el.removeAttribute('href');el.hidden=true;}}
async function loadNotes(force=false){
 if(notesState.busy)return;
 if(notesState.data&&!force)return;
 if(force&&!notesCanLeave())return;
 notesState.busy=true;notesMessage('正在读取笔记…');
 try{const data=await api('/api/terminal/notes');notesState.data=data;
  for(const [id,values,label] of [['notesCategory',data.categories,'全部分类'],['notesStatus',Object.keys(data.statuses),'全部状态']]){const select=$(id),old=select.value;select.innerHTML=`<option value="">${label}</option>`+values.map(v=>`<option value="${esc(v)}">${esc(data.statuses[v]||v)}</option>`).join('');select.value=old;}
  const form=$('notesForm');form.elements.category.innerHTML=data.categories.map(v=>`<option>${esc(v)}</option>`).join('');form.elements.verification.innerHTML=Object.entries(data.statuses).map(([v,t])=>`<option value="${esc(v)}">${esc(t)}</option>`).join('');
  const c=data.collection;$('notesProgress').textContent=`合集 ${c.total} 集 · 已录目录 ${c.catalogued} 集 · 已转写 ${c.transcribed||0} 集 · 原声已核对 ${c.verified} 集`;
  const oldId=notesState.current?.id;const item=data.items.find(n=>n.id===oldId)||data.items.find(n=>!n.archived);renderNotesList();if(item)openNote(item);else newNote(false);notesMessage('笔记保存在网站服务器；目录与正文核验进度分开记录。');
 }catch(e){notesMessage(e.message,true);}finally{notesState.busy=false;}
}
function renderNotesList(){if(!notesState.data)return;const query=$('notesSearch').value.trim().toLowerCase(),category=$('notesCategory').value,status=$('notesStatus').value,archived=$('notesArchived').checked;
 const items=notesState.data.items.filter(n=>Boolean(n.archived)===archived&&(!category||n.category===category)&&(!status||n.verification===status)&&(!query||noteFields.map(k=>n[k]||'').join(' ').toLowerCase().includes(query)));
 $('notesList').innerHTML=`<div class="notes-list-count">${items.length} 条${archived?'已归档':''}</div>`+items.map(n=>`<button type="button" class="notes-item ${notesState.current?.id===n.id?'selected':''}" data-note-id="${esc(n.id)}"><span class="notes-item-meta">${n.episode?'第 '+n.episode+' 集':esc(n.category)}<small class="note-status ${esc(n.verification)}">${esc(notesState.data.statuses[n.verification])}</small></span><b>${esc(n.title.replace(/#.*$/,''))||esc(n.title)}</b><span>${esc(n.summary?n.summary.slice(0,65):'仅已取得目录标题，正文待提取')}</span></button>`).join('')+(items.length?'':'<div class="empty compact">没有符合条件的笔记</div>');
}
function openNote(note){notesState.current=note;const form=$('notesForm');form.hidden=false;for(const k of noteFields)form.elements[k].value=note[k]||'';
 $('notesEditorTitle').textContent=note.episode?`第 ${note.episode} 集 · 笔记`:'研究笔记';$('notesSavedAt').textContent=note.updated?`已保存 ${note.updated}`:'尚未保存';$('notesArchive').hidden=!note.id;$('notesArchive').textContent=note.archived?'恢复笔记':'归档';
 notesSetDirty(false);noteSourceLink();updateNoteHint();renderNotesList();}
function newNote(check=true){if(check&&!notesCanLeave())return;openNote({title:'',category:'待分类',verification:'personal'});$('notesForm').elements.title.focus();}
function updateNoteHint(){const status=$('notesForm').elements.verification.value;$('notesSourceHint').textContent={catalog:'当前仅有目录信息，尚未获取正文，请勿当作作者操作结论。',transcribed:'转写尚未核对原声，注意术语、数字和否定句。',verified:'请确保已对照原声和相关画面，记录来源与对应时间段。',personal:'个人整理与理解；引用作者观点时请补充原视频来源。'}[status]||'';}
function upsertNote(item){const i=notesState.data.items.findIndex(n=>n.id===item.id);if(i<0)notesState.data.items.unshift(item);else notesState.data.items[i]=item;const c=notesState.data.collection;c.transcribed=notesState.data.items.filter(n=>n.episode&&['transcribed','verified'].includes(n.verification)&&!n.archived).length;c.verified=notesState.data.items.filter(n=>n.episode&&n.verification==='verified'&&!n.archived).length;$('notesProgress').textContent=`合集 ${c.total} 集 · 已录目录 ${c.catalogued} 集 · 已转写 ${c.transcribed||0} 集 · 原声已核对 ${c.verified} 集`;openNote(item);}
function initNotes(){
 for(const id of ['notesSearch','notesCategory','notesStatus','notesArchived'])$(id).addEventListener('input',renderNotesList);
 $('notesNew').onclick=()=>{if(notesState.data&&!notesState.busy)newNote();};$('notesReload').onclick=()=>loadNotes(true);
 $('notesList').onclick=e=>{const b=e.target.closest('[data-note-id]');if(!b||notesState.busy||!notesCanLeave())return;openNote(notesState.data.items.find(n=>n.id===b.dataset.noteId));};
 $('notesForm').addEventListener('input',()=>{notesSetDirty(true);noteSourceLink();updateNoteHint();});
 $('notesForm').onsubmit=async e=>{e.preventDefault();if(notesState.busy)return;const note=Object.fromEntries(noteFields.map(k=>[k,$('notesForm').elements[k].value]));if(notesState.current?.id){note.id=notesState.current.id;note.revision=notesState.current.revision;}
  notesState.busy=true;$('notesForm').inert=true;$('notesSave').disabled=true;try{const saved=await api('/api/terminal/notes',{method:'POST',body:JSON.stringify({action:'save',note})});upsertNote(saved);notesMessage('笔记已保存。');}catch(error){notesMessage(error.message,true);}finally{notesState.busy=false;$('notesForm').inert=false;$('notesSave').disabled=false;}};
 $('notesArchive').onclick=async()=>{if(notesState.busy||!notesState.current?.id||!notesCanLeave())return;notesState.busy=true;try{const n=notesState.current;const saved=await api('/api/terminal/notes',{method:'POST',body:JSON.stringify({action:n.archived?'restore':'archive',id:n.id,revision:n.revision})});upsertNote(saved);notesMessage(saved.archived?'已归档，可勾选“查看已归档”后恢复。':'已恢复笔记。');}catch(e){notesMessage(e.message,true);}finally{notesState.busy=false;}};
 window.addEventListener('beforeunload',e=>{if(notesState.dirty){e.preventDefault();e.returnValue='';}});
}
