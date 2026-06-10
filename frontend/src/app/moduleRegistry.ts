import type { FrontendModule, NavEntry, ModuleRoute } from "./types";

import { moduleManifest as auth } from "@/modules/auth/router";
import { moduleManifest as grok } from "@/modules/grok/router";
import { moduleManifest as account } from "@/modules/account/router";

export const MODULES: FrontendModule[] = [
  account,  // Self-service /account (profile + password), no nav entry
  grok,     // Profiles / Jobs / API Docs
  auth,
];

export const PUBLIC_MODULES = new Set(["auth"]);

export function getAuthedRoutes(): ModuleRoute[] {
  return MODULES.filter((m) => !PUBLIC_MODULES.has(m.name)).flatMap((m) => m.routes);
}

export function getPublicRoutes(): ModuleRoute[] {
  return MODULES.filter((m) => PUBLIC_MODULES.has(m.name)).flatMap((m) => m.routes);
}

export function getAuthedNav(role: string | undefined | null = null): NavEntry[] {
  return MODULES
    .filter((m) => !PUBLIC_MODULES.has(m.name))
    .flatMap((m) => m.nav ?? []);
}
