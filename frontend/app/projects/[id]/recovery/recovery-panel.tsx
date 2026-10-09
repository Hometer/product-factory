"use client";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

type Checkpoint = { id: string; created_at: string; phase: string; state: string; verdict: string; run_hash: string; reason: string; exit_code: number | null };
type Task = { run: { id: string; asset_id: string; state: string; details: { task_id: string; step_id: string; step_instruction: string; recipe_id: string; started_at: string | null; ended_at: string | null; exit_code: number | null; error_code: string | null; stop_requested: boolean; checks: { path: string; valid: boolean }[] } }; verdict: string; message: string; completed_steps: number; total_steps: number; step_state: string; checkpoint_id: string | null; expected_hash: string; can_resume: boolean; blockers: string[]; checkpoints: Checkpoint[]; checkpoint_count: number; asset_hash: string; authorization_id: string };
type Analysis = { id: string; state: string; attempts: { attempt: number; state: string }[]; reserved_micro: number; settled_micro: number; message: string };
type Workspace = { tasks: Task[]; analyses: Analysis[]; task_count: number; analysis_count: number; message: string };
const verdicts: Record<string, string> = { not_executed: "确定未执行 · 可核对继续", result_unknown: "结果未知 · 禁止重复执行", completed: "原任务已终结 · 仍需阶段验收", active: "原任务正在等待或执行", conflict: "结果产物冲突 · 需排查", stopped: "停止请求优先 · 不恢复", paused: "用户暂停 · 不自动恢复", cancelled: "用户取消 · 终态" };
const phases: Record<string, string> = { queued: "原批准与入队", launch_intent: "启动意图已提交", executing: "真实执行状态", finished: "终结结果已记录", reconciled: "重启核对", reviewed: "人工请求核对", resumed: "确认继续原任务", pause_requested: "暂停请求已保存", cancel_requested: "取消请求已保存", paused: "暂停已生效", cancelled: "取消已生效", continued: "明确继续暂停任务", retry_approved: "安全重复已批准" };
const steps: Record<string, string> = { pending: "尚未执行", running: "正在执行", succeeded: "本次检查通过", failed: "本次检查未通过", unknown: "结果待核对", stopped: "已停止或禁止继续" };
const date = (value: string | null) => value ? new Date(value).toLocaleString("zh-CN") : "尚无";

async function api<T>(path: string, body?: unknown, key?: string): Promise<T> {
  const response = await fetch("/api/factory/" + path, body === undefined ? { cache: "no-store" } : {
    method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": key ?? crypto.randomUUID() }, body: JSON.stringify(body),
  });
  const value = await response.json();
  if (!response.ok) throw new Error((value.error?.code ?? "REQUEST_FAILED") + "：" + (value.error?.message ?? "操作未完成，记录保留"));
  return value as T;
}

export default function RecoveryPanel({ projectId }: { projectId: string }) {
  const base = "projects/" + projectId + "/recovery";
  const [workspace, setWorkspace] = useState<Workspace | null>(null), [selected, setSelected] = useState("");
  const [reason, setReason] = useState(""), [confirmed, setConfirmed] = useState(false), [busy, setBusy] = useState(false);
  const [error, setError] = useState(""), [message, setMessage] = useState("");
  const keys = useRef<Record<string, { text: string; key: string }>>({});
  const current = workspace?.tasks.find(t => t.run.id === selected);
  const running = workspace?.tasks.some(t => t.verdict === "active");
  const load = useCallback(async () => {
    const value = await api<Workspace>(base);
    setWorkspace(value);
    setSelected(old => old || value.tasks[0]?.run.id || "");
    return value;
  }, [base]);
  useEffect(() => { let live = true; api<Workspace>(base).then(value => {
    if (live) { setWorkspace(value); setSelected(value.tasks[0]?.run.id ?? ""); }
  }).catch(e => { if (live) setError(e.message); }); return () => { live = false; }; }, [base]);
  useEffect(() => {
    if (!running) return;
    let live = true, timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try { const value = await api<Workspace>(base); if (live) setWorkspace(value); }
      catch (e) { if (live) setError(e instanceof Error ? e.message : "查询失败，记录保留"); }
      finally { if (live) timer = setTimeout(poll, 1000); }
    };
    timer = setTimeout(poll, 500); return () => { live = false; clearTimeout(timer); };
  }, [base, running]);
  useEffect(() => {
    const guard = (e: BeforeUnloadEvent) => { if (reason.trim() || busy) { e.preventDefault(); e.returnValue = ""; } };
    window.addEventListener("beforeunload", guard); return () => window.removeEventListener("beforeunload", guard);
  }, [reason, busy]);
  function leave(e: React.MouseEvent<HTMLAnchorElement>) {
    if ((reason.trim() || busy) && !window.confirm("核对依据尚未保存或操作未结束，确定离开吗？")) e.preventDefault();
  }
  function choose(id: string) {
    if (reason.trim() && !window.confirm("核对依据尚未保存，确定切换任务吗？")) return;
    setSelected(id); setReason(""); setConfirmed(false); setMessage(""); setError("");
  }
  function key(action: string, body: unknown) {
    const text = JSON.stringify(body);
    if (keys.current[action]?.text !== text) keys.current[action] = { text, key: crypto.randomUUID() };
    return keys.current[action].key;
  }
  async function act(action: () => Promise<void>) {
    setBusy(true); setError(""); setMessage("");
    try { await action(); } catch (e) { setError(e instanceof Error ? e.message : "操作未完成，输入保留"); }
    finally { setBusy(false); }
  }
  async function check() {
    if (!current) return;
    const body = { confirmed: true, reason };
    await api<Task>(base + "/runs/" + current.run.id + "/check", body, key("check-" + current.run.id, body));
    setReason(""); setConfirmed(false); await load(); setMessage("核对已保存；没有启动程序，也没有更改未知结果或费用。");
  }
  async function resume() {
    if (!current) return;
    const id = current.run.id;
    const body = { confirmed: true, reason, checkpoint_id: current.checkpoint_id, expected_hash: current.expected_hash };
    await api<Task>(base + "/runs/" + id + "/resume", body, key("resume-" + id, body));
    setReason(""); setConfirmed(false); await load(); setMessage("继续请求已受理，沿用任务 " + id + "；请查看最终状态。");
  }
  return <div className="shell recovery-workspace"><header className="topbar">
    <Link className="brand" href="/" onClick={leave}><span className="brandmark">工</span>产品工厂</Link>
    <Link href={"/projects/" + projectId + "/commands"} onClick={leave}>← 命令与运行记录</Link>
  </header><main>
    <section className="intro"><div><span className="eyebrow">任务接续 · FR11</span><h1>先核对，再继续原任务</h1><p className="subtitle">刷新不新建任务 · 未知结果不重发</p></div>
      <div className="scope">只继续确定未执行的受控步骤。成功仅表示本次配方结果满足，不表示阶段通过。</div></section>
    {error && <div role="alert" className="message error">{error} · 当前输入保留</div>}
    {message && <div role="status" className="message success">{message}</div>}
    {!workspace && !error && <p role="status">正在读取持久记录…</p>}
    <Link className="planning-entry" href={"/projects/"+projectId+"/controls"} onClick={leave}>暂停取消与有限重试 →</Link><div className="requirement-grid"><section className="panel requirement-panel">
      <div className="section-head"><h2>恢复核对</h2><button className="quiet" disabled={busy} onClick={() => act(async () => { await load(); setConfirmed(false); })}>刷新持久记录</button></div>
      <p className="hint">{workspace?.message}</p>
      <label htmlFor="recovery-task">选择已有运行（{workspace?.task_count ?? 0} 条）</label>
      <select id="recovery-task" value={selected} disabled={busy} onChange={e => choose(e.target.value)}>
        <option value="">选择原任务</option>{workspace?.tasks.map(t => <option value={t.run.id} key={t.run.id}>{t.run.details.task_id} · {verdicts[t.verdict]} · {t.run.id.slice(0, 8)}</option>)}
      </select>
      {!workspace?.tasks.length && <p className="hint">暂无命令任务；到命令页保存具体配方并审阅启动后可查看。</p>}
      {current && <>
        <div className="freeze-review"><h3>{verdicts[current.verdict]}</h3><p>{current.message}</p>
          <p>已记录终结结果的配方步骤 {current.completed_steps}/{current.total_steps} · {steps[current.step_state]}</p>
          <p>步骤 {current.run.details.step_id}：{current.run.details.step_instruction}</p>
          <p>开始 {date(current.run.details.started_at)}<br />结束 {date(current.run.details.ended_at)}<br />退出码 {current.run.details.exit_code ?? "尚无"} · 错误 {current.run.details.error_code ?? "无"}</p>
          <p className="path">原任务 ID：{current.run.id}<br />原配方资产：{current.run.asset_id}<br />原授权：{current.authorization_id}</p>
          {current.run.details.checks.map(c => <p key={c.path}>{c.path}：{c.valid ? "本次检查通过" : "本次检查未通过"}</p>)}
          {current.blockers.map(b => <p className="warning" key={b}>{b}</p>)}
        </div>
        <fieldset disabled={busy}><label htmlFor="recovery-reason">本次核对或继续依据（不要填写秘密）</label>
          <textarea id="recovery-reason" rows={3} value={reason} maxLength={500} onChange={e => { setReason(e.target.value); setConfirmed(false); }} placeholder="例如：已核对重启记录，原任务尚未创建进程，输入和授权保持有效" />
          <button className="quiet" disabled={reason.trim().length < 4} onClick={() => act(check)}>保存恢复核对（不执行）</button>
          {current.can_resume && <><label className="choice command-choice" htmlFor="recovery-confirm"><input id="recovery-confirm" type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} />我已核对确定未执行、原批准与当前输入，确认继续同一任务</label>
            <button className="primary" disabled={!confirmed || reason.trim().length < 4} onClick={() => act(resume)}>确认继续原任务</button></>}
        </fieldset>
        <p className="hint">结果未知时，先核对提供方或独立进程事实；本页不能把未知改成成功、解除未知占用或释放保留预算。失败、停止或超时不在此重跑。</p>
      </>}
    </section><section className="panel requirement-panel"><h2>检查点与接续历史</h2>
      {!current?.checkpoints.length && <p className="hint">旧运行没有新版检查点；不据此推断尚未执行。已保存退出码与产物仍可核对。</p>}
      {current && <><p className="hint">{current.checkpoint_count} 个检查点，显示最近 {current.checkpoints.length} 个；旧记录保留。</p>
        {current.checkpoints.map(c => <article className="gap" key={c.id}><h3>{phases[c.phase]}</h3><p>{date(c.created_at)} · {verdicts[c.verdict]}</p><p>{c.reason || "执行器确定性记录"}</p>
          <details><summary>查看版本与结果指纹</summary><p className="path">核对 ID {c.id}<br />运行事实 SHA-256 {c.run_hash}<br />当时状态 {c.state} · 退出码 {c.exit_code ?? "尚无"}</p></details></article>)}</>}
      <hr /><h2>外部模型结果核对</h2><p className="hint">{workspace?.analysis_count ?? 0} 条已有模型运行。本轮不调用模型；这里仅查看状态、尝试和保留费用。</p>
      {!workspace?.analyses.length && <p className="hint">此项目暂无外部运行。</p>}
      {workspace?.analyses.map(a => <article className="gap" key={a.id}><h3>{a.state === "result_unknown" ? "外部结果未知 · 禁止重发" : "已有模型运行"}</h3><p>{a.message}</p><p className="path">运行 {a.id}</p><p>保留预算 ¥{(a.reserved_micro / 1_000_000).toFixed(6)} · 已记录费用 ¥{(a.settled_micro / 1_000_000).toFixed(6)}</p>{a.attempts.map(t => <p key={t.attempt}>尝试 {t.attempt} · {t.state}</p>)}</article>)}
    </section></div><footer>本机持久接续 · 保留原批准和历史 · 未宣布阶段通过</footer>
  </main></div>;
}
