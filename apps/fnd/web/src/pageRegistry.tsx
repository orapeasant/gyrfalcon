import type { PageRegistration } from "../../../../packages/web-common/src/pageRegistry";
import { AccessControlPage } from "./pages/AccessControlPage";
import { AgentsPage } from "./pages/AgentsPage";
import { ApplicationDetailPage } from "./pages/ApplicationDetailPage";
import { ApplicationsManagePage } from "./pages/ApplicationsManagePage";
import { ConfigPage } from "./pages/ConfigPage";
import { GuardrailPage } from "./pages/GuardrailPage";
import { FunctionsPage, MenuItemsPage, MenusPage } from "./pages/NavigationManagementPage";
import { LogsPage } from "./pages/LogsPage";
import { McpPage } from "./pages/McpPage";
import { ModelsPage } from "./pages/ModelsPage";
import { PluginsPage } from "./pages/PluginsPage";
import { ProfilesPage } from "./pages/ProfilesPage";
import { RoutingPage } from "./pages/RoutingPage";
import { SchedulerPage } from "./pages/SchedulerPage";
import { SecretStorePage } from "./pages/SecretStorePage";
import { SkillsPage } from "./pages/SkillsPage";
import { ServiceAccountsPage } from "./pages/ServiceAccountsPage";

function UsersPage() { return <AccessControlPage section="users" />; }
function GroupsPage() { return <AccessControlPage section="groups" />; }
function RolesPage() { return <AccessControlPage section="roles" />; }

export const pages: PageRegistration[] = [
  { key: "applications.manage", component: ApplicationsManagePage },
  { key: "applications.detail", component: ApplicationDetailPage, grantKey: "applications.manage" },
  { key: "ai.models", component: ModelsPage, scroll: true },
  { key: "ai.mcp", component: McpPage },
  { key: "ai.agents", component: AgentsPage },
  { key: "ai.skills", component: SkillsPage },
  { key: "ai.plugins", component: PluginsPage },
  { key: "ai.routing", component: RoutingPage, scroll: true },
  { key: "ai.guardrail", component: GuardrailPage, scroll: true },
  { key: "admin.scheduler", component: SchedulerPage, scroll: true },
  { key: "admin.profiles", component: ProfilesPage, scroll: true },
  { key: "admin.config", component: ConfigPage, scroll: true },
  { key: "admin.logs", component: LogsPage, scroll: true },
  { key: "security.service_accounts", component: ServiceAccountsPage, scroll: true },
  { key: "security.secrets", component: SecretStorePage, scroll: true },
  { key: "security.users", component: UsersPage, scroll: true },
  { key: "security.groups", component: GroupsPage, scroll: true },
  { key: "security.roles", component: RolesPage, scroll: true },
  { key: "developer.functions", component: FunctionsPage, scroll: true },
  { key: "developer.menus", component: MenusPage, scroll: true },
  { key: "developer.menu_items", component: MenuItemsPage, scroll: true },
];
