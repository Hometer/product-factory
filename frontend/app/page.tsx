"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";

type Project = { id: string; name: string; description: string; target_user: string; core_task: string; path: string; created_at: string; revision: number; archived: boolean };
type Form = { name: string; description: string; target_user: string; core_task: string; path: string; template_version: string; delivery_target: string };
type Preview = { preview_id: string; content_hash: string; planned_files: string[]; manifest: { warnings: string[]; files: { path: string; status: string; size: number; chars: number }[] } };
const initialForm: Form = { name: "", description: "", target_user: "", core_task: "", path: "", template_version: "starter-v1", delivery_target: "local" };
const statuses: Record<string, string> = { parsed: "可读取", sensitive_excluded: "敏感内容已排除", invalid_type: "格式不符", parse_failed: "解析失败", unsafe_archive: "不安全文档", needs_manual: "需人工处理", needs_transcription: "需人工转写", planned: "将创建" };

async function api<T>(route: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api/factory/${route}`, { ...options, headers: { "Content-Type": "application/json", ...options?.headers } });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message ?? "操作未完成，请重试");
  return data as T;
}

export default function Workbench() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [form, setForm] = useState(initialForm);
  const [mode, setMode] = useState<"create" | "import">("import");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [stale, setStale] = useState(false);
  const [archived, setArchived] = useState(false);
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [updated, setUpdated] = useState("");
  const [roots, setRoots] = useState<string[]>([]);
  const [selected, setSelected] = useState<Project | null>(null);
  const [modelEnabled, setModelEnabled] = useState(false);
  const sequence = useRef(0);

  const load = useCallback(async (search = "", showArchived = archived) => {
    const ticket = ++sequence.current;
    try {
      const result = await api<{ items: Project[]; total: number }>(`projects?q=${encodeURIComponent(search)}&limit=100&archived=${showArchived}`);
      if (ticket !== sequence.current) return;
      setProjects(result.items); setUpdated(new Date().toLocaleTimeString("zh-CN")); setStale(false); setError("");
    } catch (e) {
      if (ticket !== sequence.current) return;
      setStale(true); setError((e as Error).message); setPreview(null);
    } finally { if (ticket === sequence.current) setLoading(false); }
  }, [archived]);

  function reload() { setLoading(true); void load(query); }

  useEffect(() => {
    const ticket = ++sequence.current;
    void api<{ items: Project[]; total: number }>("projects?limit=100").then(result => {
      if (ticket !== sequence.current) return;
      setProjects(result.items); setUpdated(new Date().toLocaleTimeString("zh-CN")); setStale(false);
    }).catch(e => {
      if (ticket !== sequence.current) return;
      setStale(true); setError((e as Error).message);
    }).finally(() => { if (ticket === sequence.current) setLoading(false); });
    void api<{ allowed_roots: string[]; default_path: string; model_enabled: boolean }>("settings").then(data => {
      setModelEnabled(data.model_enabled);
      setRoots(data.allowed_roots); setForm(current => ({ ...current, path: current.path || data.allowed_roots[0] || "" }));
    }).catch(e => setError((e as Error).message));
    return () => { sequence.current += 1; };
  }, []);

  function change(key: keyof Form, value: string) {
    setForm(current => ({ ...current, [key]: value })); setPreview(null); setNotice("");
  }
  function selectMode(value: "create" | "import") {
    setMode(value); setPreview(null); setNotice(""); setError("");
    setForm(current => ({ ...current, path: value === "import" ? roots[0] ?? "" : `${roots[0] ?? ""}/新产品` }));
  }
  async function makePreview(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setError(""); setNotice(""); setPreview(null);
    try { setPreview(await api<Preview>(`projects/${mode}-preview`, { method: "POST", body: JSON.stringify(form) })); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  async function confirm() {
    if (!preview) return;
    setBusy(true); setError("");
    try {
      const project = await api<Project>("projects/confirm", { method: "POST", headers: { "Idempotency-Key": preview.preview_id }, body: JSON.stringify({ preview_id: preview.preview_id, expected_hash: preview.content_hash }) });
      setNotice(`「${project.name}」已登记，刷新后仍可查看。`); setPreview(null); setSelected(project); await load(query);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }

  return <div className="shell">
    <header className="topbar workbench-topbar"><Link className="brand" href="/"><span className="brandmark">工</span>产品工厂</Link><Link href="/settings/provider">AI 提供方与能力 →</Link><span className="local"><span />本机工作台</span></header>
    <main>
      <div className="intro"><div><p className="eyebrow">PRODUCT FACTORY</p><h1>你的产品工作台</h1><p className="subtitle">把项目与资料放在一起，清楚地接着做下一步。</p></div><div className="scope"><strong>项目登记与需求整理</strong><span>{modelEnabled ? "模型已配置 · 分析需资料授权" : "当前范围 · 模型未启用"}</span></div></div>
      {error && <div role="alert" className="message error">{error}<button onClick={reload}>重新读取</button></div>}
      {notice && <div role="status" className="message success">{notice}</div>}
      <div className="workspace">
        <section className="projects panel" aria-labelledby="projects-title">
          <div className="section-head"><div><p className="eyebrow">01 / 项目</p><h2 id="projects-title">我的产品 <span className="count">{projects.length}</span></h2></div><button className="quiet" onClick={reload} disabled={loading}>刷新列表</button></div>
          <label className="archive-filter"><input type="checkbox" checked={archived} onChange={e=>{const value=e.target.checked;setArchived(value);setSelected(null);setLoading(true);void load(query,value);}}/>查看已归档项目（文件与历史保留）</label><form className="search" onSubmit={e => { e.preventDefault(); reload(); }}><label className="sr-only" htmlFor="search">搜索项目</label><input id="search" value={query} onChange={e => setQuery(e.target.value)} placeholder="按项目名称搜索" maxLength={60}/><button className="quiet" type="submit">搜索</button></form>
          <p className="list-meta">{loading ? "正在读取项目…" : stale ? `显示上次成功读取的数据 · ${updated || "尚无数据"}` : `最近读取 ${updated || "—"}`}</p>
          {!loading && !stale && projects.length === 0 && <div className="empty"><span className="empty-symbol">＋</span><h3>{query ? "没有匹配的项目" : archived ? "暂无归档项目" : "从你的第一个产品开始"}</h3><p>{query ? "换一个名称，再搜索一次。" : archived ? "归档保留文件和历史，取消归档后恢复可见性。" : "接入已有目录，或创建一个新项目。资料会先预览，再由你确认。"}</p></div>}
          <div className="project-list">{projects.map(project => <button className={`project-card ${selected?.id === project.id ? "selected" : ""}`} key={project.id} onClick={() => setSelected(project)}><div className="card-top"><span className="project-icon">{project.name.slice(0, 1)}</span><span className="badge">{project.archived?"已归档":"已登记"}</span></div><h3>{project.name}</h3><p>{project.core_task}</p><span className="path">{project.path}</span><div className="card-footer"><span>{new Date(project.created_at).toLocaleDateString("zh-CN")}</span><span>查看资料 →</span></div></button>)}</div>
          {selected && <div className="detail"><h3>{selected.name}</h3><dl><dt>目标用户</dt><dd>{selected.target_user}</dd><dt>核心任务</dt><dd>{selected.core_task}</dd><dt>说明</dt><dd>{selected.description || "未填写"}</dd><dt>目录</dt><dd className="path">{selected.path}</dd></dl><p>已登记表示项目信息已保存；执行与验收将在后续阶段提供。</p><Link className="planning-entry" href={`/projects/${selected.id}/backups`}>备份与归档 →</Link><Link className="primary" href={`/projects/${selected.id}`}>整理需求与确认基线 →</Link></div>}
        </section>
        <section className="registration panel" aria-labelledby="register-title">
          <div className="section-head"><div><p className="eyebrow">02 / 登记</p><h2 id="register-title">添加一个产品</h2></div></div>
          <div className="tabs" role="group" aria-label="登记方式"><button type="button" aria-pressed={mode === "import"} onClick={() => selectMode("import")} disabled={busy}>接入已有目录</button><button type="button" aria-pressed={mode === "create"} onClick={() => selectMode("create")} disabled={busy}>新建项目</button></div>
          <p className="hint">{mode === "import" ? "先读取资料清单，确认后保存项目信息。原目录保持不变。" : "先核对将创建的文件，确认后创建新目录。已有内容不会被覆盖。"}</p>
          <form onSubmit={makePreview}><fieldset disabled={busy}>
            <label htmlFor="name">产品名称 <span>必填</span></label><input id="name" value={form.name} onChange={e => change("name", e.target.value)} maxLength={60} required placeholder="例如：产品工厂"/>
            <label htmlFor="target">目标用户</label><input id="target" value={form.target_user} onChange={e => change("target_user", e.target.value)} maxLength={300} required placeholder="例如：管理多个项目的产品负责人"/>
            <label htmlFor="task">核心任务</label><textarea id="task" value={form.core_task} onChange={e => change("core_task", e.target.value)} maxLength={1000} required rows={2} placeholder="这个产品帮助用户完成什么？"/>
            <label htmlFor="path">{mode === "import" ? "已有目录" : "新项目目录"}</label><input id="path" value={form.path} onChange={e => change("path", e.target.value)} maxLength={4096} required spellCheck={false}/>
            <p className="root-hint">可用目录范围：{roots.length ? roots.join("；") : "正在读取…"}</p>
            <label htmlFor="description">补充说明 <span>选填</span></label><textarea id="description" value={form.description} onChange={e => change("description", e.target.value)} maxLength={2000} rows={2} placeholder="记录你希望保留的背景"/>
            <button className="primary" type="submit" disabled={busy || stale}>{busy ? "正在处理…" : "预览并核对 →"}</button>
          </fieldset></form>
          {preview && <section className="preview" aria-labelledby="preview-title"><h3 id="preview-title">{mode === "import" ? "资料预览" : "将创建的文件"}</h3><p>已识别 {preview.manifest.files.length} 项。请核对后确认。</p>{preview.manifest.warnings.map(text => <p className="warning" key={text}>{text}</p>)}<div className="file-list">{preview.manifest.files.map(file => <div className="file-row" key={file.path}><span className="path">{file.path}</span><span>{statuses[file.status] ?? file.status}</span></div>)}</div><button className="primary" type="button" onClick={() => void confirm()} disabled={busy || stale}>{busy ? "正在保存…" : mode === "import" ? "确认接入" : "确认创建"}</button></section>}
        </section>
      </div><footer>项目与需求保存在本机，模型分析需要明确的资料授权。</footer>
    </main>
  </div>;
}
