import {useEffect, useState} from "react";
import type {ControlApi, FleetResponse, UpdateSkew} from "../api/types";

const DISMISSED_SKEW_KEY = "dgx-forge.dismissed-update-skew";

function bounded(value: string, maximum = 256): string {
  return value.length > maximum ? `${value.slice(0, maximum)}…` : value;
}

function previouslyDismissed(digest: string): boolean {
  try {
    return localStorage.getItem(DISMISSED_SKEW_KEY) === digest;
  } catch {
    return false;
  }
}

export function FleetPage({api}: {api: ControlApi}) {
  const [fleet, setFleet] = useState<FleetResponse>();
  const [skew, setSkew] = useState<UpdateSkew>();
  const [dismissed, setDismissed] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let current = true;
    api.fleet().then(result => {
      if (current) setFleet(result);
    }).catch(value => {
      if (current) setError(String(value).slice(0, 512));
    });
    if (typeof api.updateSkew === "function") {
      api.updateSkew().then(result => {
        if (!current) return;
        setSkew(result);
        setDismissed(previouslyDismissed(result.digest) ? result.digest : "");
      }).catch(() => {
        // Fleet visibility remains useful when the update authority is
        // temporarily unavailable; the dedicated Updates page reports it.
      });
    }
    return () => { current = false; };
  }, [api]);

  function dismissUpdate() {
    if (!skew) return;
    try {
      localStorage.setItem(DISMISSED_SKEW_KEY, skew.digest);
    } catch {
      // Session-local dismissal still works if durable browser storage is denied.
    }
    setDismissed(skew.digest);
  }

  const showUpdate = skew?.prompt_required === true && dismissed !== skew.digest;

  return <>
    <h2>Fleet</h2>
    <p>Live cluster state joined to the repository-backed fleet definition.</p>
    {error && <p role="alert">{error}</p>}
    {showUpdate && skew && <section className="update-notice" aria-label="Spark update available">
      <h3>DGX-Forge update available for Sparks</h3>
      <p>The NAS is running {bounded(skew.target.platform_version)} at <code>{bounded(skew.target.build_digest)}</code>. Review and explicitly confirm the signed rollout; this notice never updates a Spark by itself.</p>
      <p>Affected Sparks: {skew.nodes.filter(node => skew.affected_nodes.includes(node.node_id)).slice(0, 1024).map(node => `${bounded(node.display_name)} (${bounded(node.node_id)})`).join(", ") || "none"}.</p>
      {skew.offline_pending.length > 0 && <p>Offline pending: {skew.offline_pending.map(bounded).join(", ")}.</p>}
      <p><a href="/updates">Review platform update</a>{" "}<button type="button" onClick={dismissUpdate}>Dismiss this exact update notice</button></p>
    </section>}
    <div className="table-scroll"><table aria-label="DGX Spark nodes">
      <caption>DGX Spark nodes</caption>
      <thead><tr><th scope="col">Node</th><th scope="col">Lifecycle</th><th scope="col">Health</th><th scope="col">Profile</th><th scope="col">Agent</th><th scope="col">Last seen</th><th scope="col">Certificate expiry</th><th scope="col">Compatibility</th></tr></thead>
      <tbody>{fleet?.nodes.map(node => <tr key={node.id}>
        <th scope="row">{node.display_name}<small>{node.id}</small></th>
        <td>{node.lifecycle}</td>
        <td><span className={`status ${node.healthy ? "good" : "unknown"}`}>{node.healthy === true ? "Healthy" : node.healthy === false ? "Unavailable" : "Unknown"}</span></td>
        <td>{node.profile ?? "—"}</td><td>{node.agent_state}</td>
        <td>{node.agent_last_seen_at ?? node.last_seen_at ?? "—"}</td>
        <td>{node.certificate_expires_at ?? "—"}</td><td>{node.compatibility}</td>
      </tr>)}</tbody>
    </table></div>
    {fleet && fleet.nodes.length === 0 && <p>No Sparks are registered yet. Use the onboarding CLI to add the first node.</p>}
  </>;
}
