import type { PageRegistration } from "../../../../packages/web-common/src/pageRegistry";
import { AnalyticsPage } from "./pages/AnalyticsPage";
import { ChatPage } from "./pages/ChatPage";
import { SessionsPage } from "./pages/SessionsPage";
import { TokenomicsPage } from "./pages/TokenomicsPage";
import { TokenomicsReportPage } from "./pages/TokenomicsReportPage";

export const pages: PageRegistration[] = [
  { key: "chat", component: ChatPage },
  { key: "sessions", component: SessionsPage, scroll: true },
  { key: "analytics", component: AnalyticsPage, scroll: true },
  { key: "tokenomics.report", component: TokenomicsReportPage, scroll: true },
  { key: "tokenomics.estimation", component: TokenomicsPage, scroll: true },
];
