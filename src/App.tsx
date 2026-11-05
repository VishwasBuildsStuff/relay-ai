import { useCallback, useEffect, useState } from 'react';
import { Activity, ArrowRight, Check, CircleAlert, FileArchive, LoaderCircle, Save, ShieldCheck, Trash2, UploadCloud, X } from 'lucide-react';

type TimelineEvent = { timestamp: string; stage: string; status: string; actor?: string; correlation_id?: string | null; details: Record<string, unknown> };
type Task = {
  task_id: string; status: string; current_stage: string; bug_report: string; created_at: string;
  triage?: { title: string; severity: string; acceptance_criteria: string[]; rationale?: string };
  investigation_patch?: { buggy_file: string; root_cause: string; confidence_score: number; files_changed: string[]; unified_diff: string; summary_of_changes: string };
  sandbox_result?: { test_command: string; tests_passed: boolean; exit_code: number; stdout: string; stderr: string; isolation_mode: string; applied_patch_cleanly: boolean };
  reviewer?: { security_assessment: string; recommendation: string; review_summary: string; regression_risks: string[]; side_effects: string[] };
  error?: { failed_stage: string; message: string }; evidence_timeline: TimelineEvent[]; is_approved?: boolean;
  is_applied?: boolean; is_committed?: boolean; commit_hash?: string | null;
  commit_message?: string | null;
  repository_id?: string | null; repository_base_revision?: string | null; repository_result_revision?: string | null;
  repository_operation_state?: string | null;
};
type Scenario = { id: string; name: string; description: string; bug_report: string; codebase: Record<string, string> };
type Session = { actor: string; role: 'viewer' | 'operator' };
type RepositoryTarget = { repository_id: string; branch: string; revision: string; clean: boolean };
type ActionAudit = { action_id: number; actor: string; action: string; outcome: string; notes?: string | null; details: Record<string, unknown>; created_at: string };
const API = (import.meta.env.VITE_API_URL || 'http://localhost:8000').replace(/\/$/, '');

async function api<T>(path: string, init?: RequestInit, onResponse?: (response: Response) => void): Promise<T> {
  const headers = new Headers(init?.headers);
  const token = window.sessionStorage.getItem('relay-api-token');
  if (token) headers.set('Authorization', `Bearer ${token}`);
  const response = await fetch(`${API}${path}`, { ...init, headers });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `API request failed (${response.status})`);
  }
  onResponse?.(response);
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export default function App() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [task, setTask] = useState<Task | null>(null);
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [repositoryTargets, setRepositoryTargets] = useState<RepositoryTarget[]>([]);
  const [repositoryId, setRepositoryId] = useState('');
  const [report, setReport] = useState('');
  const [snapshot, setSnapshot] = useState('');
  const [archive, setArchive] = useState<File | null>(null);
  const [inputMode, setInputMode] = useState<'map' | 'zip'>('map');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [apiToken, setApiToken] = useState(() => window.sessionStorage.getItem('relay-api-token') || '');
  const [taskSearch, setTaskSearch] = useState('');
  const [taskStatus, setTaskStatus] = useState('');
  const [taskPageOffset, setTaskPageOffset] = useState(0);
  const [totalTasks, setTotalTasks] = useState(0);
  const [session, setSession] = useState<Session>({ actor: '', role: 'viewer' });
  const [actions, setActions] = useState<ActionAudit[]>([]);
  const [totalActions, setTotalActions] = useState(0);
  const [actionPageOffset, setActionPageOffset] = useState(0);
  const [events, setEvents] = useState<TimelineEvent[]>([]);
  const [totalEvents, setTotalEvents] = useState(0);
  const [eventPageOffset, setEventPageOffset] = useState(0);
  const [reviewNotes, setReviewNotes] = useState('');
  const [commitMessage, setCommitMessage] = useState('');

  const refresh = useCallback(async () => {
    try {
      const params = new URLSearchParams({ limit: '50', offset: String(taskPageOffset) });
      if (taskSearch.trim()) params.set('q', taskSearch.trim());
      if (taskStatus) params.set('status', taskStatus);
      const selectedId = task?.task_id;
      const [list, demos, selectedDetail, selectedActions, selectedEvents, currentSession] = await Promise.all([
        api<Task[]>(`/api/v1/tasks?${params}`, undefined, (response) => setTotalTasks(Number(response.headers.get('X-Total-Count') || 0))),
        api<Scenario[]>('/api/v1/scenarios'),
        selectedId ? api<Task>(`/api/v1/tasks/${selectedId}`).catch(() => null) : Promise.resolve(null),
        selectedId ? api<ActionAudit[]>(`/api/v1/tasks/${selectedId}/actions?limit=50&offset=${actionPageOffset}`, undefined, (response) => setTotalActions(Number(response.headers.get('X-Total-Count') || 0))).catch(() => { setTotalActions(0); return []; }) : Promise.resolve([]),
        selectedId ? api<TimelineEvent[]>(`/api/v1/tasks/${selectedId}/events?limit=50&offset=${eventPageOffset}`, undefined, (response) => setTotalEvents(Number(response.headers.get('X-Total-Count') || 0))).catch(() => { setTotalEvents(0); return []; }) : Promise.resolve([]),
        api<Session>('/api/v1/session'),
      ]);
      setTasks(list); setScenarios(demos);
      setSession(currentSession);
      setActions(selectedActions);
      setEvents(selectedEvents);
      setTask((current) => current ? (list.find((item) => item.task_id === current.task_id) || selectedDetail || current) : current);
      setError('');
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not reach the API.'); }
  }, [taskSearch, taskStatus, taskPageOffset, task?.task_id, eventPageOffset, actionPageOffset]);
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    if (!session.actor) return;
    let active = true;
    void api<RepositoryTarget[]>('/api/v1/repositories').then((targets) => { if (active) setRepositoryTargets(targets); }).catch(() => { if (active) setRepositoryTargets([]); });
    return () => { active = false; };
  }, [session.actor]);
  useEffect(() => {
    if (!task || ['AWAITING_HUMAN_APPROVAL', 'APPROVED', 'APPLIED', 'COMMITTED', 'REJECTED', 'FAILED', 'CANCELLED'].includes(task.status)) return;
    const timer = window.setInterval(() => { void refresh(); }, 1800);
    return () => window.clearInterval(timer);
  }, [task?.task_id, task?.status, refresh]);

  async function submit() {
    setBusy(true); setError(''); setMessage('');
    try {
      let created: Task;
      if (inputMode === 'zip' && archive) {
        const body = new FormData(); body.append('bug_report', report); body.append('file', archive);
        if (repositoryId) body.append('repository_id', repositoryId);
        created = await api<Task>('/api/v1/tasks/upload', { method: 'POST', body });
      } else {
        const codebase = JSON.parse(snapshot) as Record<string, string>;
        created = await api<Task>('/api/v1/tasks', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ chat_message: report, codebase, repository_id: repositoryId || null }) });
      }
      setTask(created); setActions([]); setTotalActions(0); setActionPageOffset(0); setEvents([]); setTotalEvents(0); setEventPageOffset(0); setReviewNotes(''); setCommitMessage(''); setMessage('Task queued. Progress and evidence will appear here.'); await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Submission failed.'); }
    finally { setBusy(false); }
  }

  async function act(action: 'approve' | 'reject' | 'apply' | 'commit' | 'cancel' | 'retry') {
    if (!task) return;
    setBusy(true); setError('');
    const keySlot = `relay-action-key:${task.task_id}:${action}`;
    const idempotencyKey = window.sessionStorage.getItem(keySlot) || crypto.randomUUID();
    window.sessionStorage.setItem(keySlot, idempotencyKey);
    try {
      const effectiveCommitMessage = task.repository_operation_state === 'COMMITTING' ? task.commit_message?.trim() || '' : commitMessage.trim();
      const payload = { action, ...(['approve', 'reject'].includes(action) && reviewNotes.trim() ? { notes: reviewNotes.trim() } : {}), ...(action === 'commit' && effectiveCommitMessage ? { commit_message: effectiveCommitMessage } : {}) };
      const updated = await api<Task>(`/api/v1/tasks/${task.task_id}/actions`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey }, body: JSON.stringify(payload) });
      window.sessionStorage.removeItem(keySlot);
      setTask(updated);
      setMessage(({ approve: 'Patch approved.', reject: 'Patch rejected.', apply: updated.repository_id ? `Patch applied to ${updated.repository_id}.` : 'Patch applied to its task workspace.', commit: `Created Git commit ${updated.commit_hash || ''}.`, cancel: 'Cancellation requested.', retry: 'Task queued for retry.' })[action]);
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Action failed.'); }
    finally { setBusy(false); }
  }

  async function deleteTask(item: Task) {
    const title = item.triage?.title || item.bug_report.slice(0, 90);
    if (!window.confirm(`Permanently delete “${title}”, its evidence, uploaded source snapshot, and task workspace?`)) return;
    setBusy(true); setError(''); setMessage('');
    try {
      await api<void>(`/api/v1/tasks/${item.task_id}`, { method: 'DELETE' });
      if (task?.task_id === item.task_id) setTask(null);
      setMessage('Task and its stored source data were deleted.');
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Task deletion failed.'); }
    finally { setBusy(false); }
  }

  function loadScenario(item: Scenario) { setReport(item.bug_report); setSnapshot(JSON.stringify(item.codebase, null, 2)); setArchive(null); setInputMode('map'); setRepositoryId(''); setMessage(`${item.name} loaded.`); }
  function saveToken() { if (apiToken.trim()) window.sessionStorage.setItem('relay-api-token', apiToken.trim()); else window.sessionStorage.removeItem('relay-api-token'); void refresh(); }
  async function downloadPatch() {
    if (!task?.investigation_patch) return;
    try {
      const headers = new Headers();
      const token = window.sessionStorage.getItem('relay-api-token');
      if (token) headers.set('Authorization', `Bearer ${token}`);
      const response = await fetch(`${API}/api/v1/tasks/${task.task_id}/patch`, { headers });
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
      <div className="side-caption recent-caption">TASKS <span>{totalTasks}</span></div>
      <div className="task-filters"><input aria-label="Search tasks" value={taskSearch} onChange={(e) => { setTaskSearch(e.target.value); setTaskPageOffset(0); }} placeholder="Search tasks"/><select aria-label="Filter tasks by status" value={taskStatus} onChange={(e) => { setTaskStatus(e.target.value); setTaskPageOffset(0); }}><option value="">All statuses</option>{['RECEIVED','QUEUED','RUNNING','TRIAGING','TRIAGED','INVESTIGATING','INVESTIGATED','PATCHED','SANDBOX_RUNNING','SANDBOX_TESTED','REVIEWING','REVIEWED','AWAITING_HUMAN_APPROVAL','APPROVED','APPLIED','COMMITTED','REJECTED','CANCELLED','FAILED'].map((s) => <option key={s} value={s}>{s.replaceAll('_',' ')}</option>)}</select></div>
      <div className="task-list">{tasks.map((item) => <div key={item.task_id} className="task-row"><button className={`task-link ${task?.task_id === item.task_id ? 'selected' : ''}`} onClick={() => { setTask(item); setActions([]); setTotalActions(0); setActionPageOffset(0); setEvents([]); setTotalEvents(0); setEventPageOffset(0); setReviewNotes(''); setCommitMessage(''); }}><i className={`dot ${item.status.toLowerCase()}`}/><span><b>{item.triage?.title || item.bug_report}</b><small>{item.status.replaceAll('_', ' ')}</small></span></button>{session.role === 'operator' && <button className="task-delete" aria-label={`Delete task ${item.triage?.title || item.task_id}`} title="Delete task and stored source" disabled={busy} onClick={() => void deleteTask(item)}><Trash2 size={13}/></button>}</div>)}{!tasks.length && <p className="side-empty">Submitted tasks will appear here.</p>}</div>
      <div className="task-pagination"><button disabled={taskPageOffset === 0} onClick={() => setTaskPageOffset(Math.max(0, taskPageOffset - 50))}>Newer</button><span>{totalTasks ? `${taskPageOffset + 1}–${Math.min(taskPageOffset + tasks.length, totalTasks)}` : '0 tasks'}</span><button disabled={taskPageOffset + 50 >= totalTasks} onClick={() => setTaskPageOffset(taskPageOffset + 50)}>Older</button></div>
      <div className="side-bottom"><i/> API <span>{API.replace(/^https?:\/\//, '')}</span></div>
    </aside>
    <main className="main">
      <header className="topbar"><span>RELAY AI <b>/</b> Autonomous pipeline</span><div className="top-actions"><small className="session-badge">{session.actor ? `${session.actor} · ${session.role}` : 'Connecting'}</small><input aria-label="Optional API token" type="password" value={apiToken} onChange={(e) => setApiToken(e.target.value)} placeholder="API token (optional)"/><button onClick={saveToken} aria-label="Save API token"><Save size={13}/><span>Save</span></button><button onClick={() => void refresh()} aria-label="Refresh tasks"><Activity size={14}/><span>Refresh</span></button></div></header>
      <div className="content">
        <div className="heading"><div><small>ENGINEERING AUTOMATION</small><h1>Issue remediation</h1><p>Turn a bug report into a reviewed patch with test evidence and a human decision.</p></div><span className="pipeline-label"><i/> 3-stage pipeline</span></div>
        <label className="mobile-task-picker">Recent task<select aria-label="Select a recent task" value={task?.task_id || ''} onChange={(event) => { setTask(tasks.find((item) => item.task_id === event.target.value) || null); setActions([]); setTotalActions(0); setActionPageOffset(0); setEvents([]); setTotalEvents(0); setEventPageOffset(0); setReviewNotes(''); setCommitMessage(''); }}><option value="">Select a task</option>{tasks.map((item) => <option key={item.task_id} value={item.task_id}>{item.triage?.title || item.bug_report.slice(0, 70)} · {item.status.replaceAll('_', ' ')}</option>)}</select></label>
        <div className="mobile-task-filters"><input aria-label="Search tasks" value={taskSearch} onChange={(e) => { setTaskSearch(e.target.value); setTaskPageOffset(0); }} placeholder="Search tasks"/><select aria-label="Filter tasks by status" value={taskStatus} onChange={(e) => { setTaskStatus(e.target.value); setTaskPageOffset(0); }}><option value="">All statuses</option>{['RECEIVED','QUEUED','RUNNING','TRIAGING','TRIAGED','INVESTIGATING','INVESTIGATED','PATCHED','SANDBOX_RUNNING','SANDBOX_TESTED','REVIEWING','REVIEWED','AWAITING_HUMAN_APPROVAL','APPROVED','APPLIED','COMMITTED','REJECTED','CANCELLED','FAILED'].map((s) => <option key={s} value={s}>{s.replaceAll('_',' ')}</option>)}</select></div>
        <div className="mobile-task-pagination"><button disabled={taskPageOffset === 0} onClick={() => setTaskPageOffset(Math.max(0, taskPageOffset - 50))}>Newer tasks</button><span>{totalTasks ? `${taskPageOffset + 1}–${Math.min(taskPageOffset + tasks.length, totalTasks)} of ${totalTasks}` : 'No tasks'}</span><button disabled={taskPageOffset + 50 >= totalTasks} onClick={() => setTaskPageOffset(taskPageOffset + 50)}>Older tasks</button></div>
        {(error || message) && <div className={`notice ${error ? 'notice-error' : ''}`} role={error ? 'alert' : 'status'}><span>{error || message}</span><button aria-label="Dismiss notification" onClick={() => { setError(''); setMessage(''); }}><X size={15}/></button></div>}
        <div className={`columns ${session.role === 'viewer' ? 'viewer-columns' : ''}`}>
          {session.role === 'operator' && <section className="panel submit-panel"><div className="panel-heading"><i>01</i><div><h2>New remediation task</h2><p>Describe the defect and provide its codebase.</p></div></div>
            <label className="field-label" htmlFor="report">BUG REPORT</label><textarea id="report" className="report" maxLength={20000} value={report} onChange={(e) => setReport(e.target.value)} placeholder="What is broken? Include expected and actual behavior, errors, and relevant context."/>
            <label className="field-label" htmlFor="repository-target">APPLY TARGET · OPTIONAL</label><select id="repository-target" className="action-input" value={repositoryId} onChange={(e) => setRepositoryId(e.target.value)}><option value="">Task-owned copy (default)</option>{repositoryTargets.map((target) => <option key={target.repository_id} value={target.repository_id} disabled={!target.clean}>{target.repository_id} · {target.branch || 'detached'} · {target.revision.slice(0, 10)}{target.clean ? '' : ' · has local changes'}</option>)}</select>{!repositoryTargets.length && <small className="target-help">No local Git targets are configured. The default uses an isolated task-owned copy.</small>}
            <div className="divider">CODEBASE INPUT</div>
            <div className="input-options" role="group" aria-labelledby="codebase-input-label"><span id="codebase-input-label" className="sr-only">Codebase input type</span><label className={inputMode === 'map' ? 'option active-option' : 'option'}><input type="radio" name="codebase-input" checked={inputMode === 'map'} onChange={() => { setInputMode('map'); setArchive(null); }}/><UploadCloud size={18}/><span><b>Paste file map</b><small>JSON paths and contents</small></span></label><label className={inputMode === 'zip' ? 'option active-option' : 'option'}><input type="radio" name="codebase-input" checked={inputMode === 'zip'} onChange={() => setInputMode('zip')}/><FileArchive size={18}/><span><b>Upload ZIP</b><small>{archive?.name || 'Python repository'}</small></span></label></div>
            {inputMode === 'map' ? <textarea aria-label="Repository file map as JSON" className="snapshot" value={snapshot} onChange={(e) => setSnapshot(e.target.value)} spellCheck={false} placeholder={'{\n  "calculator.py": "def add(a, b): return a + b\\n",\n  "test_calculator.py": "..."\n}'}/> : <div className="archive-picker"><label htmlFor="archive">ZIP archive</label><input key={inputMode} id="archive" type="file" accept=".zip" onChange={(e) => setArchive(e.target.files?.[0] || null)}/><small>Choose a repository archive up to 25 MiB.</small></div>}
            <div className="submit-footer"><span>1.5 MB source context · up to 500 files</span><button className="primary" disabled={busy || report.trim().length < 5 || (inputMode === 'map' ? !snapshot.trim() : !archive)} onClick={() => void submit()}>{busy ? <><LoaderCircle className="spin" size={16}/> Working</> : <>Run pipeline <ArrowRight size={16}/></>}</button></div>
            <div className="demos"><small>LOAD A DEMO SCENARIO</small>{scenarios.map((item) => <button key={item.id} onClick={() => loadScenario(item)}>{item.name.replace(/^Scenario \d+: /, '')}<ArrowRight size={13}/></button>)}</div>
          </section>}
          <section className="panel results-panel">{!task ? <div className="no-task"><span><Activity size={25}/></span><h2>Your pipeline results</h2><p>Submit a report to see triage, patch details, test evidence, and reviewer findings here.</p><div>01 Triage　·　02 Investigate　·　03 Review</div></div> : <>
            <div className="panel-heading"><i>02</i><div><h2>Task review</h2><p className="task-id">{task.task_id}</p>{task.repository_id && <p className="task-id">Target {task.repository_id} · base {task.repository_base_revision?.slice(0, 12)}</p>}</div><span className="badge">{task.status.replaceAll('_', ' ')}</span></div>
            {task.error && <div className="error-card"><CircleAlert size={18}/><span><b>{task.error.failed_stage}</b><br/>{task.error.message}</span></div>}
            {task.triage && <div className="result-section"><div className="result-title"><h3>{task.triage.title}</h3><span className="severity">{task.triage.severity}</span></div><p>{task.triage.rationale}</p><b>Acceptance criteria</b>{task.triage.acceptance_criteria.map((item, i) => <div className="criterion" key={i}><Check size={14}/>{item}</div>)}</div>}
            {task.investigation_patch && <div className="result-section"><small className="kicker">ROOT CAUSE · {Math.round(task.investigation_patch.confidence_score * 100)}% CONFIDENCE</small><p>{task.investigation_patch.root_cause}</p><b>Changed files</b><div className="file-tags">{task.investigation_patch.files_changed.map((file) => <code key={file}>{file}</code>)}</div><details><summary>View unified diff</summary><pre>{task.investigation_patch.unified_diff}</pre></details><button className="patch-download" onClick={() => void downloadPatch()}>Download patch file</button></div>}
            {task.sandbox_result && <div className="evidence"><div><b><Activity size={15}/> Sandbox execution</b><span className={task.sandbox_result.tests_passed ? 'pass' : 'fail'}>{task.sandbox_result.tests_passed ? 'TESTS PASSED' : 'TESTS FAILED'}</span></div><small>Exit {task.sandbox_result.exit_code} · {task.sandbox_result.isolation_mode} · Patch {task.sandbox_result.applied_patch_cleanly ? 'applied' : 'rejected'}</small><code className="test-command">{task.sandbox_result.test_command}</code><details><summary>Execution output</summary>{task.sandbox_result.stdout && <section><b>stdout</b><pre>{task.sandbox_result.stdout}</pre></section>}{task.sandbox_result.stderr && <section><b>stderr</b><pre>{task.sandbox_result.stderr}</pre></section>}{!task.sandbox_result.stdout && !task.sandbox_result.stderr && <p>No output captured.</p>}</details></div>}
            {task.reviewer && <div className="review"><b><ShieldCheck size={16}/> AI reviewer <span>{task.reviewer.security_assessment} · {task.reviewer.recommendation}</span></b><p>{task.reviewer.review_summary}</p>{task.reviewer.regression_risks.map((risk, i) => <div className="criterion risk" key={i}><CircleAlert size={14}/>{risk}</div>)}</div>}
            {session.role === 'operator' && task.status === 'AWAITING_HUMAN_APPROVAL' && <><label className="field-label" htmlFor="review-notes">REVIEW NOTES · OPTIONAL</label><textarea id="review-notes" className="action-input" maxLength={2000} value={reviewNotes} onChange={(e) => { setReviewNotes(e.target.value); window.sessionStorage.removeItem(`relay-action-key:${task.task_id}:approve`); window.sessionStorage.removeItem(`relay-action-key:${task.task_id}:reject`); }} placeholder="Record the reason for your decision."/><div className="decisions"><button disabled={busy} onClick={() => void act('reject')}><X size={15}/> Reject</button><button disabled={busy} onClick={() => void act('approve')}><Check size={15}/> Approve patch</button></div></>}
            {session.role === 'operator' && !['AWAITING_HUMAN_APPROVAL', 'APPROVED', 'APPLIED', 'COMMITTED', 'REJECTED', 'CANCELLED', 'FAILED'].includes(task.status) && <div className="decisions"><button disabled={busy} onClick={() => void act('cancel')}><X size={15}/> Cancel task</button></div>}
            {session.role === 'operator' && task.status === 'FAILED' && <div className="decisions"><button disabled={busy} onClick={() => void act('retry')}>Retry task</button></div>}
            {session.role === 'operator' && task.is_approved && !task.is_applied && <div className="decisions"><button disabled={busy} onClick={() => void act('apply')}><Check size={15}/> Apply to {task.repository_id || 'task workspace'}</button></div>}
            {session.role === 'operator' && task.is_applied && !task.is_committed && <><label className="field-label" htmlFor="commit-message">COMMIT MESSAGE · OPTIONAL</label><input id="commit-message" className="action-input" maxLength={200} readOnly={task.repository_operation_state === 'COMMITTING'} value={task.repository_operation_state === 'COMMITTING' ? task.commit_message || '' : commitMessage} onChange={(e) => { setCommitMessage(e.target.value); window.sessionStorage.removeItem(`relay-action-key:${task.task_id}:commit`); }} placeholder="Uses a generated message when blank."/>{task.repository_operation_state === 'COMMITTING' && <small className="target-help">Resuming the pending commit with its recorded message.</small>}<div className="decisions"><span className="pending">Patch applied to {task.repository_id || 'isolated task copy'}.</span><button disabled={busy} onClick={() => void act('commit')}><Check size={15}/>{task.repository_operation_state === 'COMMITTING' ? 'Resume commit' : 'Commit'}</button></div></>}
            {task.is_committed && task.commit_hash && <div className="commit-record"><b>Commit {task.commit_hash.slice(0, 12)}</b><code>{task.commit_message}</code></div>}
            {task.is_committed && <div className="pending">Committed to {task.repository_id || 'task copy'} · {task.commit_hash}</div>}
            <div className="timeline"><small className="kicker">ACTIVITY · {totalEvents ? `${Math.max(1, totalEvents - eventPageOffset - events.length + 1)}–${totalEvents - eventPageOffset} OF ${totalEvents} EVENTS` : '0 EVENTS'}</small>{events.map((event, i) => <div key={`${event.timestamp}-${event.stage}-${i}`}><i/><span><b>{event.stage.replaceAll('_', ' ')}</b><small>{event.status} · {event.actor || 'system'} · {new Date(event.timestamp).toLocaleString()}</small>{event.stage === 'MODEL_USAGE' && <div className="model-usage"><small>{String(event.details.provider || 'provider')} · {String(event.details.model || 'unknown model')}</small><small>{String(event.details.prompt_tokens ?? 0)} prompt · {String(event.details.candidate_tokens ?? 0)} generated · {String(event.details.total_tokens ?? 0)} total tokens</small>{event.details.estimated_cost_usd != null && <small>Estimated ${String(event.details.estimated_cost_usd)} · {String(event.details.pricing_label || 'operator pricing')}</small>}</div>}</span></div>)}{!events.length && <p className="side-empty">No activity events on this page.</p>}<div className="event-pagination"><button disabled={eventPageOffset === 0} onClick={() => setEventPageOffset(Math.max(0, eventPageOffset - 50))}>Newer events</button><button disabled={eventPageOffset + events.length >= totalEvents} onClick={() => setEventPageOffset(eventPageOffset + 50)}>Older events</button></div></div>
            <div className="timeline action-audit"><small className="kicker">OPERATOR ACTION AUDIT · {totalActions ? `${Math.max(1, totalActions - actionPageOffset - actions.length + 1)}–${totalActions - actionPageOffset} OF ${totalActions} RECORDS` : '0 RECORDS'}</small>{actions.map((entry) => <div key={entry.action_id}><i/><span><b>{entry.action.replaceAll('_', ' ')} · {entry.outcome}</b><small>{entry.actor} · {new Date(entry.created_at).toLocaleString()}</small>{entry.notes && <p>{entry.notes}</p>}<details><summary>Action details</summary><pre>{JSON.stringify(entry.details, null, 2)}</pre></details></span></div>)}{!actions.length && <p className="side-empty">No operator actions recorded.</p>}<div className="event-pagination"><button disabled={actionPageOffset === 0} onClick={() => setActionPageOffset(Math.max(0, actionPageOffset - 50))}>Newer actions</button><button disabled={actionPageOffset + actions.length >= totalActions} onClick={() => setActionPageOffset(actionPageOffset + 50)}>Older actions</button></div></div>
          </>}</section>
        </div>
        <footer>RELAY AI · Human review remains required for every patch.<span>Local workspace</span></footer>
      </div>
    </main>
  </div>;
}
