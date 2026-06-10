import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Eye, Images, Pencil, Trash2, RefreshCw, Ban, ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight, Plus, Copy, LayoutGrid, List } from "lucide-react";
import { toast } from "@/components/ui/Toast";
import { confirm } from "@/components/ui/ConfirmDialog";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { JobDetailDrawer } from "../components/JobDetailDrawer";
import { CreateJobModal } from "../components/CreateJobModal";
import { ResultGalleryModal } from "../components/ResultGalleryModal";
import { EditJobModal } from "../components/EditJobModal";
import { JobGrid } from "../components/JobGrid";
import { CompareJobsModal } from "../components/CompareJobsModal";
import type { Job } from "../models/job";
import { jobsService } from "../services/jobs.service";
import { ERROR_HINTS, parseErrorCode } from "../utils/jobError";

const PAGE_SIZES = [10, 20, 50, 100];

export function JobsPage() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [statusFilter, setStatusFilter] = useState("");
  const [providerFilter, setProviderFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  // Bulk-select state. Empty set = no selection visible. Set of job IDs
  // for rows the user has ticked. We clear on page/filter change so a
  // stale selection from page 1 can't accidentally delete from page 2.
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  const STATUS_FILTERS = [
    { v: "",                     label: t("grok.jobs_filter_all_status") },
    { v: "queued",               label: "Queued" },
    { v: "running",              label: "Running" },
    { v: "processing_provider",  label: "Processing" },
    { v: "success",              label: "Success" },
    { v: "failed",               label: "Failed" },
    { v: "cancelled",            label: "Cancelled" },
  ];
  const PROVIDER_FILTERS = [
    { v: "", label: t("grok.jobs_filter_all_provider") },
    { v: "grok", label: "Grok" },
    { v: "flow", label: "Flow" },
  ];
  const TYPE_FILTERS = [
    { v: "", label: t("grok.jobs_filter_all_type") },
    { v: "image", label: t("grok.jobs_filter_image") },
    { v: "video", label: t("grok.jobs_filter_video") },
  ];

  const offset = (page - 1) * pageSize;
  const queryKey = ["jobs", statusFilter, providerFilter, typeFilter, search, page, pageSize];

  const { data, isLoading } = useQuery({
    queryKey,
    queryFn: async () => {
      const params = new URLSearchParams();
      params.set("limit", String(pageSize));
      params.set("offset", String(offset));
      if (statusFilter) params.set("status", statusFilter);
      if (providerFilter) params.set("provider", providerFilter);
      if (typeFilter) params.set("job_type", typeFilter);
      if (search) params.set("q", search);
      const r = await jobsService.listRaw(params.toString());
      const total = parseInt(r.headers["x-total-count"] || "0", 10);
      return { items: r.data, total };
    },
    // Adaptive polling: 1.5s while any job on this page is active so the
    // table feels as live as a `curl` poll. Idle pages drop to 10s to
    // avoid hammering the API. Without this, a successful 3s image job
    // could still show "running" for up to 5s after it actually finished.
    refetchInterval: (q) => {
      const items = (q.state.data as { items?: { status: string }[] } | undefined)?.items ?? [];
      const hasActive = items.some((j) =>
        ["queued", "running", "processing_provider"].includes(j.status),
      );
      return hasActive ? 1500 : 10000;
    },
  });

  const prevStatusesRef = useRef<Record<string, string>>({});
  useEffect(() => {
    const prev = prevStatusesRef.current;
    const curr: Record<string, string> = {};
    for (const j of data?.items ?? []) {
      curr[j.id] = j.status;
      const before = prev[j.id];
      if (before && before !== j.status) {
        const shortId = j.id.slice(0, 8);
        if (j.status === "success") {
          toast(t("grok.jobs_toast_success", { id: shortId, type: j.job_type }), "success");
        } else if (j.status === "failed") {
          const parsed = parseErrorCode(j.error_message);
          const hint = parsed && ERROR_HINTS[parsed.code];
          const detail = parsed
            ? `[${parsed.code}] ${hint ?? parsed.rest.slice(0, 80)}`
            : (j.error_message?.slice(0, 90) ?? t("grok.jobs_toast_error_unknown"));
          toast(t("grok.jobs_toast_failed", { id: shortId, detail }), "error");
        } else if (j.status === "cancelled" && before !== "cancelled") {
          toast(t("grok.jobs_toast_cancelled", { id: shortId }), "info");
        }
      }
    }
    prevStatusesRef.current = curr;
  }, [data?.items, t]);

  const [createOpen, setCreateOpen] = useState(false);
  const [drawerId, setDrawerId] = useState<string | null>(null);
  const [galleryJob, setGalleryJob] = useState<Job | null>(null);
  const [editJob, setEditJob] = useState<Job | null>(null);
  const [cloneJob, setCloneJob] = useState<Job | null>(null);
  const [compareOpen, setCompareOpen] = useState(false);
  const [viewMode, setViewMode] = useState<"table" | "grid">(() => {
    // Remember the user's last preference. Default to table since it's
    // the higher-density view operators rely on for triage.
    try { return (localStorage.getItem("grok:jobs-view") as "table" | "grid") ?? "table"; }
    catch { return "table"; }
  });
  useEffect(() => {
    try { localStorage.setItem("grok:jobs-view", viewMode); } catch { /* ignore */ }
  }, [viewMode]);

  const retry = useMutation({
    mutationFn: (id: string) => jobsService.retry(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }),
  });
  const cancel = useMutation({
    mutationFn: (id: string) => jobsService.cancel(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }),
  });
  const remove = useMutation({
    mutationFn: (id: string) => jobsService.remove(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      toast(t("grok.jobs_deleted_toast"), "success");
    },
  });
  const cancelAll = useMutation({
    mutationFn: () => jobsService.cancelAll(),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["jobs"] }),
      toast(`Đã hủy ${res.cancelled} jobs`, "success");
    },
    onError: (e: any) => toast(e?.response?.data?.detail?.message ?? "Cancel all lỗi", "error"),
  });
  const bulkCancel = useMutation({
    mutationFn: (ids: string[]) => jobsService.bulkCancel(ids),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      setSelectedIds(new Set());
      toast(`Đã hủy ${res.cancelled} jobs`, "success");
    },
    onError: (e: any) => toast(e?.response?.data?.detail?.message ?? "Bulk cancel lỗi", "error"),
  });
  const bulkDelete = useMutation({
    mutationFn: (ids: string[]) => jobsService.bulkDelete(ids),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      setSelectedIds(new Set());
      const parts = [`Đã xoá ${r.deleted}`];
      if (r.skipped_in_flight) parts.push(`bỏ qua ${r.skipped_in_flight} đang chạy`);
      if (r.skipped_not_owned) parts.push(`${r.skipped_not_owned} không có quyền`);
      toast(parts.join(" · "), "success");
    },
    onError: (e: any) => toast(e?.response?.data?.detail?.message ?? "Bulk delete lỗi", "error"),
  });

  const items = data?.items ?? [];
  const total = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const inFlightCount = items.filter((j) =>
    ["pending", "queued", "running", "processing_provider"].includes(j.status),
  ).length;

  useEffect(() => { setPage(1); }, [statusFilter, providerFilter, typeFilter, search, pageSize]);
  // Reset bulk selection whenever the visible row set changes so the
  // user can't accidentally apply page-1 selections to page-2 rows.
  useEffect(() => { setSelectedIds(new Set()); }, [statusFilter, providerFilter, typeFilter, search, page, pageSize]);

  // All visible page items are selectable
  const selectableOnPage = data?.items ?? [];
  const allSelected =
    selectableOnPage.length > 0 && selectableOnPage.every((j) => selectedIds.has(j.id));
  const toggleSelectAll = () => {
    if (allSelected) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(selectableOnPage.map((j) => j.id)));
    }
  };
  const selectedInFlight = items.filter((j) =>
    selectedIds.has(j.id) && ["pending", "queued", "running", "processing_provider"].includes(j.status)
  );
  const selectedDeletable = items.filter((j) =>
    selectedIds.has(j.id) && !["running", "processing_provider", "uploading_result"].includes(j.status)
  );
  const toggleRow = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <div className="space-y-5">
      <div className="page-header">
        <div>
          <h1 className="page-title">{t("grok.jobs_title")}</h1>
          <p className="page-subtitle">{t("grok.jobs_subtitle")}</p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          {inFlightCount > 0 && (
            <button
              onClick={async () => {
                if (await confirm({
                  title: "Cancel tất cả?",
                  message: t("grok.jobs_cancel_all_confirm", { count: inFlightCount }),
                  variant: "warning",
                  confirmLabel: "Cancel hết",
                })) cancelAll.mutate();
              }}
              className="btn-secondary text-amber-700 border-amber-200 hover:border-amber-300"
              disabled={cancelAll.isPending}
            >
              <Ban size={15} />
              {cancelAll.isPending
                ? t("grok.jobs_cancel_all_loading")
                : t("grok.jobs_cancel_all", { count: inFlightCount })}
            </button>
          )}
          <button onClick={() => setCreateOpen(true)} className="btn-primary">
            <Plus size={16} />
            {t("grok.jobs_create")}
          </button>
        </div>
      </div>

      <div className="card flex flex-wrap items-center gap-2.5 py-3.5">
        <input
          type="text"
          placeholder={t("grok.jobs_search_placeholder")}
          className="input w-56 input-sm"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select className="input input-sm w-auto" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          {STATUS_FILTERS.map((s) => <option key={s.v} value={s.v}>{s.label}</option>)}
        </select>
        <select className="input input-sm w-auto" value={providerFilter} onChange={(e) => setProviderFilter(e.target.value)}>
          {PROVIDER_FILTERS.map((s) => <option key={s.v} value={s.v}>{s.label}</option>)}
        </select>
        <select className="input input-sm w-auto" value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}>
          {TYPE_FILTERS.map((s) => <option key={s.v} value={s.v}>{s.label}</option>)}
        </select>
        {(search || statusFilter || providerFilter || typeFilter) && (
          <button
            className="btn-ghost btn-sm"
            onClick={() => { setSearch(""); setStatusFilter(""); setProviderFilter(""); setTypeFilter(""); }}
          >
            {t("grok.jobs_clear_filter")}
          </button>
        )}
        <div className="ml-auto flex items-center gap-3">
          <div className="inline-flex rounded-lg border border-slate-200 overflow-hidden">
            <button
              type="button"
              onClick={() => setViewMode("table")}
              className={`px-2 py-1 text-xs flex items-center gap-1 ${viewMode === "table" ? "bg-slate-900 text-white" : "bg-white text-slate-600 hover:bg-slate-50"}`}
              title="Table view"
            >
              <List size={14} /> Table
            </button>
            <button
              type="button"
              onClick={() => setViewMode("grid")}
              className={`px-2 py-1 text-xs flex items-center gap-1 ${viewMode === "grid" ? "bg-slate-900 text-white" : "bg-white text-slate-600 hover:bg-slate-50"}`}
              title="Grid view"
            >
              <LayoutGrid size={14} /> Grid
            </button>
          </div>
          <div className="text-xs text-slate-400 font-medium">
            <span className="text-slate-900 font-bold">{total}</span> {t("grok.jobs_total_suffix")}
          </div>
        </div>
      </div>

      {selectedIds.size > 0 && (
        <div className="card flex items-center justify-between py-3 px-4 bg-rose-50 border border-rose-200">
          <div className="text-sm">
            <strong>{selectedIds.size}</strong> job đã chọn
            <button
              className="ml-3 text-xs text-slate-500 hover:underline"
              onClick={() => setSelectedIds(new Set())}
            >
              Bỏ chọn
            </button>
          </div>
          <div className="flex items-center gap-2">
            {selectedIds.size >= 2 && selectedIds.size <= 4 && (
              <button
                type="button"
                className="btn-secondary"
                onClick={() => setCompareOpen(true)}
              >
                So sánh {selectedIds.size}
              </button>
            )}
            {selectedInFlight.length > 0 && (
              <button
                className="btn-secondary text-amber-700 border-amber-200 hover:border-amber-300 inline-flex items-center gap-1.5"
                disabled={bulkCancel.isPending}
                onClick={async () => {
                  if (await confirm({
                    title: `Cancel ${selectedInFlight.length} job?`,
                    message: "Các job được chọn đang chạy hoặc đang chờ sẽ bị hủy.",
                    variant: "warning",
                    confirmLabel: `Cancel ${selectedInFlight.length}`,
                  })) bulkCancel.mutate(selectedInFlight.map(j => j.id));
                }}
              >
                <Ban size={14} />
                {bulkCancel.isPending ? "Đang hủy…" : `Cancel ${selectedInFlight.length} job`}
              </button>
            )}
            {selectedDeletable.length > 0 && (
              <button
                className="btn-primary bg-rose-600 hover:bg-rose-700 inline-flex items-center gap-1.5"
                disabled={bulkDelete.isPending}
                onClick={async () => {
                  if (await confirm({
                    title: `Xoá ${selectedDeletable.length} job?`,
                    message: "Các job được chọn sẽ bị xoá vĩnh viễn khỏi hệ thống.",
                    variant: "danger",
                    confirmLabel: `Xoá ${selectedDeletable.length}`,
                  })) bulkDelete.mutate(selectedDeletable.map(j => j.id));
                }}
              >
                <Trash2 size={14} />
                {bulkDelete.isPending ? "Đang xoá…" : `Xoá ${selectedDeletable.length} job`}
              </button>
            )}
          </div>
        </div>
      )}

      {isLoading ? (
        <p className="text-slate-500">{t("common.loading")}</p>
      ) : viewMode === "grid" ? (
        <JobGrid
          items={items}
          onView={setDrawerId}
          onGallery={setGalleryJob}
          onClone={setCloneJob}
          onRetry={(id) => retry.mutate(id)}
          onEdit={setEditJob}
          onRemove={(j) => remove.mutate(j.id)}
          selectedIds={selectedIds}
          onToggleSelect={toggleRow}
        />
      ) : (
        <div className="card overflow-x-auto p-0">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-left">
              <tr>
                <th className="px-3 py-2 w-8">
                  <input
                    type="checkbox"
                    checked={allSelected}
                    onChange={toggleSelectAll}
                    disabled={selectableOnPage.length === 0}
                    title={allSelected ? "Bỏ chọn tất cả" : "Chọn tất cả"}
                    className="cursor-pointer"
                  />
                </th>
                <th className="px-3 py-2">{t("grok.jobs_th_id")}</th>
                <th className="px-3 py-2">{t("grok.jobs_th_provider")}</th>
                <th className="px-3 py-2">{t("grok.jobs_th_type")}</th>
                <th className="px-3 py-2">{t("grok.jobs_th_prompt")}</th>
                <th className="px-3 py-2">{t("grok.jobs_th_status")}</th>
                <th className="px-3 py-2">{t("grok.jobs_th_created")}</th>
                <th className="px-3 py-2">{t("grok.jobs_th_actions")}</th>
              </tr>
            </thead>
            <tbody>
              {items.map((j) => {
                const parsed = parseErrorCode(j.error_message);
                const editable = ["pending", "queued"].includes(j.status);
                const cancellable = ["pending", "queued", "running", "processing_provider"].includes(j.status);
                const retryable = ["failed", "cancelled"].includes(j.status);
                const deletable = !["running", "processing_provider", "uploading_result"].includes(j.status);
                return (
                  <tr key={j.id} className={`border-t hover:bg-slate-50 ${selectedIds.has(j.id) ? "bg-rose-50/50" : ""}`}>
                    <td className="px-3 py-2">
                      <input
                        type="checkbox"
                        checked={selectedIds.has(j.id)}
                        onChange={() => toggleRow(j.id)}
                        title="Chọn để thao tác hàng loạt"
                        className="cursor-pointer"
                      />
                    </td>
                    <td className="px-3 py-2 font-mono text-xs">{j.id.slice(0, 8)}</td>
                    <td className="px-3 py-2">{j.provider}</td>
                    <td className="px-3 py-2">{j.job_type}</td>
                    <td className="px-3 py-2 max-w-xs">
                      <div className="truncate" title={j.prompt}>{j.prompt}</div>
                      {parsed && j.status === "failed" && (
                        <div className="text-xs text-rose-600 mt-0.5 truncate" title={j.error_message ?? ""}>
                          ⚠ [{parsed.code}] {ERROR_HINTS[parsed.code] ?? parsed.rest}
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-2"><StatusBadge status={j.status} /></td>
                    <td className="px-3 py-2 text-slate-500 text-xs whitespace-nowrap">
                      {new Date(j.created_at).toLocaleString()}
                    </td>
                    <td className="px-3 py-2 whitespace-nowrap">
                      <div className="flex items-center gap-1">
                        <button
                          className="p-1.5 rounded hover:bg-slate-200 text-slate-600"
                          title={t("grok.jobs_view")}
                          onClick={() => setDrawerId(j.id)}
                        >
                          <Eye size={16} />
                        </button>
                        {j.status === "success" && (
                          <button
                            className="p-1.5 rounded hover:bg-emerald-100 text-emerald-600"
                            title={t("grok.jobs_view_result")}
                            onClick={() => setGalleryJob(j)}
                          >
                            <Images size={16} />
                          </button>
                        )}
                        <button
                          className={`p-1.5 rounded ${editable ? "hover:bg-amber-100 text-amber-600" : "text-slate-400 cursor-not-allowed"}`}
                          title={editable ? t("grok.jobs_edit_prompt") : t("grok.jobs_edit_locked")}
                          disabled={!editable}
                          onClick={() => editable && setEditJob(j)}
                        >
                          <Pencil size={16} />
                        </button>
                        <button
                          className={`p-1.5 rounded ${retryable ? "hover:bg-blue-100 text-blue-600" : "text-slate-400 cursor-not-allowed"}`}
                          title={retryable ? t("grok.jobs_retry_title") : t("grok.jobs_retry_locked")}
                          disabled={!retryable}
                          onClick={() => retryable && retry.mutate(j.id)}
                        >
                          <RefreshCw size={16} />
                        </button>
                        <button
                          className="p-1.5 rounded hover:bg-indigo-100 text-indigo-600"
                          title="Re-run với prompt này"
                          onClick={() => setCloneJob(j)}
                        >
                          <Copy size={16} />
                        </button>
                        {cancellable && (
                          <button
                            className="p-1.5 rounded hover:bg-amber-100 text-amber-600"
                            title={t("grok.jobs_cancel_title")}
                            onClick={async () => {
                              if (await confirm({
                                message: t("grok.jobs_cancel_confirm"),
                                variant: "warning",
                                confirmLabel: "Cancel job",
                              })) cancel.mutate(j.id);
                            }}
                          >
                            <Ban size={16} />
                          </button>
                        )}
                        <button
                          className={`p-1.5 rounded ${deletable ? "hover:bg-rose-100 text-rose-600" : "text-slate-400 cursor-not-allowed"}`}
                          title={deletable ? t("grok.jobs_delete_title") : t("grok.jobs_delete_locked")}
                          disabled={!deletable}
                          onClick={async () => {
                            if (!deletable) return;
                            if (await confirm({
                              title: "Xoá job?",
                              message: t("grok.jobs_delete_confirm", { id: j.id.slice(0, 8) }),
                              variant: "danger",
                              confirmLabel: "Xoá",
                            })) remove.mutate(j.id);
                          }}
                        >
                          <Trash2 size={16} />
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
              {items.length === 0 && (
                <tr>
                  <td colSpan={8} className="px-3 py-6 text-center text-slate-500">
                    {t("grok.jobs_empty")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>

          <div className="flex items-center justify-between border-t px-3 py-2 bg-slate-50 text-sm">
            <div className="flex items-center gap-2">
              <span className="text-slate-500">{t("grok.jobs_per_page")}</span>
              <select
                className="input w-auto py-1 text-xs"
                value={pageSize}
                onChange={(e) => setPageSize(Number(e.target.value))}
              >
                {PAGE_SIZES.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </div>
            <div className="text-slate-500 text-xs">
              {total === 0 ? "0" : `${offset + 1}–${Math.min(offset + items.length, total)}`} / {total}
            </div>
            <div className="flex items-center gap-1">
              <button
                className="btn-ghost px-2 py-1 disabled:opacity-30"
                disabled={page <= 1}
                onClick={() => setPage(1)}
                title={t("grok.jobs_pg_first")}
              >
                <ChevronsLeft size={14} />
              </button>
              <button
                className="btn-ghost px-2 py-1 disabled:opacity-30"
                disabled={page <= 1}
                onClick={() => setPage((p) => p - 1)}
                title={t("grok.jobs_pg_prev")}
              >
                <ChevronLeft size={14} />
              </button>
              <span className="px-2 text-xs">
                {page} / {totalPages}
              </span>
              <button
                className="btn-ghost px-2 py-1 disabled:opacity-30"
                disabled={page >= totalPages}
                onClick={() => setPage((p) => p + 1)}
                title={t("grok.jobs_pg_next")}
              >
                <ChevronRight size={14} />
              </button>
              <button
                className="btn-ghost px-2 py-1 disabled:opacity-30"
                disabled={page >= totalPages}
                onClick={() => setPage(totalPages)}
                title={t("grok.jobs_pg_last")}
              >
                <ChevronsRight size={14} />
              </button>
            </div>
          </div>
        </div>
      )}

      {createOpen && <CreateJobModal onClose={() => setCreateOpen(false)} />}
      {cloneJob && (
        <CreateJobModal
          onClose={() => setCloneJob(null)}
          cloneFrom={{
            prompt: cloneJob.prompt,
            provider: cloneJob.provider,
            job_type: cloneJob.job_type,
          }}
        />
      )}
      {compareOpen && (
        <CompareJobsModal
          jobs={items.filter((j) => selectedIds.has(j.id))}
          onClose={() => setCompareOpen(false)}
        />
      )}
      {drawerId && <JobDetailDrawer jobId={drawerId} onClose={() => setDrawerId(null)} />}
      {galleryJob && (
        <ResultGalleryModal
          jobId={galleryJob.id}
          jobType={galleryJob.job_type}
          onClose={() => setGalleryJob(null)}
        />
      )}
      {editJob && <EditJobModal job={editJob} onClose={() => setEditJob(null)} />}
    </div>
  );
}
