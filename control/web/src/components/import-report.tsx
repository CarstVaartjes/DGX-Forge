import type {ImportDisposition, ImportReportItem} from "../api/types";

const labels: Record<ImportDisposition, string> = {
  imported: "Imported", transformed: "Transformed safely", resolution_required: "Resolution required",
  overlay_required: "Information required", unsupported_blocking: "Unsupported — blocks running", dropped_redundant: "Not imported — redundant",
};
const order: ImportDisposition[] = ["unsupported_blocking", "resolution_required", "overlay_required", "transformed", "imported", "dropped_redundant"];

export function ImportReport({items}: {items: ImportReportItem[]}) {
  return <div className="import-report">{order.map(disposition => {
    const group = items.filter(item => item.disposition === disposition); if (!group.length) return null;
    return <section key={disposition} aria-labelledby={`import-${disposition}`}><h3 id={`import-${disposition}`}>{labels[disposition]} <span>{group.length}</span></h3><ul>{group.map(item => <li key={item.source_path}><code>{item.source_path}</code><p>{item.detail}</p>{item.destination_path && <small>→ {item.destination_path}</small>}</li>)}</ul></section>;
  })}</div>;
}
