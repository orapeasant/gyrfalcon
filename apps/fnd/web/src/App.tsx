import { App as SharedApp } from "../../../../packages/web-common/src/DashboardApp";
import { pages } from "./pageRegistry";

const pageRegistry = new Map(pages.map((page) => [page.key, { ...page, appKey: "fnd" as const }]));

export function App() {
  return <SharedApp pageRegistry={pageRegistry} activeAppKey="fnd" />;
}
