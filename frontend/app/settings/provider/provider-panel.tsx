"use client";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

type Capability = { id: string; name: string; implemented: boolean; offline_contract: string; real_verified_count: number; can_run: boolean; explanation: string };
type Receipt = { id: string; project_id: string; run_id: string; attempt: number; source: string; model: string; base_url: string; contract_version: string; adapter_hash: string; checked_at: string; request_id: string | null; elapsed_ms: number; sent: boolean; text_valid: boolean; structure_valid: boolean; usage_valid: boolean; input_tokens: number | null; output_tokens: number | null; cost_micro: number | null; error_code: string | null };
type View = { provider: string; model: string; base_url: string; configured: boolean; key_mask: string; contract_version: string; adapter_hash: string; automatic_switch: false; max_attempts: number; blockers: string[]; capabilities: Capability[]; receipts: Receipt[]; receipt_total: number; quote_date: string; quote_valid: boolean; spent_micro: number; reserved_micro: number; requests_used: number; requests_reserved: number; message: string };
const yuan = (n: number) => (n / 1_000_000).toFixed(4);
export default function ProviderPanel() {
  const [data, setData] = useState<View | null>(null), [error, setError] = useState(""), [busy, setBusy] = useState(true);
  const load = useCallback(async () => {
    setBusy(true); setError("");
    try { const r = await fetch("/api/factory/model/provider", { cache: "no-store" }); const value = await r.json(); if (!r.ok) throw new Error(value.error?.message ?? "提供方状态暂不可读"); setData(value); }
    catch (e) { setError(e instanceof Error ? e.message : "读取失败，请重新核对"); } finally { setBusy(false); }
  }, []);
  useEffect(() => { let live = true; fetch("/api/factory/model/provider", { cache: "no-store" }).then(async r => { const value = await r.json(); if (!r.ok) throw new Error(value.error?.message ?? "提供方状态暂不可读"); if (live) setData(value); }).catch(e => { if (live) setError(e.message); }).finally(() => { if (live) setBusy(false); }); return () => { live = false; }; }, []);
  return <div className="shell provider-workspace"><header className="topbar"><Link className="brand" href="/"><span className="brandmark">工</span>LaunchFlow</Link><Link href="/">← 产品工作台</Link></header><main>
    <section className="intro"><div><p className="eyebrow">AI 服务与能力</p><h1>清楚知道模型能做什么</h1><p className="subtitle">平台已接入、离线契约与真实观察分别展示。</p></div><div className="scope">保存密钥不调用模型。真实效果须通过已批准的项目样例验证。</div></section>
    {error && <div role="alert" className="message error">{error} · {data ? "下方为上次读取状态，不能据此开始调用" : "手动需求编辑仍可使用"}</div>}
    <section className="panel"><div className="section-head"><h2>提供方与配置状态</h2><button className="quiet" disabled={busy} onClick={() => void load()}>刷新能力与回执</button></div>{!data && !error && <p role="status">正在读取本机记录…</p>}
      {data && <><p><strong>{data.provider}</strong> · {data.model}</p><p id="provider-key">密钥 {data.configured ? "已配置" : "未配置"} · {data.key_mask}</p><p className="path">{data.base_url}</p><p className="hint">{data.message}</p>{data.blockers.map(b => <p className="warning" key={b}>{b}</p>)}<p className="source-note">配置入口：回到任意项目的“整理需求与确认基线”，展开“配置北京模型服务”，填密码框并点“保存配置，不调用模型”。请勿在聊天或依据中填写密钥。</p></>}
    </section>
    {data && <><div className="provider-grid">{data.capabilities.map(c => <section className="panel capability-card" key={c.id}><div className="section-head"><h2>{c.name}</h2><span className="badge">{c.implemented ? "平台已接入" : "平台未接入"}</span></div><p>{c.explanation}</p><p>离线契约：{c.offline_contract === "verified" ? "通过 · 不代表真实服务已验证" : "未支持"}</p><p>当前版本真实观察：{c.real_verified_count ? `${c.real_verified_count} 次回执通过 · 不代替效果评分` : "待验 · 无匹配的真实通过回执"}</p><p className={c.can_run && !error ? "hint" : "warning"}>{c.can_run && !error ? "可在项目资料与额度明确批准后调用" : "当前不能运行"}</p></section>)}</div>
      <div className="requirement-grid"><section className="panel"><h2>调用与费用边界</h2><p>自动切换：关闭</p><p>结构修正最多一次，共 {data.max_attempts} 次尝试；每次占用授权次数和预算，仍失败不写正式决策。</p><p>已授权硬上限：单任务 ¥2 · 开发阶段 ¥100 / 100 次请求。当前收紧规则见项目成本页，重试计入。</p><p>阶段已知结算 ¥{yuan(data.spent_micro)}<br />预留 ¥{yuan(data.reserved_micro)}<br />请求已用 {data.requests_used} · 预留 {data.requests_reserved}</p><p>报价 {data.quote_date} · {data.quote_valid ? "有效期内" : "过期，禁止调用"}</p><p className="hint">已发出请求无法保证撤回；未知费用在财务核对前保留预算；在对应项目成本页追加人工对账，原回执仍保留。这里不释放预算或提供自动重发。</p></section>
      <section className="panel"><h2>验证版本与来源</h2><p>{data.contract_version}</p><p className="path">适配器摘要 {data.adapter_hash}</p><p className="hint">改模型、地址或适配器后旧观察只作历史。Mock 不计入真实观察；无效结构和未知用量不记为通过。服务商支持某能力不等于本平台已接入。</p><p className="hint">流式、代码生成与提供方请求查询尚未支持。请求编号只是核对依据，不能据此认定外部结果或最终费用。</p></section></div>
      <section className="panel provider-receipts"><div className="section-head"><h2>各项目调用回执</h2><span className="badge">{data.receipt_total} 条 · 最多显示最近 100 条</span></div>{!data.receipts.length && <p className="empty-note">尚无调用回执。保存配置不发送资料；调用须有效项目批准，不能用保存配置或离线测试冒充真实验证。</p>}{data.receipts.map(r => <article className="gap" key={r.id}><h3>{r.source === "real_cloud" ? "真实云模型回执" : "Mock 回执 · 不计真实验证"} · 尝试 {r.attempt}/2</h3><p>{new Date(r.checked_at).toLocaleString("zh-CN")} · {r.model}</p><p>{r.sent ? "请求已发送" : "未发送"} · 耗时 {r.elapsed_ms} ms · 请求编号 {r.request_id ?? "未提供或不安全，未记录"}</p><p>文字 {r.text_valid ? "本次校验通过" : "未确认"} · 结构 {r.structure_valid ? "本次校验通过" : "未通过或未确认"} · 用量 {r.usage_valid ? "已核对" : "待核对"}</p><p>输入 {r.input_tokens ?? "未知"} / 输出 {r.output_tokens ?? "未知"} tokens · 本次用量报价核算 {r.cost_micro === null ? "未知 · 见项目成本对账" : `¥${yuan(r.cost_micro)}`}</p><p>错误 {r.error_code ?? "无"}</p><p className="path">运行 {r.run_id}<br />地址 {r.base_url}<br />版本 {r.contract_version} · {r.adapter_hash}</p><p><Link href={"/projects/" + r.project_id + "/costs"}>查看该项目成本与用量 →</Link></p><Link href={"/projects/" + r.project_id}>打开对应项目需求与分析记录 →</Link></article>)}</section>
    </>}
    <footer>本机记录 · 配置与验证分开 · 真实模型及效果仍待验</footer>
  </main></div>;
}
