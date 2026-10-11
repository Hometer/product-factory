"use client";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

type Execution = { id:string; task_id:string; revision:number; content_hash:string; payload:{ text:{files:string[]}; inputs:{task_title:string} } };
type FilePatch = {path:string; before:string|null; after:string; diff:string};
type Asset = { id:string; execution_id:string; content_hash:string; created_at:string; can_apply:boolean; blockers:string[]; payload:{ task_id:string; files:FilePatch[]; root:string; validation_plan:string; acceptance:Record<string,string>; required_checks:string[]; inputs:{baseline_revision:number;technical_revision:number;planning_revision:number;requirement_ids:string[]} } };
type Run = {id:string;patch_id:string;state:string;created_at:string;details:{message:string;completed:string[];reason:string}};
type Workspace = {assets:Asset[];runs:Run[]};
const states:Record<string,string> = {preparing:"准备备份",applying:"正在写入",applied:"已写入 · 仍需工程验证",failed:"未开始写入",rolled_back:"未完成 · 已恢复原文",rollback_required:"恢复受阻 · 需人工核对"};
async function api<T>(route:string, body?:unknown, key?:string):Promise<T> {
  const response=await fetch("/api/factory/"+route,body===undefined?{cache:"no-store"}:{method:"POST",headers:{"Content-Type":"application/json","Idempotency-Key":key??crypto.randomUUID()},body:JSON.stringify(body)});
  const data=await response.json();
  if(!response.ok)throw new Error(data.error?.message??"操作未完成，请查询记录后重试");
  return data as T;
}

export default function PatchEditor({projectId}:{projectId:string}) {
  const base="projects/"+projectId;
  const [executions,setExecutions]=useState<Execution[]>([]),[workspace,setWorkspace]=useState<Workspace>({assets:[],runs:[]});
  const [selected,setSelected]=useState(""),[files,setFiles]=useState<{path:string;content:string}[]>([]),[validation,setValidation]=useState("");
  const [preview,setPreview]=useState<Asset|null>(null),[dirty,setDirty]=useState(false),[confirmed,setConfirmed]=useState(false),[reason,setReason]=useState("");
  const [busy,setBusy]=useState(false),[message,setMessage]=useState(""),[error,setError]=useState("");
  const keys=useRef<Record<string,{fingerprint:string;key:string}>>({});
  const current=executions.find(e=>e.id===selected);
  function requestKey(action:string,body:unknown){const fingerprint=JSON.stringify(body);if(keys.current[action]?.fingerprint!==fingerprint)keys.current[action]={fingerprint,key:crypto.randomUUID()};return keys.current[action].key;}
  const load=useCallback(async()=>{
    const [exec,w]=await Promise.all([api<{entries:{plan:Execution}[]}>(base+"/execution"),api<Workspace>(base+"/patches")]);
    setExecutions(exec.entries.map(e=>e.plan));setWorkspace(w);
    setPreview(old=>old?w.assets.find(a=>a.id===old.id)??old:old);
  },[base]);
  useEffect(()=>{let active=true;Promise.all([api<{entries:{plan:Execution}[]}>(base+"/execution"),api<Workspace>(base+"/patches")]).then(([exec,w])=>{if(active){setExecutions(exec.entries.map(e=>e.plan));setWorkspace(w);}}).catch(e=>{if(active)setError(e.message);});return()=>{active=false;};},[base]);
  useEffect(()=>{const guard=(event:BeforeUnloadEvent)=>{if(dirty||busy){event.preventDefault();event.returnValue="";}};window.addEventListener("beforeunload",guard);return()=>window.removeEventListener("beforeunload",guard);},[dirty,busy]);
  function leave(event:React.MouseEvent<HTMLAnchorElement>){if((dirty||busy)&&!window.confirm("候选尚未保存或操作未结束，确定离开吗？"))event.preventDefault();}
  function edit(){setDirty(true);setConfirmed(false);}
  function choose(id:string){if(dirty&&!window.confirm("候选尚未保存，确定切换任务吗？"))return;setSelected(id);setFiles([]);setPreview(null);setValidation("");setReason("");setDirty(false);setConfirmed(false);}
  async function act(action:()=>Promise<void>){setBusy(true);setError("");setMessage("");try{await action();}catch(e){setError(e instanceof Error?e.message:"操作未完成，输入保留；查询运行后重试");}finally{setBusy(false);}}
  async function save(){if(!current)return;const body={execution_id:current.id,expected_hash:current.content_hash,files,validation_plan:validation};const asset=await api<Asset>(base+"/patches/preview",body,requestKey("preview",body));setPreview(asset);setDirty(false);setConfirmed(false);await load();setMessage("补丁预览已保存，尚未写入项目文件。请逐项核对差异。");}
  async function apply(){if(!preview)return;const body={patch_id:preview.id,expected_hash:preview.content_hash,confirmed:true,reason};const run=await api<Run>(base+"/patches/apply",body,requestKey("apply",body));setConfirmed(false);await load();setMessage(states[run.state]+"。"+run.details.message);}
  function view(asset:Asset){if(dirty&&!window.confirm("候选尚未保存，确定查看历史吗？"))return;setPreview(asset);setSelected(asset.execution_id);setFiles(asset.payload.files.map(f=>({path:f.path,content:f.after})));setValidation(asset.payload.validation_plan);setDirty(false);setConfirmed(false);setReason("");}
  async function recover(run:Run){if(!window.confirm("仅恢复仍匹配本次原文或候选的文件；外部第三方内容会保留。确认重新核对恢复？"))return;const body={run_id:run.id,confirmed:true};const result=await api<Run>(base+"/patches/recover",body,requestKey("recover",body));await load();setMessage(states[result.state]+"。"+result.details.message);}
  return <div className="shell"><header className="topbar"><Link className="brand" href="/" onClick={leave}><span className="brandmark">工</span>LaunchFlow</Link><Link href={"/projects/"+projectId+"/execution"} onClick={leave}>← 执行计划与授权</Link></header><main>
    <section className="intro"><div><span className="eyebrow">文件变更 · FR09</span><h1>先看差异，再确认写入</h1><p>人工候选 · 本地零费用 · 保留来源与恢复记录</p></div><div className="scope">仅开放已授权的有限文本写入。数据库迁移与命令执行待后续阶段。</div></section>
    {error&&<div role="alert" className="message error">{error} · 当前输入保留</div>}{message&&<div role="status" className="message success">{message}</div>}
    <div className="requirement-grid"><section className="panel requirement-panel"><h2>准备候选文件</h2><p className="hint">只处理当前执行计划显式文件；最多 5 份 UTF-8 文本，父目录须已存在。预览不会写入原工程。</p>
      <fieldset disabled={busy}><label htmlFor="patch-task">执行任务</label><select id="patch-task" value={selected} onChange={e=>choose(e.target.value)}><option value="">选择已保存的执行计划</option>{executions.map(e=><option key={e.id} value={e.id}>{e.task_id} · {e.payload.inputs.task_title} · 修订 {e.revision}</option>)}</select>
      {current&&<><p className="path">可选文件：{current.payload.text.files.join("、")||"尚未选择，请回到执行页保存文件范围"}</p>{files.map((file,i)=><div className="requirement-item" key={i}><label htmlFor={"path-"+i}>文件 {i+1}</label><select id={"path-"+i} value={file.path} onChange={e=>{setFiles(files.map((f,n)=>n===i?{...f,path:e.target.value}:f));edit();}}>{current.payload.text.files.map(path=><option key={path}>{path}</option>)}</select><label htmlFor={"candidate-"+i}>候选完整文本</label><textarea id={"candidate-"+i} rows={7} value={file.content} onChange={e=>{setFiles(files.map((f,n)=>n===i?{...f,content:e.target.value}:f));edit();}} placeholder="人工填写希望写入的完整内容"/><button type="button" className="quiet" onClick={()=>{setFiles(files.filter((_,n)=>n!==i));edit();}}>移除此候选</button></div>)}<button className="quiet" disabled={files.length>=5||current.payload.text.files.every(p=>files.some(f=>f.path===p))} onClick={()=>{const path=current.payload.text.files.find(p=>!files.some(f=>f.path===p));if(path){setFiles([...files,{path,content:""}]);edit();}}}>添加候选文件</button></>}
      <label className="requirement-field" htmlFor="patch-validation">写入后如何验证</label><textarea id="patch-validation" rows={3} value={validation} onChange={e=>{setValidation(e.target.value);edit();}} placeholder="例如：检查页面行为，运行对应测试，重启后核对结果"/>
      <button className="primary" disabled={!current||!files.length||!validation.trim()} onClick={()=>act(save)}>{busy?"正在处理…":"保存补丁预览"}</button></fieldset>
      {dirty&&<p className="warning">当前编辑尚未保存，不能应用旧预览。</p>}
    </section><aside className="panel requirement-panel"><h2>补丁与运行记录</h2><button className="quiet" disabled={busy} onClick={()=>act(load)}>刷新当前检查</button><p className="hint">来源过期、授权撤销或外部变化会阻止写入。运行记录表示当时结果，后续外部修改不会改变历史。</p>
      {workspace.assets.length===0&&<p>还没有补丁预览。</p>}{workspace.assets.map(a=><button className="baseline-link" disabled={busy} key={a.id} onClick={()=>view(a)}>{a.payload.task_id} · {a.payload.files.map(f=>f.path).join("、")}<br/><small>{new Date(a.created_at).toLocaleString("zh-CN")}</small></button>)}
      {workspace.runs.map(r=><div className="freeze-review" key={r.id}><strong>{states[r.state]}</strong><p>{r.details.message}</p><p className="path">运行 {r.id}<br/>补丁 {r.patch_id}</p><p>完成步骤：{r.details.completed.join("、")||"无"}</p>{r.state==="rollback_required"&&<button className="quiet" disabled={busy} onClick={()=>act(()=>recover(r))}>重新核对安全恢复</button>}</div>)}
    </aside></div>
    {preview&&<section className="panel model-panel" aria-label="补丁差异预览"><h2>具体补丁 · {preview.payload.task_id}</h2><p className="path">目标目录：{preview.payload.root}<br/>补丁 {preview.id} · 指纹 {preview.content_hash.slice(0,12)}</p><p>人工输入；需求基线 {preview.payload.inputs.baseline_revision} / 方案 {preview.payload.inputs.technical_revision} / 阶段计划 {preview.payload.inputs.planning_revision}；关联需求 {preview.payload.inputs.requirement_ids.join("、")||"基础设施/缺陷"}</p>
      <h3>原验收要求</h3>{Object.entries(preview.payload.acceptance).map(([id,value])=><p key={id}>{id}：{value}</p>)}<h3>验证计划</h3><p>{preview.payload.validation_plan}</p><details><summary>保留 {preview.payload.required_checks.length} 项必需检查</summary>{preview.payload.required_checks.map((c,i)=><p key={i}>{c}</p>)}</details>
      {preview.payload.files.map(f=><div className="gap" key={f.path}><h3>{f.path} · {f.before===null?"新增":"替换"}</h3><pre className="patch-diff">{f.diff}</pre><details><summary>查看完整原文与候选（包括末尾换行）</summary><p>原文：{f.before===null?"文件原先不存在":JSON.stringify(f.before)}</p><p>候选：{JSON.stringify(f.after)}</p></details></div>)}
      {preview.blockers.map((b,i)=><p className="warning" key={i}>阻塞：{b}</p>)}<p className="hint">写入前重新核对。多文件失败尝试恢复原文；外部冲突会停止并保留内容。写入成功仍需验证与阶段验收。</p>
      <fieldset disabled={busy}><label className="choice"><input type="checkbox" checked={confirmed} disabled={dirty||!preview.can_apply||preview.execution_id!==current?.id} onChange={e=>setConfirmed(e.target.checked)}/>我已核对以上完整差异、范围和验证计划，确认应用此补丁</label><label htmlFor="patch-reason">确认依据</label><input id="patch-reason" value={reason} onChange={e=>setReason(e.target.value)} placeholder="例如：仅应用已审阅的演示文本变化"/><button className="primary" disabled={dirty||!preview.can_apply||!confirmed||reason.trim().length<4||preview.execution_id!==current?.id} onClick={()=>act(apply)}>确认并写入此补丁</button></fieldset>
    </section>}
    <footer>本机保存 · 无模型调用或外发 · 自动任务与命令执行尚未开放</footer>
  </main></div>;
}
