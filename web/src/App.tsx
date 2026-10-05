import { App as SharedApp } from "../../packages/web-common/src/DashboardApp";
import { PAGE_REGISTRY } from "./pageRegistry";

export function App() {
  return <SharedApp pageRegistry={PAGE_REGISTRY} activeAppKey="all" />;
}
