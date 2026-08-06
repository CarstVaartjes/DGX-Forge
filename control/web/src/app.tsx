import {useEffect, useState} from "react";
import type {ControlApi} from "./api/types"; import {FleetPage} from "./pages/fleet"; import {AgentsPage} from "./pages/agents"; import {ProfilesPage} from "./pages/profiles"; import {ModelsPage} from "./pages/models"; import {JobsPage} from "./pages/jobs"; import {AuditPage} from "./pages/audit"; import {UpdatesPage} from "./pages/updates"; import {PackagesPage} from "./pages/packages"; import {PackageCandidatePage} from "./pages/package-candidate"; import {DeploymentsPage} from "./pages/deployments"; import type {PackageApi} from "./pages/package-types";
const pages = ["fleet", "agents", "profiles", "models", "packages", "deployments", "updates", "jobs", "audit"] as const;
type Page = typeof pages[number];
function candidateId(): string | undefined { const match = /^\/packages\/([^/]+)$/.exec(location.pathname); if (!match) return undefined; try { return decodeURIComponent(match[1]); } catch { return undefined; } }
function currentPage(): Page { const value = location.pathname.replace(/^\//, ""); return candidateId() || value === "packages" ? "packages" : pages.includes(value as Page) ? value as Page : "fleet"; }
export function App({api}: {api: ControlApi}) {
  const [page, setPage] = useState<Page>(currentPage()); useEffect(() => {const listener = () => setPage(currentPage()); addEventListener("popstate", listener); return () => removeEventListener("popstate", listener);}, []);
  function navigate(event: React.MouseEvent<HTMLAnchorElement>, target: Page) { event.preventDefault(); history.pushState(null, "", `/${target}`); setPage(target); }
  // W15's generated client supplies these package methods.  This narrow cast
  // keeps W16 independent of generated declaration timing.
  const packageApi = api as unknown as PackageApi;
  const selectedCandidate = candidateId();
  const content = selectedCandidate ? <PackageCandidatePage api={packageApi} candidateId={selectedCandidate}/> : {fleet: <FleetPage api={api}/>, agents: <AgentsPage api={api}/>, profiles: <ProfilesPage api={api}/>, models: <ModelsPage api={api}/>, packages: <PackagesPage api={packageApi}/>, deployments: <DeploymentsPage api={packageApi}/>, updates: <UpdatesPage api={api}/>, jobs: <JobsPage api={api}/>, audit: <AuditPage api={api}/>}[page];
  return <div className="shell"><aside><header><span className="mark">DF</span><div><strong>DGX Forge</strong><small>Cluster control</small></div></header><nav aria-label="Primary">{pages.map(target => <a key={target} href={`/${target}`} className={page === target ? "active" : ""} aria-current={page === target ? "page" : undefined} onClick={event => navigate(event, target)}>{target[0].toUpperCase() + target.slice(1)}</a>)}</nav><footer>Repository-authoritative</footer></aside><main>{content}</main></div>;
}
