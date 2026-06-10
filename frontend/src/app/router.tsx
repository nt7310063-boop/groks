import { createBrowserRouter, Navigate } from "react-router-dom";

import { AppShell } from "@/components/layout/AppShell";
import { ProtectedRoute } from "@/components/layout/ProtectedRoute";
import { PublicRouteGuard } from "@/components/layout/PublicRouteGuard";
import { RouteErrorBoundary } from "@/components/layout/RouteErrorBoundary";

import { LoginPage } from "@/modules/auth/views/LoginPage";
import { RegisterPage } from "@/modules/auth/views/RegisterPage";
import { lazyPage } from "./lazyPage";

import { getAuthedRoutes } from "./moduleRegistry";

export const router = createBrowserRouter(
  [
  {
    path: "/login",
    element: <PublicRouteGuard flag="allow_login"><LoginPage /></PublicRouteGuard>,
    errorElement: <RouteErrorBoundary />,
  },
  {
    path: "/admin/login",
    element: <LoginPage forceTemplate="admin" />,
    errorElement: <RouteErrorBoundary />,
  },
  {
    path: "/register",
    element: (
      <PublicRouteGuard flag="allow_register" fallback="/login">
        <RegisterPage />
      </PublicRouteGuard>
    ),
    errorElement: <RouteErrorBoundary />,
  },
  {
    path: "/create-video-pro",
    element: (
      <ProtectedRoute>
        {lazyPage(() => import("@/modules/grok/views/CreateVideoProPage"), "CreateVideoProPage")}
      </ProtectedRoute>
    ),
    errorElement: <RouteErrorBoundary />,
  },
  {
    path: "/",
    element: (
      <ProtectedRoute>
        <AppShell />
      </ProtectedRoute>
    ),
    errorElement: <RouteErrorBoundary />,
    children: [
      { index: true, element: <Navigate to="/jobs" replace /> },
      ...getAuthedRoutes(),
      { path: "*", element: <RouteErrorBoundary /> },
    ],
  },
  ],
);
