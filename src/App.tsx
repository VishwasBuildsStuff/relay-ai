import { useCallback, useEffect, useState } from 'react';
import { Activity, ArrowRight, Check, CircleAlert, FileArchive, LoaderCircle, ShieldCheck, UploadCloud, X } from 'lucide-react';

type TimelineEvent = { timestamp: string; stage: string; status: string; details: Record<string, unknown> };
type Task = {
  task_id: string; status: string; current_stage: string; bug_report: string; created_at: string;
  triage?: { title: string; severity: string; acceptance_criteria: string[]; rationale?: string };
  investigation_patch?: { buggy_file: string; root_cause: string; confidence_score: number; files_changed: string[]; unified_diff: string; summary_of_changes: string };
  sandbox_result?: { tests_passed: boolean; exit_code: number; stdout: string; stderr: string; isolation_mode: string; applied_patch_cleanly: boolean };
  reviewer?: { security_assessment: string; recommendation: string; review_summary: string; regression_risks: string[]; side_effects: string[] };
  error?: { failed_stage: string; message: string }; evidence_timeline: TimelineEvent[]; is_approved?: boolean;
  is_applied?: boolean; is_committed?: boolean; commit_hash?: string | null;
};
type Scenario = { id: string; name: string; description: string; bug_report: string; codebase: Record<string, string> };
const API = (import.meta.env.VITE_API_URL || 'http://localhost:8000').replace(/\/$/, '');

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  const token = window.sessionStorage.getItem('relay-api-token');
  if (token) headers.set('Authorization', `Bearer ${token}`);
  const response = await fetch(`${API}${path}`, { ...init, headers });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `API request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export default function App() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [task, setTask] = useState<Task | null>(null);
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [report, setReport] = useState('');
  const [snapshot, setSnapshot] = useState('');
  const [archive, setArchive] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [apiToken, setApiToken] = useState(() => window.sessionStorage.getItem('relay-api-token') || '');

  const refresh = useCallback(async () => {
    try {
      const [list, demos] = await Promise.all([api<Task[]>('/tasks?limit=50'), api<Scenario[]>('/scenarios')]);
      setTasks(list); setScenarios(demos);
      setTask((current) => current ? (list.find((item) => item.task_id === current.task_id) || current) : current);
      setError('');
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not reach the API.'); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    if (!task || ['AWAITING_HUMAN_APPROVAL', 'APPROVED', 'APPLIED', 'COMMITTED', 'REJECTED', 'FAILED', 'CANCELLED'].includes(task.status)) return;
    const timer = window.setInterval(() => { void refresh(); }, 1800);
    return () => window.clearInterval(timer);
  }, [task?.task_id, task?.status, refresh]);

  async function submit() {
    setBusy(true); setError(''); setMessage('');
    try {
      let created: Task;
      if (archive) {
        const body = new FormData(); body.append('bug_report', report); body.append('file', archive);
        created = await api<Task>('/tasks/submit', { method: 'POST', body });
      } else {
        const codebase = JSON.parse(snapshot) as Record<string, string>;
        created = await api<Task>('/tasks/report', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ chat_message: report, codebase }) });
      }
      setTask(created); setMessage('Task queued. Progress and evidence will appear here.'); await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Submission failed.'); }
    finally { setBusy(false); }
  }

  async function act(action: 'approve' | 'reject' | 'apply' | 'commit' | 'cancel' | 'retry') {
    if (!task) return;
    setBusy(true); setError('');
    try {
      const updated = await api<Task>(`/tasks/${task.task_id}/action`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action }) });
      setTask(updated);
      setMessage(({ approve: 'Patch approved.', reject: 'Patch rejected.', apply: 'Patch applied to its task workspace.', commit: `Created Git commit ${updated.commit_hash || ''}.`, cancel: 'Cancellation requested.', retry: 'Task queued for retry.' })[action]);
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Action failed.'); }
    finally { setBusy(false); }
  }

  function loadScenario(item: Scenario) { setReport(item.bug_report); setSnapshot(JSON.stringify(item.codebase, null, 2)); setArchive(null); setMessage(`${item.name} loaded.`); }
  function saveToken() { if (apiToken.trim()) window.sessionStorage.setItem('relay-api-token', apiToken.trim()); else window.sessionStorage.removeItem('relay-api-token'); void refresh(); }
  async function downloadPatch() {
    if (!task?.investigation_patch) return;
    try {
      const headers = new Headers();
      const token = window.sessionStorage.getItem('relay-api-token');
      if (token) headers.set('Authorization', `Bearer ${token}`);
      const response = await fetch(`${API}/tasks/${task.task_id}/patch`, { headers });
      if (!response.ok) throw new Error(`Patch download failed (${response.status})`);
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a'); link.href = url; link.download = `relay-${task.task_id}.patch`; link.click();
      URL.revokeObjectURL(url);
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Patch download failed.'); }
  }

  return <div className="layout">
    <aside className="sidebar">
      <div className="brand"><span className="brand-icon">R</span><div><b>relay<span>.ai</span></b><small>ENGINEERING PIPELINE</small></div></div>
      <div className="side-caption">WORKSPACE</div><div className="nav-current"><Activity size={16}/> Pipeline</div>
      <div className="side-caption recent-caption">RECENT TASKS <span>{tasks.length}</span></div>
      <div className="task-list">{tasks.map((item) => <button key={item.task_id} className={`task-link ${task?.task_id === item.task_id ? 'selected' : ''}`} onClick={() => setTask(item)}><i className={`dot ${item.status.toLowerCase()}`}/><span><b>{item.triage?.title || item.bug_report}</b><small>{item.status.replaceAll('_', ' ')}</small></span></button>)}{!tasks.length && <p className="side-empty">Submitted tasks will appear here.</p>}</div>
      <div className="side-bottom"><i/> API <span>{API.replace(/^https?:\/\//, '')}</span></div>
    </aside>
    <main className="main">
      <header className="topbar"><span>RELAY AI <b>/</b> Autonomous pipeline</span><div className="top-actions"><input aria-label="Optional API token" type="password" value={apiToken} onChange={(e) => setApiToken(e.target.value)} placeholder="API token (optional)"/><button onClick={saveToken}>Save token</button><button onClick={() => void refresh()}><Activity size={14}/> Refresh</button></div></header>
      <div className="content">
        <div className="heading"><div><small>ENGINEERING AUTOMATION</small><h1>Issue remediation</h1><p>Turn a bug report into a reviewed patch with test evidence and a human decision.</p></div><span className="pipeline-label"><i/> 3-stage pipeline</span></div>
        {(error || message) && <div className={`notice ${error ? 'notice-error' : ''}`}><span>{error || message}</span><button onClick={() => { setError(''); setMessage(''); }}><X size={15}/></button></div>}
        <div className="columns">
          <section className="panel submit-panel"><div className="panel-heading"><i>01</i><div><h2>New remediation task</h2><p>Describe the defect and provide its codebase.</p></div></div>
            <label className="field-label" htmlFor="report">BUG REPORT</label><textarea id="report" className="report" value={report} onChange={(e) => setReport(e.target.value)} placeholder="What is broken? Include expected and actual behavior, errors, and relevant context."/>
            <div className="divider">CODEBASE INPUT</div>
            <div className="input-options"><label className={!archive ? 'option active-option' : 'option'}><input type="radio" checked={!archive} onChange={() => setArchive(null)}/><UploadCloud size={18}/><span><b>Paste file map</b><small>JSON paths and contents</small></span></label><label className={archive ? 'option active-option' : 'option'}><input type="radio" checked={!!archive} onChange={() => document.getElementById('archive')?.click()}/><FileArchive size={18}/><span><b>Upload ZIP</b><small>{archive?.name || 'Python repository'}</small></span><input id="archive" hidden type="file" accept=".zip" onChange={(e) => setArchive(e.target.files?.[0] || null)}/></label></div>
            {!archive && <textarea className="snapshot" value={snapshot} onChange={(e) => setSnapshot(e.target.value)} spellCheck={false} placeholder={'{\n  "calculator.py": "def add(a, b): return a + b\\n",\n  "test_calculator.py": "..."\n}'}/>}
            <div className="submit-footer"><span>25 MiB · up to 500 files</span><button className="primary" disabled={busy || report.trim().length < 5 || (!archive && !snapshot.trim())} onClick={() => void submit()}>{busy ? <><LoaderCircle className="spin" size={16}/> Working</> : <>Run pipeline <ArrowRight size={16}/></>}</button></div>
            <div className="demos"><small>LOAD A DEMO SCENARIO</small>{scenarios.map((item) => <button key={item.id} onClick={() => loadScenario(item)}>{item.name.replace(/^Scenario \d+: /, '')}<ArrowRight size={13}/></button>)}</div>
          </section>
          <section className="panel results-panel">{!task ? <div className="no-task"><span><Activity size={25}/></span><h2>Your pipeline results</h2><p>Submit a report to see triage, patch details, test evidence, and reviewer findings here.</p><div>01 Triage　·　02 Investigate　·　03 Review</div></div> : <>
            <div className="panel-heading"><i>02</i><div><h2>Task review</h2><p className="task-id">{task.task_id}</p></div><span className="badge">{task.status.replaceAll('_', ' ')}</span></div>
            {task.error && <div className="error-card"><CircleAlert size={18}/><span><b>{task.error.failed_stage}</b><br/>{task.error.message}</span></div>}
            {task.triage && <div className="result-section"><div className="result-title"><h3>{task.triage.title}</h3><span className="severity">{task.triage.severity}</span></div><p>{task.triage.rationale}</p><b>Acceptance criteria</b>{task.triage.acceptance_criteria.map((item, i) => <div className="criterion" key={i}><Check size={14}/>{item}</div>)}</div>}
            {task.investigation_patch && <div className="result-section"><small className="kicker">ROOT CAUSE · {Math.round(task.investigation_patch.confidence_score * 100)}% CONFIDENCE</small><p>{task.investigation_patch.root_cause}</p><b>Changed files</b><div className="file-tags">{task.investigation_patch.files_changed.map((file) => <code key={file}>{file}</code>)}</div><details><summary>View unified diff</summary><pre>{task.investigation_patch.unified_diff}</pre></details><button className="patch-download" onClick={() => void downloadPatch()}>Download patch file</button></div>}
            {task.sandbox_result && <div className="evidence"><div><b><Activity size={15}/> Sandbox execution</b><span className={task.sandbox_result.tests_passed ? 'pass' : 'fail'}>{task.sandbox_result.tests_passed ? 'TESTS PASSED' : 'TESTS FAILED'}</span></div><small>Exit {task.sandbox_result.exit_code} · {task.sandbox_result.isolation_mode} · Patch {task.sandbox_result.applied_patch_cleanly ? 'applied' : 'rejected'}</small><details><summary>Execution output</summary><pre>{task.sandbox_result.stdout || task.sandbox_result.stderr}</pre></details></div>}
            {task.reviewer && <div className="review"><b><ShieldCheck size={16}/> AI reviewer <span>{task.reviewer.security_assessment} · {task.reviewer.recommendation}</span></b><p>{task.reviewer.review_summary}</p>{task.reviewer.regression_risks.map((risk, i) => <div className="criterion risk" key={i}><CircleAlert size={14}/>{risk}</div>)}</div>}
            {task.status === 'AWAITING_HUMAN_APPROVAL' && <div className="decisions"><button disabled={busy} onClick={() => void act('reject')}><X size={15}/> Reject</button><button disabled={busy} onClick={() => void act('approve')}><Check size={15}/> Approve patch</button></div>}
            {!['AWAITING_HUMAN_APPROVAL', 'APPROVED', 'APPLIED', 'COMMITTED', 'REJECTED', 'CANCELLED', 'FAILED'].includes(task.status) && <div className="decisions"><button disabled={busy} onClick={() => void act('cancel')}><X size={15}/> Cancel task</button></div>}
            {task.status === 'FAILED' && <div className="decisions"><button disabled={busy} onClick={() => void act('retry')}>Retry task</button></div>}
            {task.is_approved && !task.is_applied && <div className="decisions"><button disabled={busy} onClick={() => void act('apply')}><Check size={15}/> Apply to task workspace</button></div>}
            {task.is_applied && !task.is_committed && <div className="decisions"><span className="pending">Patch applied to isolated task copy.</span><button disabled={busy} onClick={() => void act('commit')}><Check size={15}/> Commit</button></div>}
            {task.is_committed && <div className="pending">Committed to task copy · {task.commit_hash}</div>}
            <div className="timeline"><small className="kicker">ACTIVITY · {task.evidence_timeline.length} EVENTS</small>{[...task.evidence_timeline].reverse().map((event, i) => <div key={`${event.timestamp}-${i}`}><i/><span><b>{event.stage.replaceAll('_', ' ')}</b><small>{event.status} · {new Date(event.timestamp).toLocaleString()}</small></span></div>)}</div>
          </>}</section>
        </div>
        <footer>RELAY AI · Human review remains required for every patch.<span>Local workspace</span></footer>
      </div>
    </main>
  </div>;
}
