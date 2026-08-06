import {useEffect, useState} from "react";
import type {PackageApi, PackageCandidateSummary, PackageFamily} from "./package-types";
import {PackageInventory} from "../components/package-inventory";

const MAX_TEXT = 160;

function bounded(value: string | null | undefined): string {
  if (!value) return "—";
  return value.length > MAX_TEXT ? `${value.slice(0, MAX_TEXT)}…` : value;
}

type PackagesPageApi = Pick<PackageApi, "packageFamilies" | "packageCandidates"> & Partial<Pick<PackageApi, "packageInventory" | "previewPackageGc" | "applyPackageGc" | "previewPackageRemoval" | "removePackageInventory">>;

export function PackagesPage({api}: {api: PackagesPageApi}) {
  const [families, setFamilies] = useState<PackageFamily[]>([]);
  const [candidates, setCandidates] = useState<PackageCandidateSummary[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    void Promise.all([api.packageFamilies(), api.packageCandidates()]).then(([nextFamilies, nextCandidates]) => {
      if (!active) return;
      setFamilies(nextFamilies);
      setCandidates(nextCandidates);
    }).catch(value => {
      if (active) setError(bounded(value instanceof Error ? value.message : "Unable to load package state"));
    });
    return () => { active = false; };
  }, [api]);

  return <>
    <div className="page-heading"><div><h2>Workload packages</h2><p>Review discovered package candidates before a signed promotion.</p></div></div>
    {error && <p role="alert">{error}</p>}
    <PackageInventory api={api}/>
    <section aria-labelledby="package-families-heading">
      <h3 id="package-families-heading">Families and channels</h3>
      <div className="table-scroll"><table aria-label="Package families">
        <thead><tr><th scope="col">Family</th><th scope="col">Channel</th><th scope="col">Candidates</th><th scope="col">Deployments</th></tr></thead>
        <tbody>{families.map(family => <tr key={family.id}>
          <th scope="row">{bounded(family.id)}</th><td>{bounded(family.channel ?? family.channels?.join(", "))}</td><td>{family.candidate_count ?? "—"}</td><td>{family.deployment_count ?? "—"}</td>
        </tr>)}</tbody>
      </table></div>
    </section>
    <section aria-labelledby="package-candidates-heading">
      <h3 id="package-candidates-heading">Upstream candidates</h3>
      <div className="table-scroll"><table aria-label="Package candidates">
        <thead><tr><th scope="col">Family</th><th scope="col">Version</th><th scope="col">Provider</th><th scope="col">State</th><th scope="col">Reason</th><th scope="col">Review</th></tr></thead>
        <tbody>{candidates.map(candidate => <tr key={candidate.id}>
          <th scope="row">{bounded(candidate.family_id)}<small>{bounded(candidate.channel)}</small></th>
          <td>{bounded(candidate.upstream_version)}</td><td>{bounded(candidate.provider)}</td><td><span className="status">{bounded(candidate.state)}</span></td><td>{bounded(candidate.reason_code)}</td>
          <td><a href={`/packages/${encodeURIComponent(candidate.id)}`} aria-label={`View candidate ${bounded(candidate.id)}`}>Review</a></td>
        </tr>)}</tbody>
      </table></div>
    </section>
  </>;
}
