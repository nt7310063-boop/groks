import { useEffect } from "react";
import { useLocation } from "react-router-dom";

import { useDomainStore } from "@/core/domain/store";
import { titleize } from "@/core/utils/format";

export function useDocumentTitle() {
  const location = useLocation();
  const brand = useDomainStore((s) => s.config?.brand_name) ?? "GrokFlow";

  useEffect(() => {
    const path = location.pathname || "/";
    const label = path === "/" ? null : titleize(path.split("/").pop() ?? "");
    document.title = label ? `${label} · ${brand}` : brand;
  }, [location.pathname, brand]);
}
