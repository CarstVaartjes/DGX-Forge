import {useEffect, useState} from "react";
import type {ControlApi, JobDetail, JobSummary} from "../api/types";

const PAGE_SIZE = 20;
const MAX_TEXT = 160;
const MAX_MESSAGE = 512;
const MAX_PROGRESS = 1_000_000_000;

function bounded(value: string, maximum = MAX_TEXT): string {
  return value.length > maximum ? `${value.slice(0, maximum)}…` : value;
}

function progress(value: number): number {
  return Math.max(0, Math.min(MAX_PROGRESS, Number.isFinite(value) ? Math.trunc(value) : 0));
}

function errorText(value: unknown, fallback: string): string {
  const message = value instanceof Error ? value.message : fallback;
  return bounded(message, MAX_MESSAGE);
}

export function JobsPage({api}: {api: ControlApi}) {
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [jobNextCursor, setJobNextCursor] = useState<string>();
  const [jobTotal, setJobTotal] = useState(0);
  const [jobHistory, setJobHistory] = useState<{jobs: JobSummary[]; next?: string}[]>([]);
  const [jobPage, setJobPage] = useState(0);
  const [operationPage, setOperationPage] = useState(0);
  const [targetPage, setTargetPage] = useState(0);
  const [operationHistory, setOperationHistory] = useState<JobDetail["operations"][]>([]);
  const [operationCursors, setOperationCursors] = useState<(string | undefined)[]>([]);
  const [targetHistory, setTargetHistory] = useState<string[][]>([]);
  const [targetCursors, setTargetCursors] = useState<(string | undefined)[]>([]);
  const [selected, setSelected] = useState<JobDetail>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");

  useEffect(() => {
    let current = true;
    setLoading(true);
    api.jobs().then(result => {
      if (current) {
        setJobs(result.jobs);
        setJobNextCursor(result.next_cursor ?? undefined);
        setJobTotal(result.total ?? result.jobs.length);
        setJobHistory([{jobs: result.jobs, next: result.next_cursor ?? undefined}]);
      }
    }).catch(value => {
      if (current) setError(errorText(value, "Unable to load jobs"));
    }).finally(() => {
      if (current) setLoading(false);
    });
    return () => { current = false; };
  }, [api]);

  async function view(jobId: string) {
    setError("");
    setStatus("");
    setSelected(undefined);
    setOperationPage(0);
    setTargetPage(0);
    setLoading(true);
    try {
      const detail = await api.job(jobId);
      setSelected(detail);
      setOperationHistory([detail.operations]);
      setOperationCursors([detail.operation_next_cursor ?? undefined]);
      setTargetHistory([detail.targets]);
      setTargetCursors([detail.target_next_cursor ?? undefined]);
    } catch (value) {
      setError(errorText(value, "Unable to load job detail"));
    } finally {
      setLoading(false);
    }
  }

  async function resume() {
    if (!selected || selected.state !== "waiting-for-operator") return;
    setError("");
    setStatus("");
    setLoading(true);
    try {
      const resumed = await api.resumeJob(selected.id);
      setSelected(current => current ? {...current, state: resumed.state} : current);
      setJobs(current => current.map(job => job.id === resumed.id ? {...job, state: resumed.state} : job));
      setStatus(`Job ${bounded(resumed.id)} resumed in ${bounded(resumed.state)} state.`);
    } catch (value) {
      setError(errorText(value, "Unable to resume job"));
      const staleId = selected.id;
      setSelected(undefined);
      try {
        const authoritative = await api.job(staleId);
        setSelected(authoritative);
        setOperationPage(0);
        setTargetPage(0);
        setOperationHistory([authoritative.operations]);
        setOperationCursors([authoritative.operation_next_cursor ?? undefined]);
        setTargetHistory([authoritative.targets]);
        setTargetCursors([authoritative.target_next_cursor ?? undefined]);
        setJobs(current => current.map(job => job.id === staleId ? {...job, state: authoritative.state} : job));
      } catch {
        setSelected(undefined);
      }
    } finally {
      setLoading(false);
    }
  }

  const safeJobPage = Math.min(jobPage, Math.max(0, jobHistory.length - 1));
  const jobStart = safeJobPage * PAGE_SIZE;
  const visibleJobs = jobs;
  const jobEnd = jobStart + visibleJobs.length;
  const safeOperationPage = Math.min(operationPage, Math.max(0, operationHistory.length - 1));
  const operationStart = safeOperationPage * PAGE_SIZE;
  const visibleOperations = operationHistory[safeOperationPage] ?? selected?.operations ?? [];
  const operationEnd = operationStart + visibleOperations.length;
  const safeTargetPage = Math.min(targetPage, Math.max(0, targetHistory.length - 1));
  const targetStart = safeTargetPage * PAGE_SIZE;
  const visibleTargets = targetHistory[safeTargetPage] ?? selected?.targets ?? [];
  const targetEnd = targetStart + visibleTargets.length;

  async function nextJobs() {
    if (!jobNextCursor) return;
    setLoading(true);
    try {
      const result = await api.jobs(jobNextCursor);
      setJobPage(page => page + 1);
      setJobs(result.jobs);
      setJobNextCursor(result.next_cursor ?? undefined);
      setJobTotal(result.total);
      setJobHistory(current => [...current, {jobs: result.jobs, next: result.next_cursor ?? undefined}]);
    } catch (value) {
      setError(errorText(value, "Unable to load jobs"));
    } finally { setLoading(false); }
  }

  function previousJobs() {
    const previous = Math.max(0, safeJobPage - 1);
    setJobPage(previous);
    setJobs(jobHistory[previous].jobs);
    setJobNextCursor(jobHistory[previous].next);
  }

  async function nextOperations() {
    if (!selected) return;
    const cursor = operationCursors[safeOperationPage];
    if (!cursor) return;
    const detail = await api.job(selected.id, cursor);
    setOperationHistory(current => [...current, detail.operations]);
    setOperationCursors(current => [...current, detail.operation_next_cursor ?? undefined]);
    setOperationPage(page => page + 1);
  }

  async function nextTargets() {
    if (!selected) return;
    const cursor = targetCursors[safeTargetPage];
    if (!cursor) return;
    const detail = await api.job(selected.id, undefined, cursor);
    setTargetHistory(current => [...current, detail.targets]);
    setTargetCursors(current => [...current, detail.target_next_cursor ?? undefined]);
    setTargetPage(page => page + 1);
  }

  return <>
    <h2>Jobs</h2>
    <p>Onboarding, proposal, deployment, and reconciliation progress.</p>
    {loading && <p role="status">Loading durable job state…</p>}
    {error && <p role="alert">{error}</p>}
    {status && <p role="status">{status}</p>}
    <p>{jobTotal === 0 ? "Showing jobs 0 of 0" : `Showing jobs ${jobStart + 1}–${jobEnd} of ${jobTotal}`}</p>
    <div className="table-scroll"><table aria-label="Durable jobs">
      <thead><tr><th scope="col">ID</th><th scope="col">Kind</th><th scope="col">State</th><th scope="col">Detail</th></tr></thead>
      <tbody>{visibleJobs.map(job => <tr key={job.id}>
        <th scope="row"><code>{bounded(job.id)}</code></th><td>{bounded(job.kind)}</td><td>{bounded(job.state)}</td>
        <td><button type="button" aria-label={`View job ${bounded(job.id)}`} onClick={() => void view(job.id)}>View</button></td>
      </tr>)}</tbody>
    </table></div>
    {(safeJobPage > 0 || jobNextCursor) && <div className="pagination">
      <button type="button" aria-label="Previous jobs" disabled={safeJobPage === 0} onClick={previousJobs}>Previous jobs</button>
      <button type="button" aria-label="Next jobs" disabled={!jobNextCursor} onClick={() => void nextJobs()}>Next jobs</button>
    </div>}

    {selected && <section className="job-detail" aria-labelledby="job-detail-heading">
      <h3 id="job-detail-heading">Job detail</h3>
      <dl className="evidence-grid compact">
        <div><dt>Job</dt><dd><code>{bounded(selected.id)}</code></dd></div>
        <div><dt>State</dt><dd>{bounded(selected.state)}</dd></div>
        <div><dt>Kind</dt><dd>{bounded(selected.kind)}</dd></div>
        <div><dt>Base commit</dt><dd><code>{bounded(selected.base_commit)}</code></dd></div>
        <div><dt>Attempt</dt><dd>{progress(selected.current_attempt)}</dd></div>
        <div><dt>Reconciliation</dt><dd><code>{bounded(selected.reconciliation_id ?? "—")}</code></dd></div>
      </dl>
      <p>{progress(selected.progress.completed)} of {progress(selected.progress.total)} completed; {progress(selected.progress.running)} running; {progress(selected.progress.failed)} failed</p>
      <progress max={Math.max(1, progress(selected.progress.total))} value={progress(selected.progress.completed)}>Job completion</progress>
      {selected.status_reason && <p role={selected.state === "waiting-for-operator" ? "alert" : undefined}>{bounded(selected.status_reason, MAX_MESSAGE)}</p>}
      <h4>Affected nodes</h4>
      <p>{(selected.target_total ?? selected.targets.length) === 0 ? "Showing affected nodes 0 of 0" : `Showing affected nodes ${targetStart + 1}–${targetEnd} of ${selected.target_total ?? selected.targets.length}`}</p>
      <ul>{visibleTargets.map(target => <li key={target}><code>{bounded(target)}</code></li>)}</ul>
      {(safeTargetPage > 0 || targetCursors[safeTargetPage]) && <div className="pagination">
        <button type="button" aria-label="Previous affected nodes" disabled={safeTargetPage === 0} onClick={() => setTargetPage(page => Math.max(0, page - 1))}>Previous affected nodes</button>
        <button type="button" aria-label="Next affected nodes" disabled={!targetCursors[safeTargetPage]} onClick={() => void nextTargets()}>Next affected nodes</button>
      </div>}
      {selected.state === "waiting-for-operator" && <button type="button" disabled={loading} onClick={() => void resume()}>Resume job</button>}

      <h4>Node operations</h4>
      <p>{(selected.operation_total ?? selected.operations.length) === 0 ? "Showing operations 0 of 0" : `Showing operations ${operationStart + 1}–${operationEnd} of ${selected.operation_total ?? selected.operations.length}`}</p>
      <div className="table-scroll"><table aria-label="Node operation progress">
        <thead><tr><th scope="col">Operation</th><th scope="col">Node</th><th scope="col">Kind</th><th scope="col">State</th><th scope="col">Progress</th><th scope="col">Attempt</th></tr></thead>
        <tbody>{visibleOperations.map(operation => <tr key={operation.id}>
          <th scope="row"><code>{bounded(operation.id)}</code><small>{bounded(operation.graph_operation_id ?? "—")}</small></th>
          <td><code>{bounded(operation.node_id)}</code></td><td>{bounded(operation.kind)}</td><td>{bounded(operation.state)}</td>
          <td>{bounded(operation.progress?.phase ?? "—")}</td><td>{progress(operation.attempt)}</td>
        </tr>)}</tbody>
      </table></div>
      {(safeOperationPage > 0 || operationCursors[safeOperationPage]) && <div className="pagination">
        <button type="button" aria-label="Previous operations" disabled={safeOperationPage === 0} onClick={() => setOperationPage(page => Math.max(0, page - 1))}>Previous operations</button>
        <button type="button" aria-label="Next operations" disabled={!operationCursors[safeOperationPage]} onClick={() => void nextOperations()}>Next operations</button>
      </div>}
    </section>}
  </>;
}
