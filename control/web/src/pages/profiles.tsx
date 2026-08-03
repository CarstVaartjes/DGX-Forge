import type {ControlApi} from "../api/types"; import {RepositoryEditor} from "../components/repository-editor";
export function ProfilesPage({api}: {api: ControlApi}) { return <RepositoryEditor api={api} kind="profiles"/>; }
