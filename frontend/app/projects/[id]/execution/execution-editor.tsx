"use client";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

type Step = { id: string; operation: string; instruction: string; output: string; validation: string };
type Text = { modifications: string; files: string[]; steps: Step[]; estimate_micro: number; recovery: string; risks: string };
type Check = { key: string; label: string; status: string; result: string; method: string; guidance: string };
type Inputs = { baseline_revision: number; technical_revision: number; planning_revision: number; stage_id: string; task_title: string; requirement_ids: string[]; task_dependencies: string[]; stage_dependencies: string[]; version_dependencies: string[]; environment_id: string };
type Asset = { id: string; task_id: string; kind: string; revision: number; content_hash: string; created_at: string; payload: { text?: Text; inputs?: Inputs; files?: { path: string; exists: boolean; size: number; sha256: string | null }[]; acceptance?: Record<string,string>; required_checks?: string[]; checks?: Check[]; limitations?: string[]; checked_at?: string; execution_id?: string; authorization_id?: string | null; reason?: string; scope?: string; authorization_hash?: string; content_hash?: string; budget?: { spent_micro: number; reserved_micro: number; requests_used: number; requests_reserved: number }; source?: string } };
type Entry = { task_id: string; plan: Asset; check: Asset | null; authorization: Asset | null };
type Workspace = { planning_id: string | null; planning_revision: number | null; tasks: { id: string; title: string; stage_id: string }[]; source_issues: string[]; entries: Entry[]; history: Pick<Asset,"id"|"task_id"|"kind"|"revision"|"created_at">[] };
const names: Record<string,string> = { plan:"执行计划",check:"开工检查",grant:"本地范围授权",revoke:"撤销记录" };
const operations: Record<string,string> = { read_only:"本地只读",code_write:"代码写入",local_command:"本地受控配方",model_call:"模型外发（未开放）",dependency_install:"依赖安装（未开放）",deploy:"发布（未开放）",irreversible:"不可逆动作（未开放）" };
const yuan = (micro: number) => (micro/1_000_000).toFixed(6).replace(/0+$/,"").replace(/\.$/,"");
class APIError extends Error { constructor(message: string, public code: string) { super(message); } }
async function api<T>(route: string, body?: unknown, key?: string): Promise<T> {
  const response = await fetch("/api/factory/" + route, body === undefined ? {cache:"no-store"} : {method:"POST",headers:{"Content-Type":"application/json","Idempotency-Key":key ?? crypto.randomUUID()},body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new APIError(data.error?.message ?? "操作未完成",data.error?.code ?? "REQUEST_FAILED");
  return data as T;
}
function Snapshot({asset}: {asset: Asset}) {
  const p = asset.payload;
  return <div className="freeze-review"><h3>{names[asset.kind]} · {asset.task_id} · 修订 {asset.revision}</h3><p>{new Date(asset.created_at).toLocaleString("zh-CN")}</p>
    {p.inputs && <p>输入：需求 {p.inputs.baseline_revision} / 技术方案 {p.inputs.technical_revision} / 阶段计划 {p.inputs.planning_revision}；{p.inputs.stage_id} · {p.inputs.task_title}；需求 {p.inputs.requirement_ids.join("、") || "基础设施/缺陷"}；前置 {p.inputs.task_dependencies.concat(p.inputs.stage_dependencies,p.inputs.version_dependencies).join("、") || "无"}</p>}
    {p.text && <><p><strong>预期修改：</strong>{p.text.modifications}</p><p><strong>文件范围：</strong>{p.text.files.join("、") || "尚未选择"}</p>{p.files?.map(f => <p key={f.path}>{f.path} · {f.exists ? "已有文件，"+f.size+"字节，指纹 "+f.sha256?.slice(0,12) : "拟新增，尚未创建"}</p>)}{p.text.steps.map(s => <div className="gap" key={s.id}><strong>{s.id} · {operations[s.operation]}</strong><p>步骤：{s.instruction}</p><p>产物：{s.output}</p><p>验证：{s.validation}</p></div>)}<p><strong>费用估计：</strong>{yuan(p.text.estimate_micro)} 元（准备估算，未消费；外部操作还需可靠报价）</p><p><strong>恢复：</strong>{p.text.recovery}</p><p><strong>风险：</strong>{p.text.risks}</p></>}
    {p.acceptance && <><h4>基线原验收要求</h4>{Object.entries(p.acceptance).map(([id,value]) => <p key={id}>{id}：{value}</p>)}</>}{p.required_checks && <><h4>保留的阶段闸门</h4><ol>{p.required_checks.map(c => <li key={c}>{c}</li>)}</ol></>}
    {p.reason && <p>说明：{p.reason} · 本机操作者</p>}
    {p.execution_id && <p>来源执行草稿：{p.execution_id}</p>}
    {p.authorization_id && <p>关联授权：{p.authorization_id}</p>}
    {p.scope && <p>范围：仅本地零费用；不包含实际执行、外发或发布。</p>}
    {p.checks && <><p className="warning">这是当时的检查快照；修改、撤销、外部变化或来源过期后必须重新检查。自动执行未开放。</p>{p.checks.map(c => <div className="gap" key={c.key}><strong>{c.label} · {{passed:"已核对",blocked:"阻塞",pending:"待验",info:"信息"}[c.status]}</strong><p>{c.result}</p><small>方法：{c.method}</small>{c.guidance && c.status !== "passed" && <p className="warning">解除方式：{c.guidance}</p>}</div>)}</>}
    {p.budget && <p>阶段模型预算事实：已结算 {yuan(p.budget.spent_micro)} 元 / 预留 {yuan(p.budget.reserved_micro)} 元；已用 {p.budget.requests_used} 次 / 预留 {p.budget.requests_reserved} 次。上限 100 元 / 100 次，单任务 2 元；本轮检查不预留或清零。</p>}
    {p.limitations?.map(l => <p className="hint" key={l}>{l}</p>)}
  </div>;
}
export default function ExecutionEditor({projectId}: {projectId: string}) {
  const base = "projects/"+projectId+"/execution";
  const [workspace,setWorkspace] = useState<Workspace|null>(null);
  const [name,setName] = useState("");
  const [taskId,setTaskId] = useState("");
  const [text,setText] = useState<Text|null>(null);
  const [fileInput,setFileInput] = useState("");
  const [money,setMoney] = useState("0");
  const [dirty,setDirty] = useState(false);
  const [busy,setBusy] = useState(false);
  const [error,setError] = useState("");
  const [notice,setNotice] = useState("");
  const [review,setReview] = useState(false);
  const [reason,setReason] = useState("");
  const [historical,setHistorical] = useState<Asset|null>(null);
  const mounted = useRef(true);
  const request = useRef<{body:string;key:string}|null>(null);
  const entry = workspace?.entries.find(e => e.task_id === taskId);
  const row = entry?.plan;
  const select = useCallback((state:Workspace,id:string) => {
    const plan=state.entries.find(e => e.task_id === id)?.plan.payload.text ?? null;
    setTaskId(id);setText(plan);setFileInput(plan?.files.join("\n") ?? "");setMoney(yuan(plan?.estimate_micro ?? 0));setDirty(false);setReview(false);
  },[]);
  const load = useCallback(async (replace:boolean,id?:string) => {
    const state=await api<Workspace>(base);
    if (mounted.current) { setWorkspace(state);if(replace) select(state,id ?? state.tasks[0]?.id ?? state.entries[0]?.task_id ?? ""); }
    return state;
  },[base,select]);
  useEffect(() => {
    mounted.current=true;
    void Promise.all([api<{name:string}>("projects/"+projectId),api<Workspace>(base)]).then(([p,state]) => {if(mounted.current){setName(p.name);setWorkspace(state);select(state,state.tasks[0]?.id ?? state.entries[0]?.task_id ?? "");}}).catch(e => {if(mounted.current)setError((e as Error).message);});
    return () => {mounted.current=false;};
  },[projectId,base,select]);
  useEffect(() => {
    const before=(e:BeforeUnloadEvent) => {if(dirty || busy){e.preventDefault();e.returnValue="";}};
    window.addEventListener("beforeunload",before);return () => window.removeEventListener("beforeunload",before);
  },[dirty,busy]);
  function leave(e:React.MouseEvent<HTMLAnchorElement>) {if((dirty||busy) && !window.confirm("执行输入尚未保存，确定离开吗？"))e.preventDefault();}
  function change(patch:Partial<Text>) {setText(t=>t && ({...t,...patch}));setDirty(true);setReview(false);setNotice("");}
  async function action(work:()=>Promise<void>) {
    setBusy(true);setError("");
    try {await work();} catch(e){if(mounted.current)setError((e as Error).message);}
    finally {if(mounted.current)setBusy(false);}
  }
  const ready=!!workspace && !busy && !dirty;
  const costValid=/^\d{1,3}(\.\d{1,6})?$/.test(money) && Number(money)<=100;
  const complete=!!text && costValid && [text.modifications,text.recovery,text.risks].every(s=>s.trim().length>=3) && text.steps.length>0 && text.steps.every(s=>[s.instruction,s.output,s.validation].every(v=>v.trim().length>=3));
  const localScope=!!text && text.estimate_micro===0 && text.steps.every(s=>["read_only","code_write","local_command"].includes(s.operation)) && (!text.steps.some(s=>["code_write","local_command"].includes(s.operation)) || text.files.length>0);
  async function save() {
    if(!workspace || !text || !complete || !workspace.planning_id)return;
    const body={planning_id:workspace.planning_id,task_id:taskId,expected_revision:row?.revision ?? 0,text:{...text,files:fileInput.split("\n").map(s=>s.trim()).filter(Boolean),estimate_micro:Math.round(Number(money)*1_000_000)}};
    const encoded=JSON.stringify(body);
    if(request.current?.body!==encoded)request.current={body:encoded,key:crypto.randomUUID()};
    const saved=await api<Asset>(base+"/plans/revisions",body,request.current.key);
    const state=await load(false);
    if(state.entries.find(e=>e.task_id===taskId)?.plan.id!==saved.id)throw new APIError("保存后已有新的服务器修订，输入保留，请核对。","REVISION_CONFLICT");
    select(state,taskId);setNotice("执行草稿已保存；尚未执行，检查需重新运行。");
  }
  return <div className="shell"><header className="topbar"><Link className="brand" href="/" onClick={leave}><span className="brandmark">工</span>产品工厂</Link><Link href={"/projects/"+projectId+"/planning"} onClick={leave}>← 技术与阶段计划</Link></header><main><Link className="planning-entry" href={"/projects/"+projectId+"/commands"} onClick={leave}>命令执行与运行记录 →</Link><Link className="planning-entry" href={"/projects/"+projectId+"/patches"} onClick={leave}>补丁预览与安全写入 →</Link>
    <div className="intro"><div><p className="eyebrow">EXECUTION / PREFLIGHT</p><h1>{name || "项目"} · 开工检查</h1><p className="subtitle">把要改什么、怎样验证与恢复写清楚，再核对开工条件。</p></div><div className="scope"><strong>{busy ? "正在处理…" : dirty ? "输入尚未保存" : row ? "已保存 · "+taskId+" 执行修订 "+row.revision : "尚未生成执行草稿"}</strong><span>手动执行待核对 · 自动执行未开放</span></div></div>
    <p className="hint">范围授权只记录明确的本地零费操作；不启动任务、不修改项目文件、不调用模型、不代表阶段通过。</p>
    {error && <div className="message error" role="alert">{error}<button disabled={busy} onClick={()=>void action(async()=>{await load(!workspace);setNotice("服务器记录已读取，当前输入保留；核对后明确保存。");})}>重新核对服务器记录</button></div>}
    {notice && <p className="message success" role="status">{notice}</p>}
    {!workspace ? <section className="panel">正在读取执行准备记录。</section> : <>
    {workspace.source_issues.map((i,n)=><p className="warning" key={n}>来源阻塞：{i}。请回技术与计划页核对，旧记录仍可查看或撤销。</p>)}
    <section className="panel requirement-panel"><p className="eyebrow">01 / 选择任务</p><h2>来源阶段计划 {workspace.planning_revision ?? "未确认"}</h2><label htmlFor="execution-task">要核对的任务</label><select id="execution-task" disabled={busy} value={taskId} onChange={e=>{if(dirty && !window.confirm("当前输入尚未保存，确定切换任务吗？"))return;select(workspace,e.target.value);setHistorical(null);}}>
      {!workspace.tasks.length && !workspace.entries.length && <option value="">先确认阶段计划</option>}
      {workspace.tasks.map(t=><option key={t.id} value={t.id}>{t.id} · {t.title} · {t.stage_id}</option>)}
      {workspace.entries.filter(e=>!workspace.tasks.some(t=>t.id===e.task_id)).map(e=><option key={e.task_id} value={e.task_id}>{e.task_id} · 旧执行草稿（当前计划无此任务）</option>)}
    </select><button className="quiet" disabled={!ready || !taskId || workspace.source_issues.length>0 || !workspace.tasks.some(t=>t.id===taskId)} onClick={()=>void action(async()=>{await api(base+"/plans/generate",{planning_id:workspace.planning_id,task_id:taskId,expected_revision:row?.revision ?? 0});await load(true,taskId);setNotice("规则执行草稿已生成，必须核对；没有启动任务。");})}>生成规则执行草稿</button></section>
    {text && <section className="panel model-panel"><p className="eyebrow">02 / 执行计划</p><h2>预期修改与验证</h2><p className="source-note">来源：{row?.payload.source==="rule_template" ? "规则建议，待人工核对" : "用户编辑"}；输入需求 {row?.payload.inputs?.baseline_revision} / 方案 {row?.payload.inputs?.technical_revision} / 阶段计划 {row?.payload.inputs?.planning_revision}</p><fieldset disabled={busy}>
      <label htmlFor="execution-modifications">预期修改（具体影响）</label><textarea id="execution-modifications" maxLength={2000} value={text.modifications} onChange={e=>change({modifications:e.target.value})}/>
      <label htmlFor="execution-files">拟修改或核对文件（相对路径，每行一个，最多20个）</label><textarea id="execution-files" rows={3} maxLength={4020} value={fileInput} onChange={e=>{setFileInput(e.target.value);setDirty(true);setReview(false);}}/><p className="hint">只保存所选文件指纹；未选文件不在检查范围内。秘密、数据、依赖目录、链接文件及超限文件拒绝核对。拟新增文件不会创建。</p>
      <div className="section-head"><h3>执行步骤（仅描述，不运行命令）</h3><button className="quiet" disabled={text.steps.length>=20} onClick={()=>change({steps:[...text.steps,{id:"E"+(Math.max(...text.steps.map(s=>Number(s.id.slice(1))))+1),operation:"read_only",instruction:"",output:"",validation:""}]})}>＋ 添加步骤</button></div>
      {text.steps.map((s,i)=><div className="requirement-item" key={s.id}><h3>{s.id}</h3><label htmlFor={s.id+"-operation"}>操作类型</label><select id={s.id+"-operation"} value={s.operation} onChange={e=>change({steps:text.steps.map((v,n)=>n===i?{...v,operation:e.target.value}:v)})}>{Object.entries(operations).map(([k,v])=><option key={k} value={k}>{v}</option>)}</select>{([["instruction","步骤与具体操作"],["output","预期产物"],["validation","验证项"]] as const).map(([k,label])=><div key={k}><label htmlFor={s.id+"-"+k}>{label}</label><textarea id={s.id+"-"+k} rows={2} maxLength={1000} value={s[k]} onChange={e=>change({steps:text.steps.map((v,n)=>n===i?{...v,[k]:e.target.value}:v)})}/></div>)}</div>)}
      <label htmlFor="execution-cost">费用估计（元，最多6位小数；不是实际报价）</label><input id="execution-cost" inputMode="decimal" maxLength={10} value={money} onChange={e=>{setMoney(e.target.value);setDirty(true);setReview(false);}}/>
      <label htmlFor="execution-recovery">恢复方式（涉及数据时单列备份）</label><textarea id="execution-recovery" maxLength={2000} value={text.recovery} onChange={e=>change({recovery:e.target.value})}/>
      <label htmlFor="execution-risks">风险与限制</label><textarea id="execution-risks" maxLength={2000} value={text.risks} onChange={e=>change({risks:e.target.value})}/>
    </fieldset>{dirty && <p className="warning">输入尚未保存；核对后点击保存，不自动授权或执行。</p>}<button className="primary" disabled={busy || !dirty || !complete || workspace.source_issues.length>0} onClick={()=>void action(save)}>保存执行计划</button>
    {row && <details><summary>查看服务器已保存计划（输入冲突时用于核对）</summary><Snapshot asset={row}/></details>}</section>}
    {row && <div className="requirement-grid"><section className="panel requirement-panel"><p className="eyebrow">03 / 范围授权</p><h2>本地零费操作范围</h2>{entry?.authorization ? <><p className="success">范围记录匹配当前草稿；来源、文件和执行条件仍需重新检查。</p><p>授权来源执行草稿：{entry.authorization.payload.execution_id}；修改范围没变时只改验证措辞无需重新授权。</p><label htmlFor="execution-revoke-reason">撤销说明</label><textarea id="execution-revoke-reason" maxLength={500} value={reason} onChange={e=>setReason(e.target.value)}/><button className="quiet" disabled={!ready || reason.trim().length<3} onClick={()=>void action(async()=>{await api(base+"/revoke",{authorization_id:entry.authorization?.id,reason});await load(true,taskId);setReason("");setNotice("范围授权已撤销，历史记录保留。");})}>撤销本地范围授权</button></> : <><p className="hint">尚无匹配且未撤销的本地范围授权。只读准备可直接检查；代码修改需明确文件后核对范围。</p><button className="quiet" disabled={!ready || !localScope || workspace.source_issues.length>0} onClick={()=>{setReview(true);setReason("");}}>核对本地范围授权</button>{!localScope && <p className="warning">仅开放本地零费范围确认；代码写入须有文件列表。外发、安装、发布、不可逆或付费描述不能取得本轮执行授权。</p>}</>}
    </section><section className="panel requirement-panel"><p className="eyebrow">04 / 开工条件</p><h2>检查与阻塞原因</h2><p className="warning">执行器及写锁未交付，自动执行始终不可用；检查不代表已运行或验收。</p><button className="primary" disabled={!ready} onClick={()=>void action(async()=>{await api(base+"/check",{execution_id:row.id,expected_hash:row.content_hash});await load(true,taskId);setNotice("检查已持久化；请查看阻塞和待验，未启动执行。");})}>运行开工检查</button><button disabled>自动执行未开放</button>{entry?.check && <><p className="source-note">{entry.check.payload.execution_id===row.id ? "对应当前执行草稿的历史检查" : "旧执行草稿的历史检查，需重新检查"}</p><Snapshot asset={entry.check}/></>}</section></div>}
    {review && row && ready && <section className="panel model-panel"><h2>确认前核对：本地操作范围</h2><p className="warning">只确认以下本地零费范围，不开始执行、不授权模型外发或发布。描述、文件、费用或风险变化须重新核对。</p><Snapshot asset={row}/><label htmlFor="execution-authorize-reason">确认说明</label><textarea id="execution-authorize-reason" maxLength={500} value={reason} onChange={e=>setReason(e.target.value)}/><button className="primary" disabled={reason.trim().length<3} onClick={()=>void action(async()=>{await api(base+"/authorize",{execution_id:row.id,expected_hash:row.content_hash,reason});await load(true,taskId);setReview(false);setReason("");setNotice("本地范围授权已记录；尚未执行，其他条件仍须检查。");})}>确认本地零费操作范围</button></section>}
    <section className="panel requirement-panel"><p className="eyebrow">05 / 历史</p><h2>准备、检查与撤销记录</h2><select aria-label="查看执行准备历史" defaultValue="" disabled={busy} onChange={e=>{if(e.target.value)void action(async()=>setHistorical(await api<Asset>(base+"/assets/"+e.target.value)));}}><option value="">选择不可变历史记录</option>{workspace.history.map(h=><option key={h.id} value={h.id}>{h.task_id} · {names[h.kind]} · 修订 {h.revision}</option>)}</select>{historical && <Snapshot asset={historical}/>}</section>
    </>}<footer>先计划、再核对 · 当前只保存准备记录 · 不执行项目命令或写入文件</footer></main></div>;
}
