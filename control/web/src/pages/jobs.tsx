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
  const [jobPage, setJobPage] = useState(0);
  const [operationPage, setOperationPage] = useState(0);
  const [targetPage, setTargetPage] = useState(0);
  const [selected, setSelected] = useState<JobDetail>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");

  useEffect(() => {
    let current = true;
    setLoading(true);
    api.jobs().then(result => {
      if (current) setJobs(result.jobs);
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
      setSelected(await api.job(jobId));
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
    } finally {
      setLoading(false);
    }
  }

  const jobPageCount = Math.max(1, Math.ceil(jobs.length / PAGE_SIZE));
  const safeJobPage = Math.min(jobPage, jobPageCount - 1);
  const jobStart = safeJobPage * PAGE_SIZE;
  const visibleJobs = jobs.slice(jobStart, jobStart + PAGE_SIZE);
  const jobEnd = jobStart + visibleJobs.length;
  const operations = selected?.operations ?? [];
  const operationPageCount = Math.max(1, Math.ceil(operations.length / PAGE_SIZE));
  const safeOperationPage = Math.min(operationPage, operationPageCount - 1);
  const operationStart = safeOperationPage * PAGE_SIZE;
  const visibleOperations = operations.slice(operationStart, operationStart + PAGE_SIZE);
  const operationEnd = operationStart + visibleOperations.length;
  const targets = selected?.targets ?? [];
  const targetPageCount = Math.max(1, Math.ceil(targets.length / PAGE_SIZE));
  const safeTargetPage = Math.min(targetPage, targetPageCount - 1);
  const targetStart = safeTargetPage * PAGE_SIZE;
  const visibleTargets = targets.slice(targetStart, targetStart + PAGE_SIZE);
  const targetEnd = targetStart + visibleTargets.length;

  return <>
    <h2>Jobs</h2>
    <p>Onboarding, proposal, deployment, and reconciliation progress.</p>
    {loading && <p role="status">Loading durable job state…</p>}
    {error && <p role="alert">{error}</p>}
    {status && <p role="status">{status}</p>}
    <p>{jobs.length === 0 ? "Showing jobs 0 of 0" : `Showing jobs ${jobStart + 1}–${jobEnd} of ${jobs.length}`}</p>
    <div className="table-scroll"><table aria-label="Durable jobs">
      <thead><tr><th scope="col">ID</th><th scope="col">Kind</th><th scope="col">State</th><th scope="col">Detail</th></tr></thead>
      <tbody>{visibleJobs.map(job => <tr key={job.id}>
        <th scope="row"><code>{bounded(job.id)}</code></th><td>{bounded(job.kind)}</td><td>{bounded(job.state)}</td>
        <td><button type="button" aria-label={`View job ${bounded(job.id)}`} onClick={() => void view(job.id)}>View</button></td>
      </tr>)}</tbody>
    </table></div>
    {jobPageCount > 1 && <div className="pagination">
      <button type="button" aria-label="Previous jobs" disabled={safeJobPage === 0} onClick={() => setJobPage(page => Math.max(0, page - 1))}>Previous jobs</button>
      <button type="button" aria-label="Next jobs" disabled={safeJobPage === jobPageCount - 1} onClick={() => setJobPage(page => Math.min(jobPageCount - 1, page + 1))}>Next jobs</button>
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
      <p>{targets.length === 0 ? "Showing affected nodes 0 of 0" : `Showing affected nodes ${targetStart + 1}–${targetEnd} of ${targets.length}`}</p>
      <ul>{visibleTargets.map(target => <li key={target}><code>{bounded(target)}</code></li>)}</ul>
      {targetPageCount > 1 && <div className="pagination">
        <button type="button" aria-label="Previous affected nodes" disabled={safeTargetPage === 0} onClick={() => setTargetPage(page => Math.max(0, page - 1))}>Previous affected nodes</button>
        <button type="button" aria-label="Next affected nodes" disabled={safeTargetPage === targetPageCount - 1} onClick={() => setTargetPage(page => Math.min(targetPageCount - 1, page + 1))}>Next affected nodes</button>
      </div>}
      {selected.state === "waiting-for-operator" && <button type="button" disabled={loading} onClick={() => void resume()}>Resume job</button>}

      <h4>Node operations</h4>
      <p>{operations.length === 0 ? "Showing operations 0 of 0" : `Showing operations ${operationStart + 1}–${operationEnd} of ${operations.length}`}</p>
      <div className="table-scroll"><table aria-label="Node operation progress">
        <thead><tr><th scope="col">Operation</th><th scope="col">Node</th><th scope="col">Kind</th><th scope="col">State</th><th scope="col">Progress</th><th scope="col">Attempt</th></tr></thead>
        <tbody>{visibleOperations.map(operation => <tr key={operation.id}>
          <th scope="row"><code>{bounded(operation.id)}</code><small>{bounded(operation.graph_operation_id ?? "—")}</small></th>
          <td><code>{bounded(operation.node_id)}</code></td><td>{bounded(operation.kind)}</td><td>{bounded(operation.state)}</td>
          <td>{bounded(operation.progress?.phase ?? "—")}</td><td>{progress(operation.attempt)}</td>
        </tr>)}</tbody>
      </table></div>
      {operationPageCount > 1 && <div className="pagination">
        <button type="button" aria-label="Previous operations" disabled={safeOperationPage === 0} onClick={() => setOperationPage(page => Math.max(0, page - 1))}>Previous operations</button>
        <button type="button" aria-label="Next operations" disabled={safeOperationPage === operationPageCount - 1} onClick={() => setOperationPage(page => Math.min(operationPageCount - 1, page + 1))}>Next operations</button>
      </div>}
    </section>}
  </>;
}
