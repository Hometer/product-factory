"use client";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

type Run = { id: string; state: string; asset_id: string; created_at: string; details: { task_id: string; step_id: string; step_instruction: string; recipe_id: string; reason: string; exit_code: number | null; error_code: string | null; message: string; started_at: string | null; ended_at: string | null; port: number | null; health: string; checks: { path: string; valid: boolean }[] } };
type Request = { kind: "pause" | "cancel"; reason: string; requested_at: string; effect: string; effective_at: string | null; process_started: boolean | null };
type Entry = { run: Run; root_id: string; parent_id: string | null; attempt: number; max_attempts: number; cost_micro: number; request: Request | null; expected_hash: string; can_pause: boolean; can_cancel: boolean; can_continue: boolean; can_retry: boolean; blockers: string[] };
type Workspace = { entries: Entry[]; total: number; message: string };
type Action = "pause" | "cancel" | "continue" | "retry";
const states: Record<string, string> = { queued: "等待执行", starting: "正在启动", running: "正在检查", serving: "静态预览运行中", succeeded: "配方通过 · 仍需验收", failed: "配方未通过", timed_out: "超时 · 已停止", stopped: "已停止", interrupted: "结果未知 · 待核对", paused: "暂停已生效", cancelled: "取消已生效 · 终态" };
const effects: Record<string, string> = { pending: "请求已发出，等待生效", applied: "已确认生效", step_completed: "已生效，当前唯一安全步骤已结束", unknown: "请求已保存，无法确认生效；结果仍未知" };
const names: Record<Action, string> = { pause: "请求暂停", cancel: "请求取消", continue: "确认继续暂停任务", retry: "确认安全重试" };
const date = (value: string | null) => value ? new Date(value).toLocaleString("zh-CN") : "尚无";
const active = (e: Entry) => ["queued", "starting", "running", "serving"].includes(e.run.state) || e.request?.effect === "pending";
async function api<T>(path: string, body?: unknown, key?: string): Promise<T> {
  const response = await fetch("/api/factory/" + path, body === undefined ? { cache: "no-store" } : { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": key ?? crypto.randomUUID() }, body: JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok) throw new Error((data.error?.code ?? "REQUEST_FAILED") + "：" + (data.error?.message ?? "操作未完成，请查看原记录"));
  return data as T;
}
export default function ControlsPanel({ projectId }: { projectId: string }) {
  const base = "projects/" + projectId + "/controls";
  const [workspace, setWorkspace] = useState<Workspace | null>(null), [selected, setSelected] = useState("");
  const [reason, setReason] = useState(""), [confirmed, setConfirmed] = useState(false), [busy, setBusy] = useState(false);
  const [error, setError] = useState(""), [message, setMessage] = useState("");
  const keys = useRef<Record<string, { text: string; key: string }>>({});
  const current = workspace?.entries.find(e => e.run.id === selected);
  const pending = workspace?.entries.some(active);
  const load = useCallback(async () => { const value = await api<Workspace>(base); setWorkspace(value); setSelected(old => old || value.entries[0]?.run.id || ""); }, [base]);
  useEffect(() => { let live = true; api<Workspace>(base).then(value => { if (live) { setWorkspace(value); setSelected(value.entries[0]?.run.id ?? ""); } }).catch(e => { if (live) setError(e.message); }); return () => { live = false; }; }, [base]);
  useEffect(() => {
    if (!pending) return;
    let live = true, timer: ReturnType<typeof setTimeout>;
    const poll = async () => { try { const value = await api<Workspace>(base); if (live) setWorkspace(value); } catch (e) { if (live) setError(e instanceof Error ? e.message : "查询失败，记录保留"); } finally { if (live) timer = setTimeout(poll, 750); } };
    timer = setTimeout(poll, 300); return () => { live = false; clearTimeout(timer); };
  }, [base, pending]);
  useEffect(() => { const guard = (e: BeforeUnloadEvent) => { if (reason.trim() || busy) { e.preventDefault(); e.returnValue = ""; } }; window.addEventListener("beforeunload", guard); return () => window.removeEventListener("beforeunload", guard); }, [reason, busy]);
  function leave(e: React.MouseEvent<HTMLAnchorElement>) { if ((reason.trim() || busy) && !window.confirm("控制依据尚未保存或请求未结束，确定离开吗？")) e.preventDefault(); }
  function choose(id: string) { if (reason.trim() && !window.confirm("控制依据尚未保存，确定切换尝试吗？")) return; setSelected(id); setReason(""); setConfirmed(false); setError(""); setMessage(""); }
  async function act(action: () => Promise<void>) { setBusy(true); setError(""); setMessage(""); try { await action(); } catch (e) { setError(e instanceof Error ? e.message : "操作未完成，输入保留"); } finally { setBusy(false); } }
  async function control(action: Action) {
    if (!current) return;
    const body = { confirmed: true, reason, expected_hash: current.expected_hash }, text = JSON.stringify(body), id = action + current.run.id;
    if (keys.current[id]?.text !== text) keys.current[id] = { text, key: crypto.randomUUID() };
    const result = await api<Entry>(base + "/runs/" + current.run.id + "/" + action, body, keys.current[id].key);
    setSelected(result.run.id); setReason(""); setConfirmed(false); await load();
    setMessage(action === "pause" || action === "cancel" ? "请求已保存，请以请求的生效状态和最终运行结果为准。" : "明确批准已保存；请查看原任务关联和当前尝试结果，刷新不会重复执行。");
  }
  return <div className="shell controls-workspace"><header className="topbar"><Link className="brand" href="/" onClick={leave}><span className="brandmark">工</span>产品工厂</Link><Link href={"/projects/" + projectId + "/commands"} onClick={leave}>← 命令与运行记录</Link></header><main>
    <section className="intro"><div><span className="eyebrow">任务控制 · FR12</span><h1>暂停、取消与安全重试</h1><p className="subtitle">请求与生效分别记录 · 未知结果不重复执行</p></div><div className="scope">最多初次加两次重试。静态服务重新启动也计入；新尝试保留原结果。</div></section>
    <Link className="planning-entry" href={"/projects/" + projectId + "/recovery"} onClick={leave}>查看恢复核对与检查点历史 →</Link>
    {error && <div role="alert" className="message error">{error} · 当前输入保留</div>}{message && <div role="status" className="message success">{message}</div>}
    {!workspace && !error && <p role="status">正在读取持久控制记录…</p>}
    <div className="requirement-grid"><section className="panel requirement-panel"><div className="section-head"><h2>当前尝试与控制</h2><button className="quiet" disabled={busy} onClick={() => act(async () => { await load(); setConfirmed(false); })}>刷新持久记录</button></div><p className="hint">{workspace?.message}</p>
      <label htmlFor="control-run">已有尝试（{workspace?.total ?? 0} 条）</label><select id="control-run" value={selected} disabled={busy} onChange={e => choose(e.target.value)}><option value="">选择当前最新尝试</option>{workspace?.entries.map(e => <option key={e.run.id} value={e.run.id}>{e.run.details.task_id} · 尝试 {e.attempt}/3 · {states[e.run.state]} · {e.run.id.slice(0, 8)}</option>)}</select>
      {!workspace?.entries.length && <p className="hint">暂无运行，到命令页审阅具体配方并启动后可控制。</p>}
      {current && <><div className="freeze-review"><h3>{states[current.run.state]}</h3><p>{current.run.details.message}</p><p>尝试 {current.attempt}/{current.max_attempts} · 本次费用 ¥{(current.cost_micro / 1_000_000).toFixed(2)}</p><p>步骤 {current.run.details.step_id}：{current.run.details.step_instruction}</p><p>开始 {date(current.run.details.started_at)}<br />结束 {date(current.run.details.ended_at)}<br />退出码 {current.run.details.exit_code ?? "尚无"} · 错误 {current.run.details.error_code ?? "无"}</p>
        <p className="path">原任务 {current.root_id}<br />当前尝试 {current.run.id}<br />上次尝试 {current.parent_id ?? "初次"}</p>{current.run.details.port !== null && <p>本机端口 {current.run.details.port} · {current.run.details.health === "healthy" ? "本次实例健康" : current.run.details.health === "stopped" ? "已停止" : "待核对"}{current.run.state === "serving" && <><br /><a href={"http://127.0.0.1:" + current.run.details.port} target="_blank" rel="noreferrer">打开本次静态预览 ↗</a></>}</p>}
        {current.request ? <div className="control-request"><h3>{current.request.kind === "pause" ? "暂停请求" : "取消请求"}</h3><p>{effects[current.request.effect]}</p><p>发出 {date(current.request.requested_at)}<br />生效 {date(current.request.effective_at)}</p><p>依据：{current.request.reason}</p></div> : <p className="hint">本次暂无暂停或取消请求。</p>}
      </div><fieldset disabled={busy}><label htmlFor="control-reason">本次操作依据（不要填写秘密）</label><textarea id="control-reason" rows={3} maxLength={500} value={reason} onChange={e => { setReason(e.target.value); setConfirmed(false); }} placeholder="例如：已核对本次预览归属，暂停后保留历史；只重复已确认的只读配方" />
        <label className="choice command-choice" htmlFor="control-confirm"><input id="control-confirm" type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} />我已核对当前尝试、原授权与对应操作；请求发出不等于生效</label><div className="button-row">{(["pause", "cancel", "continue", "retry"] as Action[]).filter(a => current[("can_" + a) as "can_pause"] as boolean).map(a => <button className={a === "continue" || a === "retry" ? "primary" : "quiet"} key={a} disabled={!confirmed || reason.trim().length < 4} onClick={() => act(() => control(a))}>{names[a]}</button>)}</div>
      </fieldset><details><summary>为什么有些操作不可用</summary>{current.blockers.map(b => <p className="warning" key={b}>{b}</p>)}</details><p className="hint">当前唯一安全检查可结束后暂停生效；全部步骤已结束时保留真实成功/失败。已启动的静态预览继续会创建关联的新尝试并计入上限，不是恢复旧进程。成功、取消、未知或产物冲突不能在这里重试。</p></>}
    </section><section className="panel requirement-panel"><h2>关联尝试历史</h2><p className="hint">原任务与每次原因、结果保留，尝试数不代表产品完成率。</p>{workspace?.entries.filter(e => !current || e.root_id === current.root_id).map(e => <article className="gap" key={e.run.id}><h3>尝试 {e.attempt}/3 · {states[e.run.state]}</h3><p>{date(e.run.created_at)}</p><p>依据：{e.run.details.reason}</p><p>退出码 {e.run.details.exit_code ?? "尚无"} · 本次费用 ¥0.00</p>{e.request && <p>{e.request.kind === "pause" ? "暂停" : "取消"}：{effects[e.request.effect]}</p>}{e.run.details.checks.map(c => <p key={c.path}>{c.path}：{c.valid ? "本次检查通过" : "本次检查未通过"}</p>)}<button className="quiet baseline-link" disabled={busy} onClick={() => choose(e.run.id)}>查看尝试 {e.attempt} · {e.run.id.slice(0, 8)}</button></article>)}</section></div><footer>本机受控配方 · 费用零 · 模型/外部控制未接入 · 未宣布阶段通过</footer>
  </main></div>;
}
