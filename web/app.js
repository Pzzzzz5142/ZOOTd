'use strict';
const $ = id => document.getElementById(id);
const labels = {success:'已通过',failed:'未通过',invalid:'证据异常',unfinished:'未结束',succeeded:'通过','policy-resolved':'策略完成','not-applicable':'无需执行',degraded:'降级',pending:'尚无记录'};
const phases = {'runtime-readiness':'运行环境','device-readiness':'设备准备',depot:'仓库扫描',daily:'基建与日常','source-refresh':'来源刷新',annihilation:'每周剿灭',farming:'材料刷图',award:'奖励领取',cleanup:'设备清理'};
const modes = {full:'完整托管',award:'奖励领取','dry-run':'静态检查',device:'设备检查'};
let state = {offset:0,total:0,runs:[],filter:'all',selected:null,latest:null,busy:false};
function node(tag,text,className){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(className)n.className=className;return n;}
function date(value){return value?new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'—';}
function duration(run){if(!run.finished_at)return '—';const s=Math.max(0,Math.round((new Date(run.finished_at)-new Date(run.started_at))/1000));return s<60?`${s} 秒`:`${Math.floor(s/60)} 分 ${s%60} 秒`;}
function badge(status){return node('span',labels[status]||status,'badge '+status);}
async function api(path){const r=await fetch(path,{signal:AbortSignal.timeout(8000)});if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}
function render(){
 $('rows').replaceChildren();
 const query=$('search').value.trim().toLowerCase();
 const visible=state.runs.filter(r=>(state.filter==='all'||(state.filter==='success'?r.status==='success':r.status!=='success'))&&r.run_id.toLowerCase().includes(query));
 $('empty').hidden=visible.length>0;
 $('empty').textContent=state.total===0?'暂无运行记录。完成一次托管运行后，记录会显示在这里。':'当前页没有符合筛选条件的记录。';
 for(const r of visible){const tr=node('tr');const time=node('td',date(r.started_at));time.append(node('small',r.run_id));tr.append(time,node('td',modes[r.mode]||'未知'));
 const status=node('td');status.append(badge(r.status));tr.append(status);
 const done=r.phases.filter(p=>['succeeded','policy-resolved','not-applicable'].includes(p.result)).length;
 tr.append(node('td',`${done} / ${r.expected_phases?.length||'—'}`),node('td',duration(r)));
 const action=node('td'),button=node('button','查看详情 ↗','view');button.addEventListener('click',()=>select(r.run_id));action.append(button);tr.append(action);$('rows').append(tr);}
 $('success').textContent=`${state.runs.filter(r=>r.status==='success').length} / ${state.runs.length}`;
 $('attention').textContent=state.runs.filter(r=>r.status!=='success').length;
 $('total').textContent=`共 ${state.total} 次运行`;$('page').textContent=Math.floor(state.offset/30)+1;
 $('prev').disabled=state.offset===0||state.busy;$('next').disabled=state.offset+30>=state.total||state.busy;
}
function detail(r){
 $('detail').hidden=false;const content=$('detail-content');content.replaceChildren();
 content.append(node('p',r.run_id,'muted'),badge(r.status));
 content.append(node('p',`开始：${date(r.started_at)} · 最近事件：${date(r.updated_at)}`));
 if(r.error){content.append(node('p',r.error));return;}
 content.append(node('p',`代码版本：${r.repository?.head?.slice(0,12)||'未知'} · 已校验事件哈希链`,'muted'));
 for(const name of r.expected_phases){const p=r.phases.find(p=>p.phase===name),row=node('div',undefined,'phase'),left=node('div',phases[name]||name),right=node('div');right.append(badge(p?.result||'pending'));
 if(p){right.append(node('p',`${date(p.recorded_at)} · ${p.outcome}`));if(Object.keys(p.details||{}).length){const d=node('details');d.append(node('summary','阶段数据'),node('pre',JSON.stringify(p.details,null,2)));right.append(d);}if(p.evidence_files?.length)right.append(node('p',`证据文件：${p.evidence_files.map(f=>f.path).join('、')}`));}
 row.append(left,right);content.append(row);}
 if(r.recovery?.length)content.append(node('p',`恢复事件：${r.recovery.map(e=>`${e.event_type} (${date(e.recorded_at)})`).join(' → ')}。原始运行结果保留；恢复重跑作为独立运行展示。`,'muted'));
}
async function select(id){state.selected=id;$('detail').hidden=false;$('detail-content').textContent='正在读取阶段详情…';try{const r=await api('/api/runs/'+encodeURIComponent(id));if(state.selected===id)detail(r);}catch(e){if(state.selected===id)$('detail-content').textContent='详情读取失败，请稍后重试。';}}
async function refresh(){
 if(state.busy)return;state.busy=true;$('refresh').disabled=true;render();
 try{const [status,list]=await Promise.all([api('/api/status'),api(`/api/runs?offset=${state.offset}&limit=30`)]);
 state.runs=list.runs;state.total=list.total;
 const activity=status.activity;
 $('service').textContent={running:'运行中',idle:'空闲',unknown:'状态未知'}[activity.state]||'状态未知';
 $('service-detail').textContent=activity.units.length?activity.units.map(s=>`${s.unit} · ${s.SubState} · PID ${s.MainPID}`).join('；'):activity.state==='idle'?'当前没有正在执行的定时托管任务':'部分服务状态无法读取';
 state.latest=status.latest_run;
 $('latest-result').textContent=state.latest?(labels[state.latest.status]||state.latest.status):'暂无记录';
 $('latest-detail').textContent=state.latest?`${modes[state.latest.mode]||'未知模式'} · ${date(state.latest.started_at)}${state.latest.finished_at?' · 耗时 '+duration(state.latest):''}`:'尚无托管运行记录';
 $('latest-view').hidden=!state.latest;
 const failures=status.service_failures;
 const incomplete=Object.values(status.services).some(s=>!s.available);
 $('service-failures').textContent=failures.length?`${failures.length} 个服务`:incomplete?'状态未知':'无';
 $('failure-detail').replaceChildren();
 for(const s of failures){$('failure-detail').append(node('div',`${s.unit} · ${s.Result} · 退出码 ${s.ExecMainStatus||'未知'}`),node('div',s.ExecMainExitTimestamp||'退出时间未知'));}
 $('failure-detail').append(node('div',failures.length?'保留的上次失败状态；不代表当前仍在运行。':incomplete?'部分服务状态无法读取。':'systemd 未保留失败状态；历史运行记录见下方。'));
 const receipt=status.runtime.receipt;$('core').textContent=receipt?.core?.active_version||'暂无记录';$('runtime').textContent=receipt?`receipt：${receipt.status||'未知'} · ${date(receipt.checked_at)}`:'暂无 runtime receipt';
 $('connection').textContent=`● 数据已更新 · ${date(status.observed_at)}`;
 if(state.selected){const r=await api('/api/runs/'+encodeURIComponent(state.selected));if(r.run_id===state.selected)detail(r);}
 }catch(e){$('connection').textContent='连接失败 · 当前显示为上次快照，请检查面板服务。';}
 finally{state.busy=false;$('refresh').disabled=false;render();}
}
$('latest-view').addEventListener('click',()=>{if(state.latest)select(state.latest.run_id);});
$('refresh').addEventListener('click',refresh);$('search').addEventListener('input',render);
for(const b of document.querySelectorAll('[data-filter]'))b.addEventListener('click',()=>{state.filter=b.dataset.filter;for(const x of document.querySelectorAll('[data-filter]')){x.classList.toggle('selected',x===b);x.setAttribute('aria-pressed',String(x===b));}render();});
$('prev').addEventListener('click',()=>{state.offset=Math.max(0,state.offset-30);refresh();});$('next').addEventListener('click',()=>{state.offset+=30;refresh();});
$('close').addEventListener('click',()=>{state.selected=null;$('detail').hidden=true;});
refresh();setInterval(()=>{if(!document.hidden)refresh();},10000);
