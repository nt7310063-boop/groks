import { api } from "@/core/api/axios";

import type { Job, JobFile, JobLog, JobOut } from "../models/job";

export const jobsService = {
  /** Paged list — returns axios response so callers can read x-total-count.
   *  Used by JobsPage which needs the header for pagination. */
  listRaw: (qs: string) => api.get<Job[]>(`/api/jobs?${qs}`),

  get: (id: string) => api.get<Job>(`/api/jobs/${id}`).then((r) => r.data),

  files: (id: string) => api.get<JobFile[]>(`/api/jobs/${id}/files`).then((r) => r.data),

  logs: (id: string) => api.get<JobLog[]>(`/api/jobs/${id}/logs`).then((r) => r.data),

  create: (payload: Record<string, unknown>) => api.post("/api/jobs", payload),

  /** One-off submit using a Bearer API key (Playground path) so the job
   *  lands under the key's owner instead of the logged-in JWT user.
   *  Uses the main `api` instance with a per-request Authorization
   *  override — no new axios instance, matching module rules. */
  submitWithKey: (bearer: string, payload: Record<string, unknown>) =>
    api
      .post<JobOut>("/api/jobs", payload, {
        headers: { Authorization: `Bearer ${bearer}` },
      })
      .then((r) => r.data),

  /** Playground admin / JWT-authed submit. */
  submit: (payload: Record<string, unknown>) =>
    api.post<JobOut>("/api/jobs", payload).then((r) => r.data),

  update: (id: string, payload: Record<string, unknown>) =>
    api.patch(`/api/jobs/${id}`, payload),

  retry: (id: string) => api.post(`/api/jobs/${id}/retry`),

  /** Flip favorite flag — the response carries the post-toggle state. */
  toggleFavorite: (id: string) =>
    api.post<Job>(`/api/jobs/${id}/favorite`).then((r) => r.data),

  /** Replace the full tag set on a job. */
  setTags: (id: string, tags: string[]) =>
    api.put<Job>(`/api/jobs/${id}/tags`, { tags }).then((r) => r.data),
  cancel: (id: string) => api.post(`/api/jobs/${id}/cancel`),
  cancelAll: () =>
    api.post<{ cancelled: number }>("/api/jobs/cancel-all").then((r) => r.data),
  bulkCancel: (ids: string[]) =>
    api
      .post<{ cancelled: number }>("/api/jobs/bulk-cancel", { ids })
      .then((r) => r.data),
  remove: (id: string) => api.delete(`/api/jobs/${id}`),

  /** Delete many jobs in one request. Backend skips in-flight rows and
   *  rows the caller doesn't own; the response counts each bucket. */
  bulkDelete: (ids: string[]) =>
    api
      .post<{ deleted: number; skipped_in_flight: number; skipped_not_owned: number }>(
        "/api/jobs/bulk-delete",
        { ids },
      )
      .then((r) => r.data),

  /** Multipart upload of a reference image — returns the file_id to
   *  attach via `input_image_file_id`. */
  uploadInput: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    // Important: do NOT set Content-Type — letting axios/browser auto-add the
    // multipart boundary header. Setting it manually breaks FastAPI's
    // multipart parser (no boundary token → 422).
    return api
      .post<{ file_id: string; file_name: string }>("/api/jobs/upload-input", fd)
      .then((r) => r.data);
  },
};
