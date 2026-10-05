import type React from "react";

export type AppKey = "aie" | "wf" | "fnd";

export interface PageRegistration {
  key: string;
  component: React.ComponentType;
  appKey?: AppKey;
  scroll?: boolean;
  requiresFlowDesign?: boolean;
  /** Route access inherits the grant for another page, such as an app detail
   * route inheriting the Applications hub grant. */
  grantKey?: string;
}

export interface RegisteredPage {
  key: string;
  route: string;
  app_key: AppKey;
  label: string;
  enabled: boolean;
}

export interface NavigationPayload {
  roots: Array<{
    menu_id: string;
    label: string;
    icon?: string | null;
    items: Array<Record<string, unknown>>;
  }>;
  pages: RegisteredPage[];
  grants: Record<string, { kind: string; target: string; access: "read" | "write" }>;
}
