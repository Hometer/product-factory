"use client";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

type Check = { key: string; label: string; status: string; result: string; method: string; guidance: string };
type Technical = Record<string, string>;
type Version = { id: string; name: string; goal: string; depends_on: string[] };
type Stage = { id: string; version_id: string; name: string; scope: string; out_of_scope: string; deliverable: string; depends_on: string[]; parent_id: string | null };
type Task = { id: string; stage_id: string; title: string; kind: "requirement" | "defect" | "infrastructure"; requirement_ids: string[]; defect_ref: string; reason: string; priority: string; depends_on: string[] };
type Plan = { versions: Version[]; stages: Stage[]; tasks: Task[] };
type Asset = { id: string; kind: string; revision: number; baseline_id: string | null; content_hash: string; created_at: string; payload: { environment_id?: string; technical_id?: string; source?: string; text?: Technical | Plan; checks?: Check[]; limitations?: string[]; asset_id?: string; reason?: string; acceptance?: Record<string,string>; required_checks?: string[] } };
type Summary = Pick<Asset, "id" | "kind" | "revision" | "baseline_id" | "created_at">;
type Issue = { code: string; message: string; object_id: string };
type Workspace = { baselines: { id: string; revision: number; created_at: string }[]; environment: Asset | null; technical: Asset | null; plan: Asset | null; technical_approval: Asset | null; plan_approval: Asset | null; history: Summary[]; technical_issues: Issue[]; plan_issues: Issue[]; notices: string[]; required_checks: string[]; acceptance: Record<string, string>; handoff_ready: boolean; execution_supported: false };
const labels: Record<string, string> = { understanding: "需求理解", recommendation: "推荐实现方案", retained: "保留已有工程", modules: "按需模块", deviations: "模板偏离与理由", costs: "增量费用与预算", deferred: "暂缓项与触发时机", data_flow: "数据流与隐私", criteria: "通过条件", risks: "风险与限制" };
const names: Record<string, string> = { environment: "环境检查", technical: "技术方案", plan: "阶段计划", technical_approval: "方案确认", plan_approval: "计划确认" };
class APIError extends Error { constructor(message: string, public code: string) { super(message); } }
async function api<T>(route: string, body?: unknown, key?: string): Promise<T> {
  const response = await fetch("/api/factory/" + route, body === undefined ? { cache: "no-store" } : { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": key ?? crypto.randomUUID() }, body: JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok) throw new APIError(data.error?.message ?? "操作未完成", data.error?.code ?? "REQUEST_FAILED");
  return data as T;
}
const split = (value: string) => value.split(/[，,\s]+/).map(s => s.trim()).filter(Boolean);
const isPlan = (text: Technical | Plan): text is Plan => Array.isArray(text.versions);
function ReferencesInput({ id, value, onChange }: { id: string; value: string[]; onChange: (value: string[]) => void }) {
  const [input, setInput] = useState<string | null>(null);
  return <input id={id} maxLength={1700} value={input ?? value.join(",")} onChange={e => { setInput(e.target.value); onChange(split(e.target.value)); }} onBlur={() => setInput(null)}/>;
}
function Snapshot({ asset }: { asset: Asset }) {
  const text = asset.payload.text;
  return <div className="freeze-review"><h3>{names[asset.kind]} · 修订 {asset.revision}</h3><p>{new Date(asset.created_at).toLocaleString("zh-CN")}</p>
    {asset.payload.reason && <p>确认说明：{asset.payload.reason} · 本机操作者</p>}
    {asset.payload.acceptance && Object.entries(asset.payload.acceptance).map(([id,value]) => <p key={id}><strong>{id} 原验收</strong>：{value}</p>)}{asset.payload.required_checks && <p>每阶段闸门：{asset.payload.required_checks.join("、")}</p>}
    {asset.payload.asset_id && <p>绑定资产：{asset.payload.asset_id}</p>}
    {asset.payload.checks?.map(c => <div className="gap" key={c.key}><strong>{c.label}：{c.result}</strong><p>方法：{c.method}</p>{c.guidance && <small>{c.guidance}</small>}</div>)}
    {text && (isPlan(text) ? <>{text.versions.map(v => <p key={v.id}>{v.id} · {v.name}：{v.goal}；依赖 {v.depends_on.join("、") || "无"}</p>)}{text.stages.map(s => <div className="gap" key={s.id}><h4>{s.id} · {s.name} · {s.version_id}</h4><p>范围：{s.scope}</p><p>范围外：{s.out_of_scope}</p><p>交付：{s.deliverable}</p><p>依赖：{s.depends_on.join("、") || "无"}；父阶段 {s.parent_id || "无"}</p></div>)}{text.tasks.map(t => <div className="gap" key={t.id}><h4>{t.id} · {t.title}</h4><p>阶段 {t.stage_id} · {t.priority}；需求 {t.requirement_ids.join("、") || "无"}；依赖 {t.depends_on.join("、") || "无"}</p><p>{t.defect_ref || t.reason}</p></div>)}</> : Object.entries(labels).map(([k,label]) => <p key={k}><strong>{label}</strong>：{text[k]}</p>))}
  </div>;
}

export default function PlanningEditor({ projectId }: { projectId: string }) {
  const base = "projects/" + projectId + "/planning";
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [name, setName] = useState("");
  const [baselineId, setBaselineId] = useState("");
  const [environmentId, setEnvironmentId] = useState("");
  const [technical, setTechnical] = useState<Technical | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [dirty, setDirty] = useState<"technical" | "plan" | null>(null);
  const [saving, setSaving] = useState(false);
  const [busy, setBusy] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [review, setReview] = useState<"technical" | "plan" | null>(null);
  const [reason, setReason] = useState("已核对本轮方案与计划范围，执行及后续待验仍需独立检查");
  const [historical, setHistorical] = useState<Asset | null>(null);
  const mounted = useRef(true);
  const edits = useRef(0);
  const saveRequest = useRef<{ hash: string; key: string } | null>(null);

  const install = useCallback((state: Workspace, replace: boolean) => {
    setWorkspace(state);
    if (replace) {
      setBaselineId(state.technical?.baseline_id ?? state.baselines[0]?.id ?? "");
      setEnvironmentId(state.technical?.payload.environment_id ?? state.environment?.id ?? "");
      setTechnical(state.technical?.payload.text as Technical ?? null);
      setPlan(state.plan?.payload.text as Plan ?? null);
      setDirty(null); setConflict(false);
    }
  }, []);
  useEffect(() => {
    mounted.current = true;
    void Promise.all([api<{ name: string }>("projects/" + projectId), api<Workspace>(base)]).then(([p, state]) => {
      if (mounted.current) { setName(p.name); install(state, true); }
    }).catch(e => { if (mounted.current) setError((e as Error).message); });
    return () => { mounted.current = false; };
  }, [base, install, projectId]);
  function change(kind: "technical" | "plan") { edits.current++; setDirty(kind); setNotice(""); setReview(null); }
  async function refresh(replace = true) {
    const state = await api<Workspace>(base);
    if (mounted.current) install(state, replace);
    return state;
  }
  async function action(task: () => Promise<void>) {
    setBusy(true); setError("");
    try { await task(); } catch(e) { setError((e as Error).message); if ((e as APIError).code === "REVISION_CONFLICT") setConflict(true); }
    finally { if (mounted.current) setBusy(false); }
  }
  const completeTechnical = !!technical && Object.values(technical).every(s => s.trim().length >= 3);
  const completePlan = !!plan && plan.versions.every(v => v.name.trim() && v.goal.trim().length >= 3) && plan.stages.every(s => [s.name,s.scope,s.out_of_scope,s.deliverable].every(x => x.trim().length >= 3)) && plan.tasks.every(t => t.title.trim().length >= 3);
  const save = useCallback(async () => {
    if (!workspace || !dirty || busy || saving || dirty === "technical" && !completeTechnical || dirty === "plan" && !completePlan) return;
    const ticket = edits.current;
    const body = dirty === "technical" ? { expected_revision: workspace.technical?.revision ?? 0, baseline_id: baselineId, environment_id: environmentId, text: technical } : { expected_revision: workspace.plan?.revision ?? 0, technical_id: workspace.technical?.id, text: plan };
    const hash = JSON.stringify(body);
    if (saveRequest.current?.hash !== hash) saveRequest.current = { hash, key: crypto.randomUUID() };
    setSaving(true); setError("");
    try {
      const asset = await api<Asset>(base + "/" + (dirty === "technical" ? "technical" : "plans") + "/revisions", body, saveRequest.current.key);
      const state = await api<Workspace>(base);
      if (!mounted.current) return;
      install(state, false);
      const current = dirty === "technical" ? state.technical : state.plan;
      if (current?.id !== asset.id) { setConflict(true); setError("保存后已有其他新修订；输入保留，请核对服务器版本。"); return; }
      setConflict(false);
      if (ticket === edits.current) { install(state, true); setNotice("草稿已保存；新的修订需重新核对确认。"); }
    } catch(e) { if (mounted.current) { setError((e as Error).message); setConflict(true); } }
    finally { if (mounted.current) setSaving(false); }
  }, [workspace, dirty, busy, saving, completeTechnical, completePlan, baselineId, environmentId, technical, plan, base, install]);
  useEffect(() => {
    if (!dirty || busy || saving || conflict) return;
    const timer = setTimeout(() => { void save(); }, 800); return () => clearTimeout(timer);
  }, [dirty,busy,saving,conflict,save]);
  useEffect(() => {
    const before = (e: BeforeUnloadEvent) => { if (dirty || saving) { e.preventDefault(); e.returnValue = ""; } };
    window.addEventListener("beforeunload", before); return () => window.removeEventListener("beforeunload", before);
  }, [dirty,saving]);
  const ready = !!workspace && !dirty && !busy && !saving && !conflict;
  function leave(e: React.MouseEvent<HTMLAnchorElement>) { if ((dirty || saving) && !window.confirm("规划输入尚未保存，确定离开吗？")) e.preventDefault(); }
  function updateVersion(i: number, patch: Partial<Version>) { change("plan"); setPlan(p => p && ({ ...p, versions: p.versions.map((v,n) => n === i ? {...v,...patch} : v) })); }
  function updateStage(i: number, patch: Partial<Stage>) { change("plan"); setPlan(p => p && ({ ...p, stages: p.stages.map((v,n) => n === i ? {...v,...patch} : v) })); }
  function updateTask(i: number, patch: Partial<Task>) { change("plan"); setPlan(p => p && ({ ...p, tasks: p.tasks.map((v,n) => n === i ? {...v,...patch} : v) })); }

  return <div className="shell"><header className="topbar"><Link className="brand" href="/" onClick={leave}><span className="brandmark">工</span>产品工厂</Link><Link href={"/projects/" + projectId} onClick={leave}>← 需求与基线</Link></header><main><Link className="planning-entry" href={"/projects/"+projectId+"/context"} onClick={leave}>上下文与知识资产 →</Link><Link className="planning-entry" href={"/projects/"+projectId+"/verification"} onClick={leave}>工程验证 →</Link>
    <div className="intro"><div><p className="eyebrow">ADAPTATION / PLAN</p><h1>{name || "项目"} · 技术与计划</h1><p className="subtitle">先核对怎么做，再确定分几步、每一步如何验收。</p></div><div className="scope"><strong>{saving ? "保存中…" : dirty ? "当前规划尚未保存" : workspace ? workspace.technical || workspace.plan ? "已保存 · 方案 " + (workspace.technical?.revision ?? "—") + " / 计划 " + (workspace.plan?.revision ?? "—") : "尚未生成方案与计划" : "正在读取…"}</strong><span>当前仅准备与交接 · 不执行开发任务</span></div></div>
    <Link className="planning-entry" href={"/projects/" + projectId + "/execution"} onClick={leave}>执行计划与开工检查 →</Link>
    {error && <div role="alert" className="message error">{error}<button onClick={() => void action(async () => { const state = await refresh(false); if (!workspace) install(state,true); else { setConflict(true); setNotice("服务器记录已读取，当前输入保留。核对后明确保存。"); } })}>重新核对服务器记录</button></div>}
    {dirty && !(dirty === "technical" ? completeTechnical : completePlan) && <p role="status" className="hint">请补齐新增条目或方案正文；完整后自动保存，离开前需确认保存状态。</p>}
    {notice && <p role="status" className="message success">{notice}</p>}
    {!workspace ? <section className="panel">正在读取规划；失败时可重新读取。</section> : <>
    {workspace.notices.map((n,i) => <p className="hint" key={i}>{n}</p>)}
    {conflict && <div className="message error">自动保存已暂停，当前输入保留；请核对后明确保存。<details><summary>查看服务器规划</summary>{workspace.technical && <Snapshot asset={workspace.technical}/>} {workspace.plan && <Snapshot asset={workspace.plan}/>}</details></div>}
    <div className="requirement-grid"><div>
    <section className="panel requirement-panel"><div className="section-head"><div><p className="eyebrow">01 / 环境事实</p><h2>本机与项目环境</h2></div><button className="quiet" disabled={!ready} onClick={() => void action(async () => { const env = await api<Asset>(base + "/environment", {}); await refresh(false); if (!technical) setEnvironmentId(env.id); setNotice("环境已检查；新结果尚未自动替代已保存方案的来源。"); })}>检查本机与项目环境</button></div>
      {!workspace.environment && <p className="hint">尚未检查。不会安装依赖、执行仓库脚本或发送资料。</p>}
      {workspace.environment && <><p className="source-note">检查于 {new Date(workspace.environment.created_at).toLocaleString("zh-CN")}，确认有效期15分钟。</p>{workspace.environment.payload.checks?.map(c => <div className="gap" key={c.key}><strong>{c.label} · {{passed:"已核对",blocked:"阻塞",pending:"待交付",info:"状态"}[c.status] ?? c.status}</strong><p>{c.result}</p><small>方法：{c.method}</small>{c.status === "blocked" && <p className="warning">{c.guidance}</p>}</div>)}{workspace.environment.payload.limitations?.map(l => <p className="hint" key={l}>{l}</p>)}</>}
    </section>
    <section className="panel requirement-panel"><p className="eyebrow">02 / 方案准备</p><h2>技术适配声明</h2>
      <label htmlFor="plan-baseline">绑定的需求基线</label><select id="plan-baseline" disabled={busy || saving || dirty === "plan"} value={baselineId} onChange={e => { setBaselineId(e.target.value); if (technical) change("technical"); }}>{!workspace.baselines.length && <option value="">先在需求页冻结基线</option>}{workspace.baselines.map(b => <option key={b.id} value={b.id}>需求修订 {b.revision} · {new Date(b.created_at).toLocaleString("zh-CN")}</option>)}</select>
      <label htmlFor="plan-environment">环境来源</label><select id="plan-environment" disabled={busy || saving || dirty === "plan"} value={environmentId} onChange={e => { setEnvironmentId(e.target.value); if (technical) change("technical"); }}><option value="">先检查环境</option>{workspace.history.filter(h => h.kind === "environment").map(h => <option key={h.id} value={h.id}>环境检查 {h.revision} · {new Date(h.created_at).toLocaleString("zh-CN")}</option>)}</select>
      <button className="quiet" disabled={!ready || !baselineId || !environmentId} onClick={() => void action(async () => { await api(base + "/technical/generate", { expected_revision: workspace.technical?.revision ?? 0, baseline_id: baselineId, environment_id: environmentId }); await refresh(); setNotice("已生成规则建议，需核对并人工确认。"); })}>生成新的规则方案草稿</button>
      {technical && <><p className="source-note">方案修订 {workspace.technical?.revision} · 来源 {workspace.technical?.payload.source === "rule_template" ? "规则建议，待人工核对" : "用户编辑"} · {workspace.technical_approval && workspace.technical_issues.length === 0 ? "已确认本修订" : "待确认或需重新核对"}</p><fieldset disabled={busy || dirty === "plan"}>{Object.entries(labels).map(([k,label]) => <div className="requirement-field" key={k}><label htmlFor={"tech-" + k}>{label}</label><textarea id={"tech-" + k} rows={3} maxLength={k === "understanding" || k === "recommendation" ? 3000 : 2000} value={technical[k] ?? ""} onChange={e => { change("technical"); setTechnical(t => ({...t,[k]:e.target.value})); }}/></div>)}</fieldset><button className="quiet" disabled={saving || busy || dirty !== "technical" || !completeTechnical} onClick={() => void save()}>保存方案输入</button><button className="primary" disabled={!ready || workspace.technical_issues.length > 0 || !!workspace.technical_approval} onClick={() => setReview("technical")}>核对并确认方案</button></>}
    </section>
    </div><aside>
    <section className="panel requirement-panel"><p className="eyebrow">03 / 检查与交接</p><h2>{workspace.handoff_ready ? "计划已确认，可交接" : "准备中，仍需核对"}</h2><p className="hint">执行器未交付；方案与计划确认不启动任务、不代表阶段验收通过。</p>
      <h3>方案检查</h3>{workspace.technical_issues.length ? workspace.technical_issues.map((i,n) => <p className="warning" key={n}>{i.object_id && "[" + i.object_id + "] "}{i.message}</p>) : <p>当前来源检查无阻塞，业务方案仍须人工核对。</p>}
      <h3>计划检查</h3>{workspace.plan_issues.length ? workspace.plan_issues.map((i,n) => <p className="warning" key={n}>{i.object_id && "[" + i.object_id + "] "}{i.message}</p>) : <p>覆盖与依赖规则检查无阻塞。</p>}
      <h3>每阶段保留的十项闸门</h3><ol>{workspace.required_checks.map(c => <li key={c}>{c}</li>)}</ol><p className="hint">规划不删除必需验收项；不适用须在验收时说明依据与责任人。</p>
      <h3>基线原验收要求</h3>{Object.entries(workspace.acceptance).map(([id,text]) => <p key={id}><strong>{id}</strong>：{text}</p>)}
    </section>
    <section className="panel requirement-panel"><p className="eyebrow">04 / 历史资产</p><h2>修订与人工确认</h2><select aria-label="查看规划历史" defaultValue="" disabled={busy} onChange={e => { if (e.target.value) void action(async () => { setHistorical(await api<Asset>(base + "/assets/" + e.target.value)); }); }}><option value="">选择历史快照</option>{workspace.history.map(h => <option key={h.id} value={h.id}>{names[h.kind]} · 修订 {h.revision}</option>)}</select>{historical && <Snapshot asset={historical}/>}</section>
    </aside>
    <section className="panel model-panel"><div className="section-head"><div><p className="eyebrow">05 / 版本与阶段</p><h2>开发阶段计划</h2></div><button className="quiet" disabled={!ready || !workspace.technical_approval || workspace.technical_issues.length > 0} onClick={() => void action(async () => { await api(base + "/plans/generate", {expected_revision:workspace.plan?.revision ?? 0,technical_id:workspace.technical?.id}); await refresh(); setNotice("已按基线版本和优先级生成规则计划，尚未执行。"); })}>生成新的规则阶段计划</button></div>
      {!plan && <p className="hint">确认技术方案后生成；任务必须关联基线需求，或注明缺陷/基础设施理由。</p>}
      {plan && <><p className="source-note">计划修订 {workspace.plan?.revision} · {workspace.plan?.payload.source === "rule_template" ? "规则建议" : "用户编辑"} · {workspace.plan_approval && workspace.plan_issues.length === 0 ? "已确认本修订" : "待确认或需重新核对"}</p><fieldset disabled={busy || dirty === "technical"}>
      <h3>版本</h3><button className="quiet" disabled={plan.versions.length >= 100} onClick={() => { change("plan"); setPlan(p => p && ({...p,versions:[...p.versions,{id:"V"+(Math.max(0,...p.versions.map(v=>Number(v.id.slice(1))))+1),name:"",goal:"",depends_on:[]}]})); }}>＋ 添加版本</button>{plan.versions.map((v,i) => <div className="requirement-item" key={v.id}><h3>{v.id}</h3><label htmlFor={v.id + "-name"}>版本名称（与基线目标版本一致）</label><input id={v.id + "-name"} maxLength={60} value={v.name} onChange={e => updateVersion(i,{name:e.target.value})}/><label htmlFor={v.id + "-goal"}>交付目标</label><textarea id={v.id + "-goal"} maxLength={1000} value={v.goal} onChange={e => updateVersion(i,{goal:e.target.value})}/><label htmlFor={v.id + "-deps"}>依赖版本（编号，逗号分隔）</label><ReferencesInput id={v.id + "-deps"} value={v.depends_on} onChange={value => updateVersion(i,{depends_on:value})}/></div>)}
      <h3>阶段与子阶段</h3><button className="quiet" disabled={plan.stages.length >= 200} onClick={() => { change("plan"); setPlan(p => p && ({...p,stages:[...p.stages,{id:"S"+(Math.max(0,...p.stages.map(s=>Number(s.id.slice(1))))+1),version_id:p.versions[0]?.id ?? "",name:"",scope:"",out_of_scope:"",deliverable:"",depends_on:[],parent_id:null}]})); }}>＋ 添加阶段或子阶段</button>{plan.stages.map((s,i) => <div className="requirement-item" key={s.id}><h3>{s.id} · {s.name}</h3>{([["name","阶段名称",100],["version_id","所属版本编号",16],["scope","范围内事项",2000],["out_of_scope","范围外事项",1000],["deliverable","交付物与目标",1000]] as const).map(([k,label,limit]) => <div key={k}><label htmlFor={s.id + "-" + k}>{label}</label><textarea id={s.id + "-" + k} rows={2} maxLength={limit} value={s[k]} onChange={e => updateStage(i,{[k]:e.target.value})}/></div>)}<label htmlFor={s.id + "-deps"}>依赖阶段（编号，逗号分隔）</label><ReferencesInput id={s.id + "-deps"} value={s.depends_on} onChange={value => updateStage(i,{depends_on:value})}/><label htmlFor={s.id + "-parent"}>父阶段编号（无则留空）</label><input id={s.id + "-parent"} maxLength={16} value={s.parent_id ?? ""} onChange={e => updateStage(i,{parent_id:e.target.value || null})}/></div>)}
      <h3>任务与需求关联</h3><button className="quiet" disabled={plan.tasks.length >= 300} onClick={() => { change("plan"); setPlan(p => p && ({...p,tasks:[...p.tasks,{id:"T"+(Math.max(0,...p.tasks.map(t=>Number(t.id.slice(1))))+1),stage_id:p.stages[0]?.id ?? "",title:"",kind:"requirement",requirement_ids:[],defect_ref:"",reason:"",priority:"P0",depends_on:[]}]})); }}>＋ 添加任务</button>{plan.tasks.map((t,i) => <div className="requirement-item" key={t.id}><h3>{t.id}</h3><label htmlFor={t.id + "-title"}>任务标题</label><input id={t.id + "-title"} maxLength={200} value={t.title} onChange={e => updateTask(i,{title:e.target.value})}/><div className="planning-input-grid"><div><label htmlFor={t.id + "-stage"}>所属阶段</label><input id={t.id + "-stage"} maxLength={16} value={t.stage_id} onChange={e => updateTask(i,{stage_id:e.target.value})}/></div><div><label htmlFor={t.id + "-priority"}>优先级</label><select id={t.id + "-priority"} value={t.priority} onChange={e => updateTask(i,{priority:e.target.value})}><option>P0</option><option>P1</option><option>P2</option></select></div></div><label htmlFor={t.id + "-kind"}>任务依据</label><select id={t.id + "-kind"} value={t.kind} onChange={e => updateTask(i,{kind:e.target.value as Task["kind"]})}><option value="requirement">基线需求</option><option value="defect">明确缺陷</option><option value="infrastructure">基础设施理由</option></select><label htmlFor={t.id + "-requirements"}>关联需求编号（逗号分隔）</label><ReferencesInput id={t.id + "-requirements"} value={t.requirement_ids} onChange={value => updateTask(i,{requirement_ids:value})}/>{t.kind !== "requirement" && <><label htmlFor={t.id + "-basis"}>{t.kind === "defect" ? "缺陷标识与定位" : "基础设施理由"}</label><textarea id={t.id + "-basis"} maxLength={t.kind === "defect" ? 160 : 1000} value={t.kind === "defect" ? t.defect_ref : t.reason} onChange={e => updateTask(i,t.kind === "defect" ? {defect_ref:e.target.value} : {reason:e.target.value})}/></>}<label htmlFor={t.id + "-deps"}>依赖任务（编号，逗号分隔）</label><ReferencesInput id={t.id + "-deps"} value={t.depends_on} onChange={value => updateTask(i,{depends_on:value})}/>{t.requirement_ids.map(rid => <p className="hint" key={rid}>{rid} 原验收：{workspace.acceptance[rid] ?? "未识别，必须先形成需求变更"}</p>)}</div>)}
      </fieldset><div className="button-row"><button className="quiet" disabled={saving || busy || dirty !== "plan" || !completePlan} onClick={() => void save()}>保存计划输入</button><button className="quiet" disabled={!ready} onClick={() => void action(async () => { await refresh(); setNotice("覆盖、依赖及来源已重新检查。"); })}>重新检查计划</button></div><button className="primary" disabled={!ready || workspace.plan_issues.length > 0 || !!workspace.plan_approval} onClick={() => setReview("plan")}>核对并确认计划</button></>}
    </section>
    {review && ready && <section className="panel model-panel"><h2>确认前核对：{review === "technical" ? "技术方案" : "阶段计划"}</h2><p className="warning">仅确认当前准备范围。执行、付费、外发及阶段验收仍需独立条件。</p>{(review === "technical" ? workspace.technical : workspace.plan) && <Snapshot asset={(review === "technical" ? workspace.technical : workspace.plan)!}/>}<label htmlFor="planning-confirm-reason">确认说明</label><textarea id="planning-confirm-reason" maxLength={500} value={reason} onChange={e => setReason(e.target.value)}/><button className="primary" disabled={reason.trim().length < 3} onClick={() => void action(async () => { const source = review === "technical" ? workspace.technical : workspace.plan; if (!source) return; await api(base + "/" + (review === "technical" ? "technical" : "plans") + "/confirm", {expected_revision:source.revision,expected_hash:source.content_hash,reason}); await refresh(); setReview(null); setNotice("当前修订已人工确认并保留快照；尚未开始执行或通过阶段验收。"); })}>确认当前{review === "technical" ? "方案" : "计划"}修订</button></section>}
    </div></>}
    <footer>本机规划资产可恢复 · 规则建议与环境事实分别展示 · 原项目文件保持原样</footer>
  </main></div>;
}
