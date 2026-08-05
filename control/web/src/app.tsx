import {useEffect, useState} from "react";
import type {ControlApi} from "./api/types"; import {FleetPage} from "./pages/fleet"; import {AgentsPage} from "./pages/agents"; import {ProfilesPage} from "./pages/profiles"; import {ModelsPage} from "./pages/models"; import {JobsPage} from "./pages/jobs"; import {AuditPage} from "./pages/audit";
const pages = ["fleet", "agents", "profiles", "models", "jobs", "audit"] as const;
type Page = typeof pages[number];
function currentPage(): Page { const value = location.pathname.replace(/^\//, ""); return pages.includes(value as Page) ? value as Page : "fleet"; }
export function App({api}: {api: ControlApi}) {
  const [page, setPage] = useState<Page>(currentPage()); useEffect(() => {const listener = () => setPage(currentPage()); addEventListener("popstate", listener); return () => removeEventListener("popstate", listener);}, []);
  function navigate(event: React.MouseEvent<HTMLAnchorElement>, target: Page) { event.preventDefault(); history.pushState(null, "", `/${target}`); setPage(target); }
  const content = {fleet: <FleetPage api={api}/>, agents: <AgentsPage api={api}/>, profiles: <ProfilesPage api={api}/>, models: <ModelsPage api={api}/>, jobs: <JobsPage api={api}/>, audit: <AuditPage api={api}/>}[page];
  return <div className="shell"><aside><header><span className="mark">DF</span><div><strong>DGX Forge</strong><small>Cluster control</small></div></header><nav aria-label="Primary">{pages.map(target => <a key={target} href={`/${target}`} className={page === target ? "active" : ""} aria-current={page === target ? "page" : undefined} onClick={event => navigate(event, target)}>{target[0].toUpperCase() + target.slice(1)}</a>)}</nav><footer>Repository-authoritative</footer></aside><main>{content}</main></div>;
}
