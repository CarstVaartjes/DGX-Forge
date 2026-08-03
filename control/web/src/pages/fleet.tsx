import {useEffect, useState} from "react";
import type {ControlApi, FleetResponse} from "../api/types";
export function FleetPage({api}: {api: ControlApi}) {
  const [fleet, setFleet] = useState<FleetResponse>(); const [error, setError] = useState("");
  useEffect(() => {api.fleet().then(setFleet).catch(value => setError(String(value)));}, [api]);
  return <><h2>Fleet</h2><p>Live cluster state joined to the repository-backed fleet definition.</p>{error && <p role="alert">{error}</p>}<table><caption>DGX Spark nodes</caption><thead><tr><th scope="col">Node</th><th scope="col">Lifecycle</th><th scope="col">Health</th><th scope="col">Profile</th></tr></thead><tbody>{fleet?.nodes.map(node => <tr key={node.id}><th scope="row">{node.display_name}<small>{node.id}</small></th><td>{node.lifecycle}</td><td><span className={`status ${node.healthy ? "good" : "unknown"}`}>{node.healthy === true ? "Healthy" : node.healthy === false ? "Unavailable" : "Unknown"}</span></td><td>{node.profile ?? "—"}</td></tr>)}</tbody></table>{fleet && fleet.nodes.length === 0 && <p>No Sparks are registered yet. Use the onboarding CLI to add the first node.</p>}</>;
}
