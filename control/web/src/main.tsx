import {StrictMode} from "react"; import {createRoot} from "react-dom/client"; import {ApiClient} from "./api/client"; import {App} from "./app"; import "./styles.css";
createRoot(document.getElementById("root")!).render(<StrictMode><App api={new ApiClient()}/></StrictMode>);
