import { Navigate } from "react-router-dom";
import type { PageRegistration } from "../../../../packages/web-common/src/pageRegistry";
import { FlowDeploymentsPage } from "./pages/FlowDeploymentsPage";
import { FlowDesignerPage } from "./pages/FlowDesignerPage";
import { FlowEventsPage } from "./pages/FlowEventsPage";
import { FlowInstancesPage } from "./pages/FlowInstancesPage";
import { FlowTasksPage } from "./pages/FlowTasksPage";

function FlowDefinitionsRedirect() {
  return <Navigate to="/flows/designer" replace />;
}

export const pages: PageRegistration[] = [
  { key: "flow.instances", component: FlowInstancesPage, scroll: true },
  { key: "flow.tasks", component: FlowTasksPage, scroll: true },
  { key: "flow.definitions", component: FlowDefinitionsRedirect },
  { key: "flow.designer", component: FlowDesignerPage, scroll: true, requiresFlowDesign: true },
  { key: "flow.deployments", component: FlowDeploymentsPage, scroll: true },
  { key: "flow.events", component: FlowEventsPage, scroll: true },
];
