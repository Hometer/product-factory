"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

const labels: Record<string, string> = { user_task: "目标用户与核心任务", input_output: "输入、过程和产物", workflow: "Agent 主链路", quality: "效果判断", priority: "交付阶段与优先级", model_cost: "模型与成本约束", data: "数据与非功能要求", launch: "上线与账号" };
const examples: Record<string, string> = { user_task: "谁在什么场景，完成什么任务？", input_output: "输入需求文本，过程中核对草稿，最终保存需求基线", workflow: "先整理草稿，人工确认后才能进入开发", quality: "好的结果是什么？不确定时可留待真实样例打分", priority: "第一版必须有什么，哪些可以后做？", model_cost: "模型厂商或限制，以及单次和阶段预算", data: "是否含个人信息或机密？保存、外发、大小与响应要求是什么？", launch: "仅本机试用，无云账号要求（可选不适用并写明依据）" };
type Field = { key: string; status: "known" | "unknown" | "not_applicable"; content: string; source: "user" | "project" | "ai_suggestion"; source_ref: string; confirmed_by: string };
type Item = { id: string; value: string; trigger: string; rule: string; exception: string; acceptance: string; priority: "P0" | "P1" | "P2"; target_version: string; source_ref: string };
type Gap = { id: string; message: string; severity: string; stage: string };
type Draft = { project_id: string; revision: number; fields: Field[]; items: Item[]; content_hash: string; saved_at: string | null; gaps: Gap[] };
type Round = { id: string; revision: number; version: string; questions: { id: string; question: string; recommendation: string; example: string; impact: string }[]; answered: boolean };
type Baseline = { id: string; revision: number; content_hash: string; fields: Field[]; items: Item[]; reason: string; created_at: string; deferred: Gap[] };
type Workspace = { revisions: number[]; draft: Draft; baselines: Baseline[]; round: Round | null; decisions: { id: string; question: string; conclusion: string; status: string; revision: number }[] };
type Model = { configured: boolean; model: string; base_url: string; quote_valid: boolean; quote_date: string; spent_micro: number; reserved_micro: number; requests_used: number; requests_reserved: number };
type Preview = { id: string; revision: number; content_hash: string; model: string; base_url: string; selected_fields: { key: string; content: string; status: string }[]; omitted_fields: string[]; max_attempts: number; reserve_micro: number; expires_at: string };
type Run = { id: string; authorization_id: string; revision: number; state: string; source: string; model: string; candidate: { summary: string; suggestions: { field: string; content: string; rationale: string; source_fields: string[] }[]; questions: { question: string; recommendation: string; example: string; impact: string }[] } | null; error: { code: string; message: string } | null; settled_micro: number; reserved_micro: number };
class APIError extends Error { constructor(message: string, public code: string) { super(message); } }
async function api<T>(route: string, body?: unknown, key?: string): Promise<T> {
  const response = await fetch(`/api/factory/${route}`, body === undefined ? { cache: "no-store" } : { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": key ?? crypto.randomUUID() }, body: JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok) throw new APIError(data.error?.message ?? "操作未完成", data.error?.code ?? "REQUEST_FAILED");
  return data as T;
}
function sourceLabel(field: Field) {
  if (field.source === "project") return "项目登记";
  if (field.source === "ai_suggestion") return "AI 建议，经你采用";
  const match = field.source_ref.match(/^draft:(\d+):/);
  return match ? `用户填写 · 草稿修订 ${match[1]}` : field.source_ref.startsWith("round:") ? field.confirmed_by ? "需求问答，经你确认" : "需求问答，仍待确认" : "用户填写";
}
const yuan = (micro: number) => (micro / 1_000_000).toFixed(4);

function RequirementSnapshot({ fields, items }: { fields: Field[]; items: Item[] }) {
  const statuses = { known: "已知", unknown: "不确定", not_applicable: "不适用" };
  return <>{fields.map(f => <div className="gap" key={f.key}><strong>{labels[f.key]} · {statuses[f.status]}</strong><p>{f.content || "未知"}</p><small>来源：{sourceLabel(f)}</small></div>)}{items.map(i => <div className="gap" key={i.id}><h4>{i.id} · {i.priority} · {i.target_version}</h4>{([['value','用户价值'],['trigger','触发条件'],['rule','业务规则'],['exception','异常处理'],['acceptance','验收方式']] as const).map(([key,label]) => <p key={key}><strong>{label}</strong>：{i[key]}</p>)}</div>)}</>;
}


export default function RequirementEditor({ projectId }: { projectId: string }) {
  const base = `projects/${projectId}`;
  const [name, setName] = useState("");
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [fields, setFields] = useState<Field[]>([]);
  const [items, setItems] = useState<Item[]>([]);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [busy, setBusy] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [round, setRound] = useState<Round | null>(null);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [freezePreview, setFreezePreview] = useState(false);
  const [reason, setReason] = useState("已核对本轮需求准备范围，后续待验继续保留");
  const [model, setModel] = useState<Model | null>(null);
  const [secret, setSecret] = useState("");
  const [baseUrl, setBaseUrl] = useState("https://dashscope.aliyuncs.com/compatible-mode/v1");
  const [selected, setSelected] = useState<string[]>([]);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [consent, setConsent] = useState(false);
  const [run, setRun] = useState<Run | null>(null);
  const [adoptFields, setAdoptFields] = useState<string[]>([]);
  const [viewBaseline, setViewBaseline] = useState<Baseline | null>(null);
  const [viewRevision, setViewRevision] = useState<Draft | null>(null);
  const edits = useRef(0);
  const mounted = useRef(true);
  const saveRequest = useRef<{ hash: string; key: string } | null>(null);

  useEffect(() => {
    mounted.current = true;
    void Promise.all([api<{ name: string }>(base), api<Workspace>(`${base}/requirements`), api<Model>("model/status").catch(() => { if (mounted.current) setError("模型配置暂不可读，手动需求编辑仍可使用。"); return null; }), api<{ items: Run[] }>(`${base}/analysis/runs`).catch(() => { if (mounted.current) setError("分析历史暂不可读，请重新读取核对。"); return { items: [] }; })]).then(([project, state, config, runs]) => {
      if (!mounted.current) return;
      setName(project.name); setWorkspace(state); setFields(state.draft.fields); setItems(state.draft.items);
      setRound(state.round); setModel(config); if (config) setBaseUrl(config.base_url); setRun(runs.items[0] ?? null);
    }).catch(e => { if (mounted.current) setError((e as Error).message); });
    return () => { mounted.current = false; };
  }, [base]);

  const validInput = fields.every(f => f.status === "unknown" || f.content.trim().length >= 3) && items.every(i => [i.value, i.trigger, i.rule, i.exception, i.acceptance].every(v => v.trim().length >= 3) && i.target_version.trim().length > 0);
  const save = useCallback(async () => {
    if (!workspace || saving || busy || !validInput) return;
    const ticket = edits.current;
    const body = { expected_revision: workspace.draft.revision, fields, items };
    const hash = JSON.stringify(body);
    if (saveRequest.current?.hash !== hash) saveRequest.current = { hash, key: crypto.randomUUID() };
    setSaving(true); setError("");
    try {
      const draft = await api<Draft>(`${base}/requirements/revisions`, body, saveRequest.current.key);
      const state = await api<Workspace>(`${base}/requirements`);
      if (!mounted.current) return;
      setWorkspace(state); setRound(state.round);
      if (state.draft.revision !== draft.revision) { setDirty(true); setConflict(true); setError("保存后又有新修订；当前输入已保留，请与服务器版本核对。"); return; }
      setConflict(false);
      if (ticket === edits.current) { setDirty(false); setFields(draft.fields); setItems(draft.items); }
    } catch (e) {
      if (!mounted.current) return;
      setError((e as Error).message); setConflict(true);
    } finally { if (mounted.current) setSaving(false); }
  }, [workspace, saving, busy, fields, items, base, validInput]);

  useEffect(() => {
    if (!dirty || saving || busy || conflict || !validInput) return;
    const timer = setTimeout(() => { void save(); }, 800);
    return () => clearTimeout(timer);
  }, [dirty, saving, busy, conflict, save, validInput]);

  useEffect(() => {
    const before = (event: BeforeUnloadEvent) => { if (dirty || saving) { event.preventDefault(); event.returnValue = ""; } };
    window.addEventListener("beforeunload", before);
    return () => window.removeEventListener("beforeunload", before);
  }, [dirty, saving]);

  useEffect(() => {
    if (!run || !["queued", "running"].includes(run.state)) return;
    let active = true;
    const timer = setInterval(() => {
      void api<Run>(`${base}/analysis/runs/${run.id}`).then(data => {
        if (!mounted.current || !active) return;
        setRun(data);
        if (!["queued", "running"].includes(data.state)) void api<Model>("model/status").then(status => { if (mounted.current) setModel(status); }).catch(() => { if (mounted.current) setError("阶段费用账本暂不可读，请重新核对。"); });
      }).catch(e => { if (mounted.current && active) setError((e as Error).message); });
    }, 1500);
    return () => { active = false; clearInterval(timer); };
  }, [run, base]);

  function changed() { edits.current += 1; setDirty(true); setPreview(null); setConsent(false); setFreezePreview(false); setNotice(""); }
  function fieldChange(index: number, patch: Partial<Field>) { changed(); setFields(current => current.map((f, i) => i === index ? { ...f, ...patch } : f)); }
  function itemChange(index: number, patch: Partial<Item>) { changed(); setItems(current => current.map((f, i) => i === index ? { ...f, ...patch } : f)); }
  async function updateWorkspace(replaceInput = true) {
    const state = await api<Workspace>(`${base}/requirements`); setWorkspace(state); setRound(state.round);
    if (replaceInput) { setFields(state.draft.fields); setItems(state.draft.items); setDirty(false); setConflict(false); }
    else { edits.current += 1; setDirty(true); setConflict(true); }
    return state;
  }
  async function action(task: () => Promise<void>) { setBusy(true); setError(""); try { await task(); } catch (e) { setError((e as Error).message); if ((e as APIError).code === "REVISION_CONFLICT") setConflict(true); } finally { setBusy(false); } }
  const ready = !!workspace && !dirty && !saving && !busy && !conflict;
  const blocked = workspace?.draft.gaps.some(g => g.severity === "blocking") ?? true;

  return <div className="shell">
    <header className="topbar"><Link className="brand" href="/" onClick={e => { if (dirty && !window.confirm("需求尚未保存，确定离开吗？")) e.preventDefault(); }}><span className="brandmark">工</span>产品工厂</Link><span className="local">本机工作台</span></header>
    <main><Link className="planning-entry" href={`/projects/${projectId}/evaluation`} onClick={e=>{if((dirty||saving||secret)&&!window.confirm("需求输入尚未保存，确定离开吗？"))e.preventDefault();}}>真实样例与效果评测 →</Link><Link className="planning-entry" href={"/projects/" + projectId + "/planning"} onClick={e => { if ((dirty || saving) && !window.confirm("需求尚未保存，确定离开吗？")) e.preventDefault(); }}>技术方案与阶段计划 →</Link>
      <div className="intro"><div><p className="eyebrow">REQUIREMENTS / 需求准备</p><h1>{name || "项目需求"}</h1><p className="subtitle">整理需求，核对缺口，由你确认本轮基线。</p></div><div className="scope"><strong>{saving ? "保存中…" : dirty ? "当前输入尚未保存" : workspace ? workspace.draft.revision === 0 ? "初始草稿 · 尚未保存" : `已保存 · 修订 ${workspace.draft.revision}` : "正在读取…"}</strong><span>阶段待验 · 基线冻结不代表开发完成</span></div></div>
      {error && <div role="alert" className="message error">{error}<button onClick={() => void action(async () => { await updateWorkspace(false); setNotice("服务器版本已读取，当前输入已保留。请核对后点击保存当前输入。"); })}>重新核对服务器版本</button></div>}
      {notice && <div role="status" className="message success">{notice}</div>}
      {!workspace ? <div className="panel"><p>正在读取需求；失败时可重新读取，登记入口仍在首页。</p></div> : <div className="requirement-grid">
        <div>
          <section className="panel requirement-panel"><div className="section-head"><div><p className="eyebrow">01 / 草稿</p><h2>八类需求输入</h2></div><button className="quiet" disabled={saving || busy || !dirty || !validInput} onClick={() => void save()}>保存当前输入</button></div>
            {dirty && !validInput && <p role="status" className="hint">请补齐条目，以及已知/不适用字段的依据（至少三个字）；填写完整后自动保存。</p>}
            {conflict && <div className="message error">保存未完成，自动保存已暂停；请与服务器修订 {workspace.draft.revision} 核对后，再明确保存当前输入。<details><summary>查看服务器草稿</summary>{workspace.draft.fields.map(f => <p key={f.key}>{labels[f.key]}：{f.content || "未知"}</p>)}</details></div>}
            <fieldset disabled={busy}>{fields.map((field, index) => <div className="requirement-field" key={field.key}><div className="field-heading"><label htmlFor={`field-${field.key}`}>{index + 1}. {labels[field.key]}</label><select aria-label={`${labels[field.key]}状态`} value={field.status} onChange={e => fieldChange(index, { status: e.target.value as Field["status"] })}><option value="unknown">不确定</option><option value="known">已知</option><option value="not_applicable">不适用（需依据）</option></select></div><textarea id={`field-${field.key}`} rows={3} maxLength={3000} value={field.content} placeholder={examples[field.key]} onChange={e => fieldChange(index, { content: e.target.value })}/><p className="source-note">来源：{sourceLabel(field)}</p></div>)}</fieldset>
          </section>
          <section className="panel requirement-panel"><div className="section-head"><div><p className="eyebrow">02 / 需求条目</p><h2>把需求写到能验收</h2></div><button className="quiet" disabled={busy || items.length >= 100} onClick={() => { changed(); const next = Math.max(0, ...items.map(i => Number(i.id.slice(1)))) + 1; setItems(current => [...current, { id: `R${next}`, value: "", trigger: "", rule: "", exception: "", acceptance: "", priority: "P0", target_version: "首版", source_ref: "user:requirement-item" }]); }}>＋ 添加条目</button></div>
            {items.length === 0 && <p className="hint">至少添加一条需求，写明业务规则与验收方式，才能冻结基线。</p>}
            <fieldset disabled={busy}>{items.map((item, index) => <div className="requirement-item" key={item.id}><h3>{item.id}</h3>{([['value','用户价值'],['trigger','触发条件'],['rule','业务规则'],['exception','异常处理'],['acceptance','验收方式']] as const).map(([key, label]) => <div key={key}><label htmlFor={`${item.id}-${key}`}>{label}</label><textarea id={`${item.id}-${key}`} rows={2} value={item[key]} maxLength={key === "rule" || key === "acceptance" ? 1000 : 600} onChange={e => itemChange(index, { [key]: e.target.value })}/></div>)}<div className="field-heading"><label htmlFor={`${item.id}-priority`}>优先级</label><select id={`${item.id}-priority`} value={item.priority} onChange={e => itemChange(index, { priority: e.target.value as Item["priority"] })}><option>P0</option><option>P1</option><option>P2</option></select></div><label htmlFor={`${item.id}-version`}>目标版本</label><input id={`${item.id}-version`} maxLength={60} value={item.target_version} onChange={e => itemChange(index, { target_version: e.target.value })}/></div>)}</fieldset>
          </section>
        </div>
        <aside>
          <section className="panel requirement-panel"><p className="eyebrow">03 / 体检与确认</p><h2>当前缺口</h2>{workspace.draft.gaps.length === 0 && <p>当前规则未发现字段缺口，仍需你核对业务内容。</p>}{workspace.draft.gaps.map(g => <div className="gap" key={g.id}><strong>{g.severity === "blocking" ? "需补齐" : "后续待验"}</strong><p>{g.message}</p><small>{g.stage}</small></div>)}<button className="primary" disabled={!ready} onClick={() => void action(async () => { const result = await api<Round>(`${base}/requirements/check`, { expected_revision: workspace.draft.revision }); setRound(result); setAnswers({}); })}>需求体检 · 最多五问</button>
            {round && !round.answered && round.revision === workspace.draft.revision && <div className="question-round"><h3>本轮问题</h3>{round.questions.length === 0 && <p>没有需要追问的字段缺口。</p>}{round.questions.map(q => <div className="gap" key={q.id}><h3>{q.question}</h3><p>推荐：{q.recommendation}</p><p>示例：{q.example}</p><p className="hint">影响：{q.impact}</p><label htmlFor={`answer-${q.id}`}>你的回答</label><textarea id={`answer-${q.id}`} rows={2} maxLength={3000} value={answers[q.id] ?? ""} onChange={e => setAnswers(current => ({ ...current, [q.id]: e.target.value }))}/><div className="button-row"><button disabled={!ready || (answers[q.id]?.trim().length ?? 0) < 3} onClick={() => void action(async () => { await api(`${base}/requirements/answers`, { expected_revision: workspace.draft.revision, round_id: round.id, question_version: round.version, answers: [{ question_id: q.id, mode: "custom", content: answers[q.id] }] }); await updateWorkspace(); })}>确认回答</button><button disabled={!ready} onClick={() => void action(async () => { await api(`${base}/requirements/answers`, { expected_revision: workspace.draft.revision, round_id: round.id, question_version: round.version, answers: [{ question_id: q.id, mode: "recommended" }] }); await updateWorkspace(); })}>采用此推荐</button><button disabled={!ready} onClick={() => void action(async () => { await api(`${base}/requirements/answers`, { expected_revision: workspace.draft.revision, round_id: round.id, question_version: round.version, answers: [{ question_id: q.id, mode: "uncertain" }] }); await updateWorkspace(); })}>不确定</button></div></div>)}<button className="primary" disabled={!ready || !round.questions.some(q => (answers[q.id]?.trim().length ?? 0) >= 3)} onClick={() => void action(async () => { await api(`${base}/requirements/answers`, { expected_revision: workspace.draft.revision, round_id: round.id, question_version: round.version, answers: round.questions.filter(q => (answers[q.id]?.trim().length ?? 0) >= 3).map(q => ({ question_id: q.id, mode: "custom", content: answers[q.id] })) }); await updateWorkspace(); })}>确认本轮已填写回答</button></div>}
            <hr/><h3>冻结本轮需求基线</h3><p className="hint">{blocked ? "当前阻断项未关闭，不能冻结。" : "核对当前已保存草稿；效果待验等后续事项仍会保留。"}</p><button className="primary" disabled={!ready || blocked} onClick={() => setFreezePreview(true)}>核对冻结内容</button>
            {freezePreview && ready && <div className="freeze-review"><h3>待冻结修订 {workspace.draft.revision}</h3><RequirementSnapshot fields={workspace.draft.fields} items={workspace.draft.items}/><label htmlFor="freeze-reason">确认说明</label><textarea id="freeze-reason" value={reason} maxLength={500} onChange={e => setReason(e.target.value)}/><button className="primary" disabled={!ready || reason.trim().length < 3} onClick={() => void action(async () => { await api(`${base}/requirements/freeze`, { expected_revision: workspace.draft.revision, expected_hash: workspace.draft.content_hash, reason }); setFreezePreview(false); await updateWorkspace(); setNotice("需求基线已冻结；这不代表开发或阶段验收完成。"); })}>确认冻结此版本</button></div>}
          </section>
          <section className="panel requirement-panel"><p className="eyebrow">04 / 决策与版本</p><h2>确认记录</h2>{workspace.decisions.length === 0 && <p className="hint">回答问题后的关键决策保存在这里。</p>}{workspace.decisions.map(d => <div className="gap" key={d.id}><strong>{d.status === "confirmed" ? "已定" : "待定"} · 修订 {d.revision}</strong><p>{d.question}</p><p>{d.conclusion}</p></div>)}<h3>草稿修订历史</h3><select aria-label="查看草稿修订" defaultValue="" onChange={e => { if (e.target.value) void action(async () => { setViewRevision(await api<Draft>(`${base}/requirements/revisions/${e.target.value}`)); }); }}><option value="">选择旧修订查看</option>{workspace.revisions.map(r => <option key={r} value={r}>修订 {r}</option>)}</select>{viewRevision && <div className="freeze-review"><h3>历史草稿 · 修订 {viewRevision.revision}</h3><RequirementSnapshot fields={viewRevision.fields} items={viewRevision.items}/></div>}<h3>冻结的基线</h3>{workspace.baselines.length === 0 && <p className="hint">暂无冻结基线。</p>}{workspace.baselines.map(b => <button className="baseline-link" key={b.id} onClick={() => setViewBaseline(b)}>查看修订 {b.revision} · {new Date(b.created_at).toLocaleString("zh-CN")}</button>)}{viewBaseline && <div className="freeze-review"><h3>不可变基线 · 修订 {viewBaseline.revision}</h3><p>{viewBaseline.reason}</p><RequirementSnapshot fields={viewBaseline.fields} items={viewBaseline.items}/>{viewBaseline.deferred.map(g => <p className="warning" key={g.id}>待验：{g.message}</p>)}</div>}</section>
        </aside>
        <section className="panel model-panel"><div className="section-head"><div><p className="eyebrow">05 / AI 辅助</p><h2>{model?.configured ? "候选需求分析" : "AI 未启用"}</h2></div><span className="badge">真实效果仍待验</span></div>
          <p className="hint">模型只给候选建议。配置不产生调用费用；发送前核对实际内容并授权。密钥、个人信息和企业机密不得外发。</p>
          <Link className="planning-entry" href={"/projects/"+projectId+"/context"} onClick={e=>{if((dirty||saving||secret)&&!window.confirm("输入尚未保存，确定离开吗？"))e.preventDefault();}}>上下文与知识资产 →</Link><Link className="planning-entry" href={"/projects/"+projectId+"/verification"} onClick={e=>{if((dirty||saving||secret)&&!window.confirm("输入尚未保存，确定离开吗？"))e.preventDefault();}}>工程验证 →</Link><Link className="planning-entry" href="/settings/provider" onClick={e => { if ((dirty || saving || secret) && !window.confirm("输入尚未保存，确定离开吗？")) e.preventDefault(); }}>查看 AI 提供方与能力 →</Link><details><summary>配置北京模型服务</summary><form autoComplete="off" onSubmit={e => { e.preventDefault(); void action(async () => { try { const result = await api<Model>("model/config", { api_key: secret, base_url: baseUrl }); setModel(result); setNotice("模型配置已保存，尚未调用或验证真实效果。"); } finally { setSecret(""); } }); }}><label htmlFor="model-secret">百炼 API Key（仅保存到本机后端）</label><input id="model-secret" type="password" autoComplete="new-password" value={secret} onChange={e => setSecret(e.target.value)} required minLength={20} maxLength={256}/><label htmlFor="model-base">北京接口地址</label><input id="model-base" value={baseUrl} onChange={e => setBaseUrl(e.target.value)} required maxLength={300}/><button className="quiet" disabled={busy}>保存配置，不调用模型</button></form></details>
          {model && <p className="source-note">开发阶段：已结算 ¥{yuan(model.spent_micro)}，预留 ¥{yuan(model.reserved_micro)}；请求已用 {model.requests_used}，预留 {model.requests_reserved}。上限 ¥100 / 100 次，单任务 ¥2。报价 {model.quote_date}{model.quote_valid ? "，在有效期内" : "，已过期，禁止调用"}。</p>}
          <fieldset disabled={!ready}>{fields.map(f => <label className="choice" key={f.key}><input type="checkbox" checked={selected.includes(f.key)} onChange={e => { setSelected(current => e.target.checked ? [...current, f.key] : current.filter(k => k !== f.key)); setPreview(null); setConsent(false); }}/>{labels[f.key]}</label>)}</fieldset>
          <button className="quiet" disabled={!ready || !model?.configured || !model.quote_valid || selected.length === 0} onClick={() => void action(async () => { setPreview(await api<Preview>(`${base}/analysis/preview`, { expected_revision: workspace.draft.revision, selected_fields: selected })); setConsent(false); })}>预览将发送的需求</button>
          {preview && <div className="freeze-review"><h3>外发内容预览 · 修订 {preview.revision}</h3><p>{preview.model} · {preview.base_url}</p>{preview.selected_fields.map(f => <p key={f.key}><strong>{labels[f.key]}</strong>：{f.content || "未知"}</p>)}<p>未选字段：{preview.omitted_fields.map(k => labels[k]).join("、") || "无"}。不读取或发送项目目录中的其他文件。</p><p>最多 {preview.max_attempts} 次请求，费用预留上界 ¥{yuan(preview.reserve_micro)}；授权到 {new Date(preview.expires_at).toLocaleTimeString("zh-CN")}。</p><label className="choice"><input type="checkbox" checked={consent} onChange={e => setConsent(e.target.checked)}/>我已核对，仅包含脱敏非敏感资料，允许在上述范围与额度内发送。</label><button className="primary" disabled={!ready || !consent} onClick={() => void action(async () => { const auth = await api<{ id: string }>(`${base}/analysis/authorize`, { preview_id: preview.id, expected_hash: preview.content_hash, nonsensitive_confirmed: true }); const result = await api<Run>(`${base}/analysis/runs`, { expected_revision: workspace.draft.revision, preview_id: preview.id, authorization_id: auth.id }); setRun(result); setPreview(null); setConsent(false); setModel(await api<Model>("model/status")); })}>授权发送并开始分析</button></div>}
          {run && <div className="freeze-review"><h3>分析状态：{{ queued: "已受理", running: "分析中", succeeded: "候选已生成", failed: "失败", result_unknown: "结果未知，待核对" }[run.state] ?? run.state}</h3><p>来源：{run.source === "real_cloud" ? "真实云模型" : "Mock 测试"} · 需求修订 {run.revision}。本次已知结算 ¥{yuan(run.settled_micro)}。</p>{["queued", "running"].includes(run.state) && <button className="quiet" disabled={busy} onClick={() => void action(async () => { await api(`${base}/analysis/authorizations/${run.authorization_id}/revoke`, {}); setNotice("后续外发授权已撤销；已经发出的请求仍需等待结果与费用核对。"); })}>撤销后续外发授权</button>}{run.error && <p role="alert" className="warning">{run.error.message}</p>}{run.candidate && <><p>{run.candidate.summary}</p>{run.candidate.suggestions.map(s => <div className="gap" key={s.field}><label className="choice"><input type="checkbox" checked={adoptFields.includes(s.field)} onChange={e => setAdoptFields(current => e.target.checked ? [...current, s.field] : current.filter(k => k !== s.field))}/>{labels[s.field]}</label><p>{s.content}</p><p className="source-note">建议依据：{s.rationale}；引用：{s.source_fields.map(k => labels[k]).join("、")}</p></div>)}{run.candidate.questions.map((q, i) => <div className="gap" key={i}><h3>待确认：{q.question}</h3><p>推荐：{q.recommendation}；示例：{q.example}</p><p>影响：{q.impact}</p></div>)}<button className="primary" disabled={!ready || run.revision !== workspace.draft.revision || adoptFields.length === 0} onClick={() => void action(async () => { await api(`${base}/analysis/runs/${run.id}/adopt`, { expected_revision: workspace.draft.revision, fields: adoptFields }); await updateWorkspace(); setAdoptFields([]); setNotice("选中的候选已采用为新草稿修订，尚未冻结。"); })}>确认采用所选建议到草稿</button></>}</div>}
        </section>
      </div>}
      <footer>需求历史和人工确认保存在本机；当前范围为需求准备，工程与真实效果验收分别记录。</footer>
    </main>
  </div>;
}
