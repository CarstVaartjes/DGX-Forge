import {useState} from "react";
import type {SparkRunApi, SparkRunPreview} from "../api/types";
import {ImportReport} from "../components/import-report";

export function SparkRunImportPage({api}: {api: SparkRunApi}) {
  const [source, setSource] = useState(""); const [preview, setPreview] = useState<SparkRunPreview | null>(null);
  const [error, setError] = useState(""); const [message, setMessage] = useState(""); const [busy, setBusy] = useState(false);
  async function loadFile(file?: File) {
    setError(""); if (!file) return;
    if (!/\.ya?ml$/i.test(file.name) || file.size > 262144) { setError("Choose one .yaml or .yml file no larger than 256 KiB."); return; }
    setSource(await file.text()); setPreview(null);
  }
  async function showPreview() {
    setBusy(true); setError(""); setMessage("");
    try { setPreview(await api.previewSparkRun(source)); } catch (value) { setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to preview import"); }
    finally { setBusy(false); }
  }
  async function apply() {
    if (!preview) return; setBusy(true); setError("");
    try { const result = await api.applySparkRun(source, preview.source_sha256, preview.report_digest); setMessage(`Imported ${result.lifecycle} draft revision ${result.revision_number}. Open it from the catalog to provide missing information.`); }
    catch (value) { setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to import draft"); }
    finally { setBusy(false); }
  }
  return <>
    <div className="page-heading"><div><h2>Import SparkRun recipe</h2><p>Preview is non-executing. Every source field is explained before anything is saved.</p></div></div>
    {error && <p role="alert">{error}</p>}{message && <p role="status">{message}</p>}
    <section className="import-source"><label>Upload YAML<input type="file" accept=".yaml,.yml,application/yaml,text/yaml" onChange={event => void loadFile(event.target.files?.[0])}/></label><span>or</span><label>SparkRun YAML<textarea rows={16} value={source} onChange={event => {setSource(event.target.value); setPreview(null);}} maxLength={262144}/></label><button onClick={() => void showPreview()} disabled={busy || !source}>Preview import</button></section>
    {preview && <><section className="import-identity" aria-label="Import identity"><p>Source sha256:{preview.source_sha256}</p><p>Report sha256:{preview.report_digest}</p><strong>{preview.runnable ? "Ready to resolve" : "Blocked until the listed requirements are addressed"}</strong></section><ImportReport items={preview.report}/><button onClick={() => void apply()} disabled={busy}>Import blocked draft</button></>}
  </>;
}
