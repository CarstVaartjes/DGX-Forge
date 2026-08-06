import {useEffect, useState} from "react";
import type {PackageApi, PackageCandidate, PackagePreview, PackageValidationProgress} from "./package-types";

const MAX_TEXT = 256;
function bounded(value: string | null | undefined): string {
  if (!value) return "—";
  return value.length > MAX_TEXT ? `${value.slice(0, MAX_TEXT)}…` : value;
}

type CandidateApi = Pick<PackageApi, "packageCandidate" | "previewPackagePromotion" | "promotePackage"> & Partial<Pick<PackageApi, "previewPackageValidation" | "validatePackage" | "packageValidation">>;

export function PackageCandidatePage({api, candidateId}: {api: CandidateApi; candidateId: string}) {
  const [candidate, setCandidate] = useState<PackageCandidate>();
  const [preview, setPreview] = useState<PackagePreview>();
  const [validationPreview, setValidationPreview] = useState<PackagePreview>();
  const [validation, setValidation] = useState<PackageValidationProgress>();
  const [confirmation, setConfirmation] = useState("");
  const [validationConfirmation, setValidationConfirmation] = useState("");
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");

  useEffect(() => {
    let active = true;
    void api.packageCandidate(candidateId).then(result => {
      if (active) setCandidate(result);
    }).catch(value => {
      if (active) setError(bounded(value instanceof Error ? value.message : "Unable to load candidate"));
    });
    return () => { active = false; };
  }, [api, candidateId]);

  async function previewPromotion() {
    setError(""); setStatus(""); setConfirmation("");
    try { setPreview(await api.previewPackagePromotion(candidateId)); }
    catch (value) { setError(bounded(value instanceof Error ? value.message : "Unable to preview promotion")); }
  }

  async function promote() {
    if (!preview || confirmation !== preview.digest) return;
    setError("");
    try {
      await api.promotePackage(candidateId, preview.digest);
      setStatus("Package promotion accepted. The desired-state proposal and audit trail are authoritative.");
    } catch (value) { setError(bounded(value instanceof Error ? value.message : "Package promotion failed")); }
  }

  async function previewValidation() {
    if (!api.previewPackageValidation) return;
    setError(""); setStatus(""); setValidationConfirmation("");
    try { setValidationPreview(await api.previewPackageValidation(candidateId)); }
    catch (value) { setError(bounded(value instanceof Error ? value.message : "Unable to preview validation")); }
  }

  async function validate() {
    if (!api.validatePackage || !validationPreview || validationConfirmation !== validationPreview.digest) return;
    setError("");
    try {
      const result = await api.validatePackage(candidateId, validationPreview.digest);
      setValidation(result);
      setStatus("Package validation was queued. The active workload remains untouched.");
    } catch (value) { setError(bounded(value instanceof Error ? value.message : "Package validation failed")); }
  }

  async function refreshValidation() {
    if (!api.packageValidation || !validationPreview?.validation_id) return;
    try { setValidation(await api.packageValidation(validationPreview.validation_id)); }
    catch (value) { setError(bounded(value instanceof Error ? value.message : "Unable to refresh validation")); }
  }

  return <>
    <div className="page-heading"><div><h2>Package candidate</h2><p>Immutable lock, compatibility evidence, and administrator promotion control.</p></div><a href="/packages">Back to packages</a></div>
    {error && <p role="alert">{error}</p>}
    {status && <p role="status">{status}</p>}
    {!candidate && !error && <p role="status">Loading package candidate…</p>}
    {candidate && <>
      <section className="review-card" aria-label="Package candidate state">
        <h3>{bounded(candidate.family_id)} <span className="status">{bounded(candidate.state)}</span></h3>
        <dl className="evidence-grid compact">
          <div><dt>Channel</dt><dd>{bounded(candidate.channel)}</dd></div><div><dt>Version</dt><dd>{bounded(candidate.upstream_version)}</dd></div><div><dt>Provider</dt><dd>{bounded(candidate.provider)}</dd></div>
          <div><dt>Lock digest</dt><dd><code>{bounded(candidate.lock?.digest)}</code></dd></div><div><dt>Provenance</dt><dd>{bounded(candidate.lock?.provenance)}</dd></div><div><dt>Compatibility</dt><dd>{candidate.compatibility ? `${candidate.compatibility.compatible.length} compatible; ${candidate.compatibility.incompatible_count} incompatible` : "Not evaluated"}</dd></div>
        </dl>
        <p>Components: {candidate.lock?.components.map(bounded).join(", ") || "—"}</p>
        <p>Dependencies: {candidate.lock?.dependencies.map(bounded).join(", ") || "—"}</p>
      </section>
      <section aria-labelledby="validation-heading"><h3 id="validation-heading">Validation</h3>{candidate.validations?.length ? <ul>{candidate.validations.map((validation, index) => <li key={`${validation.backend}-${index}`}>{bounded(validation.backend)} — {bounded(validation.state)}{validation.reason_code ? `: ${bounded(validation.reason_code)}` : ""}</li>)}</ul> : <p>No validation records are available yet.</p>}
        {api.previewPackageValidation && <><button type="button" onClick={() => void previewValidation()}>Preview package validation</button>{validationPreview && <div className="review-card compact"><p>Exact validation plan <code>{bounded(validationPreview.digest)}</code></p><p>Validation run: <code>{bounded(validationPreview.validation_id)}</code></p><label>Type the exact validation digest<input value={validationConfirmation} onChange={event => setValidationConfirmation(event.target.value)} maxLength={512}/></label><button type="button" disabled={validationConfirmation !== validationPreview.digest || !api.validatePackage} onClick={() => void validate()}>Start exact validation plan</button></div>}{validation && <p role="status">Validation state: <strong>{bounded(validation.state)}</strong>{validation.progress ? ` (${validation.progress.completed}/${validation.progress.total})` : ""} {api.packageValidation && validationPreview?.validation_id && <button type="button" onClick={() => void refreshValidation()}>Refresh validation</button>}</p>}</>}
      </section>
      <section aria-labelledby="promotion-heading" className="review-card"><h3 id="promotion-heading">Promote approved package</h3>
        <button type="button" onClick={() => void previewPromotion()}>Preview package promotion</button>
        {preview && <><p>Exact preview digest <code>{bounded(preview.digest)}</code></p><p>{bounded(preview.diff)}</p><label>Type the exact preview digest<input value={confirmation} onChange={event => setConfirmation(event.target.value)} maxLength={512}/></label><button type="button" disabled={confirmation !== preview.digest} onClick={() => void promote()}>Promote exact package preview</button></>}
      </section>
      <section aria-labelledby="package-audit-heading"><h3 id="package-audit-heading">Audit evidence</h3>{candidate.audit?.length ? <ul>{candidate.audit.map(event => <li key={event.request_id}>{bounded(event.action)} <code>{bounded(event.request_id)}</code></li>)}</ul> : <p>No audit events are attached to this projection.</p>}</section>
    </>}
  </>;
}
