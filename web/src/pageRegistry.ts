import type { PageRegistration } from "../../packages/web-common/src/pageRegistry";
import { pages as aiePages } from "../../apps/aie/web/src/pageRegistry";
import { pages as wfPages } from "../../apps/wf/web/src/pageRegistry";
import { pages as fndPages } from "../../apps/fnd/web/src/pageRegistry";

export const PAGE_REGISTRY = new Map<string, PageRegistration>(
  [
    ...aiePages.map((page) => ({ ...page, appKey: "aie" as const })),
    ...wfPages.map((page) => ({ ...page, appKey: "wf" as const })),
    ...fndPages.map((page) => ({ ...page, appKey: "fnd" as const })),
  ].map((page) => [page.key, page]),
);
