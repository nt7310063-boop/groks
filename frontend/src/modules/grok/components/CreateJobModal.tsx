import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useForm, Controller } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { Image as ImageIcon, Lock, X } from "lucide-react";
import { useFeature } from "@/core/auth/store";
import { FEATURE_KEYS } from "@/core/entitlements/catalog";
import { toast } from "@/components/ui/Toast";
import type { Profile } from "../models/profile";
import { jobsService } from "../services/jobs.service";
import { profilesService } from "../services/profiles.service";
import { projectsService } from "../services/projects.service";
import { ASPECT_OPTIONS, SIZES_FROM_ASPECT } from "../configs/aspects";
import type { CreateJobForm } from "./CreateJobForm.types";
import { CreateJobReferenceImagePicker, type InputImage } from "./CreateJobReferenceImagePicker";
import { CreateJobImageFields } from "./CreateJobImageFields";
import { PromptHistoryDropdown, rememberPrompt } from "./PromptHistoryDropdown";
import {
  CreateJobVideoResolutionAndDuration,
  CreateJobVideoModeField,
} from "./CreateJobVideoFields";

// Grok web UI doesn't expose model / style / variant-count / seed pickers.
// Form schema still includes those fields so the existing Pydantic
// JobCreate payload validates, but they're hidden from the UI to match
// Grok's actual prompt bar.

export function CreateJobModal({
  onClose,
  cloneFrom,
}: {
  onClose: () => void;
  cloneFrom?: { prompt: string; provider: string; job_type: string };
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  // Multi-reference picker (up to 4). Single-ref legacy users see no
  // change — they upload one image, the array is length 1, the submit
  // path still produces a usable job.
  const [inputImages, setInputImages] = useState<InputImage[]>([]);
  // True while ANY reference-image upload is in flight. The Submit
  // button reads this to refuse firing — without the gate, operators
  // hit Submit before /api/jobs/upload-input returns, the job row is
  // created with no file_id attached, and Grok ends up asking the
  // user for the image they thought they sent (see job 9914a926).
  const [uploadPending, setUploadPending] = useState(false);

  // Entitlements — gate UI options to what the user's plan allows.
  const canImage = useFeature(FEATURE_KEYS.jobImage);
  const canVideo = useFeature(FEATURE_KEYS.jobVideo);
  const canImg2Img = useFeature(FEATURE_KEYS.jobImageToImage);
  const canImg2Vid = useFeature(FEATURE_KEYS.jobImageToVideo);
  const canQualityHigh = useFeature(FEATURE_KEYS.imageQualityHigh);
  const can720p = useFeature(FEATURE_KEYS.videoResolution720p);
  const can10s = useFeature(FEATURE_KEYS.videoDuration10s);
  const canSpicy = useFeature(FEATURE_KEYS.videoSpicy);
  const canFun = useFeature(FEATURE_KEYS.videoFunMode);
  const canCustom = useFeature(FEATURE_KEYS.videoCustomMode);

  const { data: profiles } = useQuery({
    queryKey: ["profiles"],
    queryFn: () => profilesService.list(),
  });

  const { register, handleSubmit, watch, control, setValue, formState: { isSubmitting } } = useForm<CreateJobForm>({
    defaultValues: {
      provider: (cloneFrom?.provider as "grok" | "flow") ?? "grok",
      job_type: (cloneFrom?.job_type as "image" | "video") ?? "image",
      prompt: cloneFrom?.prompt ?? "",
      profile_id: "", project_id: "",
      size: "1024x1024", aspect: "1:1",
      quality: "speed",                  // image-only
      resolution: "720p", duration: 6,   // video-only
      mode: "normal", model: "aurora", style: "natural", n: 1, seed: null,
    },
  });
  const provider = watch("provider");
  const jobType = watch("job_type");
  const aspect = watch("aspect");
  const profileIdWatch = watch("profile_id");

  // Project list — only fetched when the user has picked an explicit
  // profile. When `profile_id` is empty (auto-pick) we hide the project
  // dropdown because the backend's per-user / per-domain rules need to
  // run first to know which profile gets the job.
  const { data: projects } = useQuery({
    queryKey: ["grok-projects", profileIdWatch || "none"],
    queryFn: () => projectsService.list(profileIdWatch),
    enabled: !!profileIdWatch,
  });

  // Reset project when profile changes so a stale project from a
  // previous profile doesn't leak in.
  useEffect(() => {
    setValue("project_id", "");
  }, [profileIdWatch, setValue]);

  // If the user picked an image-only profile and then flips to Video,
  // silently drop the selection back to Auto pick. Without this the
  // submit would hit the backend's 422 — worse UX than just clearing it.
  useEffect(() => {
    if (jobType !== "video" || !profileIdWatch) return;
    const picked = (profiles ?? []).find((p) => p.id === profileIdWatch);
    if (picked && picked.allows_video === false) {
      setValue("profile_id", "");
    }
  }, [jobType, profileIdWatch, profiles, setValue]);

  useEffect(() => {
    if (provider === "flow") setValue("job_type", "video");
    setValue("model", provider === "grok" ? "aurora" : "veo-3");
  }, [provider, setValue]);

  // Default the form to whichever job_type the user actually has access to.
  useEffect(() => {
    if (jobType === "image" && !canImage && canVideo) setValue("job_type", "video");
    if (jobType === "video" && !canVideo && canImage) setValue("job_type", "image");
  }, [canImage, canVideo, jobType, setValue]);

  // Drop ineligible defaults so the form doesn't submit a blocked option.
  useEffect(() => {
    if (!canQualityHigh) setValue("quality", "speed");
    if (!can720p) setValue("resolution", "480p");
    if (!can10s) setValue("duration", 6);
  }, [canQualityHigh, can720p, can10s, setValue]);

  // Keep `size` in sync with `aspect` (size is what backend currently uses;
  // aspect is what we display + pass through as a hint).
  useEffect(() => {
    if (aspect && SIZES_FROM_ASPECT[aspect]) {
      setValue("size", SIZES_FROM_ASPECT[aspect]);
    }
  }, [aspect, setValue]);

  // Pre-filter: only profiles for the chosen provider. The dropdown
  // further narrows down to "selectable" ones (status ready, video flag
  // if needed) — non-selectable profiles are hidden entirely rather
  // than shown greyed-out, since they're noise to end users who can't
  // do anything about them anyway.
  const eligibleProfiles = (profiles ?? []).filter((p) => p.provider === provider);

  // Preview which profile the backend WOULD pick when "Auto pick" is
  // selected. Mirrors backend's _resolve_profile_for_job rules:
  //   - provider match
  //   - status in {logged_in, running_job}
  //   - if video job: allows_video must be true
  //   - ordered by least-loaded (active_video_jobs for video, active_jobs
  //     otherwise), tie-broken by oldest last_used_at
  // Shown as a hint below the dropdown so the user sees the routing
  // decision before submitting. Not a contract — the actual choice is
  // re-resolved server-side at create time (state may change in between).
  const autoPickPreview = (() => {
    if (profileIdWatch) return null; // user picked manually
    const pool = eligibleProfiles.filter((p) => {
      const ready = p.status === "logged_in" || p.status === "running_job";
      if (!ready) return false;
      if (jobType === "video" && p.allows_video === false) return false;
      return true;
    });
    if (pool.length === 0) return null;
    const sorted = [...pool].sort((a, b) => {
      // Mirror backend tier ordering: video prefers heavy (first), image
      // avoids heavy (last) but still allows it as a fallback.
      const tierRank = (p: Profile) =>
        jobType === "video" ? (p.tier === "heavy" ? 0 : 1) : (p.tier === "heavy" ? 1 : 0);
      if (tierRank(a) !== tierRank(b)) return tierRank(a) - tierRank(b);
      const aLoad = jobType === "video" ? (a.active_video_jobs ?? 0) : a.active_jobs;
      const bLoad = jobType === "video" ? (b.active_video_jobs ?? 0) : b.active_jobs;
      if (aLoad !== bLoad) return aLoad - bLoad;
      const aTime = a.last_used_at ? new Date(a.last_used_at).getTime() : 0;
      const bTime = b.last_used_at ? new Date(b.last_used_at).getTime() : 0;
      return aTime - bTime;
    });
    return sorted[0];
  })();

  const onSubmit = async (v: CreateJobForm) => {
    // Hard refuse if a reference upload is still in flight — see the
    // uploadPending state above for context.
    if (uploadPending) {
      toast("Đang upload ảnh tham chiếu — chờ xong rồi submit lại", "info");
      return;
    }
    const payload: any = {
      provider: v.provider, job_type: v.job_type, prompt: v.prompt,
      size: v.size, model: v.model, style: v.style, n: Number(v.n),
      // Pass aspect/quality/duration through `options` JSONB so provider
      // can consume them. Backend's create_job merges these into options.
      options: {
        aspect: v.aspect,
        // Match Grok's actual prompt-bar controls:
        //   image → Speed | Quality (no resolution/duration)
        //   video → 480p | 720p, 6s | 10s (no Speed/Quality)
        ...(v.job_type === "image"
          ? { quality: v.quality }
          : {
              resolution: v.resolution,
              duration: Number(v.duration),
              mode: v.mode,
            }),
      },
    };
    if (v.profile_id) payload.profile_id = v.profile_id;
    if (v.project_id) payload.project_id = v.project_id;
    if (v.seed != null && Number(v.seed) > 0) payload.seed = Number(v.seed);
    if (inputImages.length === 1) {
      // Stay on the single-ref field so legacy parts of the system
      // (entitlement checks, status formatters that read input_image_file_id)
      // keep behaving exactly the same for the common 1-image case.
      payload.input_image_file_id = inputImages[0].file_id;
    } else if (inputImages.length > 1) {
      payload.reference_images = inputImages.map((i) => i.file_id);
    }
    try {
      await jobsService.create(payload);
      rememberPrompt(v.prompt, v.job_type);
    } catch (e: any) {
      const msg = e?.response?.data?.detail?.message ?? e?.message ?? t("grok.create_job_error");
      toast(msg, "error");
      return;
    }
    qc.invalidateQueries({ queryKey: ["jobs"] });
    toast(t("grok.create_job_queued"), "success");
    onClose();
  };

  // A profile can accept a job whenever it's "ready" (logged in or already
  // running another job). Even if all slots are currently occupied the
  // backend will queue the new job — concurrency is a runtime gate, not a
  // queue-admission gate.
  //
  // When the user picks job_type=video, also exclude image-only profiles
  // (allows_video=false). The backend rejects this combo with 422, but
  // disabling in the UI saves the round-trip and makes the rule visible.
  const profileSelectable = (p: Profile) => {
    const ready = p.status === "logged_in" || p.status === "running_job";
    if (!ready) return false;
    if (jobType === "video" && p.allows_video === false) return false;
    return true;
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-white/50 backdrop-blur-sm p-4 animate-fade-in">
      <form
        onSubmit={handleSubmit(onSubmit)}
        className="w-full max-w-3xl rounded-2xl bg-white p-6 shadow-card-hover space-y-4 max-h-[95vh] overflow-auto animate-scale-in border border-slate-200/60"
      >
        <div className="flex items-center justify-between pb-3 border-b border-slate-200">
          <div>
            <h2 className="text-xl font-bold text-slate-900 flex items-center gap-2">
              <span className="w-8 h-8 rounded-lg bg-gradient-brand text-white flex items-center justify-center">
                <ImageIcon size={16} />
              </span>
              {t("grok.create_job_title")}
            </h2>
            <p className="text-xs text-slate-400 mt-1">{t("grok.create_job_subtitle")}</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="w-8 h-8 rounded-lg text-slate-500 hover:text-slate-900 hover:bg-slate-100 flex items-center justify-center transition-colors"
            aria-label="Close"
          >
            <X size={18} />
          </button>
        </div>

        <div className="grid grid-cols-3 gap-3">
          <div>
            <label className="text-sm font-medium">{t("grok.create_job_provider")}</label>
            <select className="input" {...register("provider")}>
              <option value="grok">Grok</option>
              <option value="flow">{t("grok.create_job_provider_flow")}</option>
            </select>
          </div>
          <div>
            <label className="text-sm font-medium">{t("grok.create_job_type")}</label>
            <select className="input" {...register("job_type")} disabled={provider === "flow"}>
              <option value="image" disabled={!canImage}>{t("grok.create_job_type_image")}{!canImage ? " 🔒" : ""}</option>
              <option value="video" disabled={!canVideo}>{t("grok.create_job_type_video")}{!canVideo ? " 🔒" : ""}</option>
            </select>
            {provider === "grok" && jobType === "video" && (
              <p className="text-xs text-amber-600 mt-1">{t("grok.create_job_need_video_perm")}</p>
            )}
            {!canImage && !canVideo && (
              <p className="text-xs text-rose-600 mt-1">
                <Lock size={12} className="inline" /> {t("grok.create_job_plan_no_create")}
              </p>
            )}
          </div>
          <div>
            <label className="text-sm font-medium">{t("grok.create_job_profile")}</label>
            <Controller
              control={control}
              name="profile_id"
              render={({ field }) => (
                <select className="input" {...field}>
                  <option value="">{t("grok.create_job_auto_pick")}</option>
                  {eligibleProfiles
                    .filter(profileSelectable)
                    .map((p) => {
                      const slots = `${p.active_jobs}/${p.max_concurrent_jobs}`;
                      const full = p.active_jobs >= p.max_concurrent_jobs;
                      // Selectable profiles can still be "full" — let the
                      // user know the job will queue rather than start
                      // immediately. Other states are hidden by the filter
                      // above so don't need annotation here.
                      const note = full ? ` — ${t("grok.create_job_full_will_queue")}` : "";
                      return (
                        <option key={p.id} value={p.id}>
                          {p.name} [{slots}{note}]
                        </option>
                      );
                    })}
                </select>
              )}
            />
            {eligibleProfiles.length === 0 && (
              <p className="text-xs text-amber-600 mt-1">{t("grok.create_job_no_profile", { provider })}</p>
            )}
            {eligibleProfiles.length > 0 && eligibleProfiles.every((p) => !profileSelectable(p)) && (
              <p className="text-xs text-rose-600 mt-1">
                {jobType === "video"
                  && eligibleProfiles.every(
                    (p) => (p.status === "logged_in" || p.status === "running_job") && p.allows_video === false,
                  )
                  ? t("grok.create_job_all_image_only")
                  : t("grok.create_job_profile_not_logged_in", { provider })}
              </p>
            )}
            {autoPickPreview && (
              <p className="text-xs text-emerald-600 mt-1">
                {t("grok.create_job_auto_pick_preview", {
                  name: autoPickPreview.name,
                  load: jobType === "video"
                    ? `${autoPickPreview.active_video_jobs ?? 0}/${autoPickPreview.max_concurrent_video ?? 4} video`
                    : `${autoPickPreview.active_jobs}/${autoPickPreview.max_concurrent_jobs} image`,
                })}
              </p>
            )}
          </div>
          {profileIdWatch && (projects?.length ?? 0) > 0 && (
            <div className="sm:col-span-2">
              <label className="text-sm font-medium">Grok Project</label>
              <Controller
                control={control}
                name="project_id"
                render={({ field }) => (
                  <select className="input" {...field}>
                    <option value="">Auto pick (theo pin user → domain)</option>
                    {(projects ?? []).map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name} — {p.grok_project_id.slice(0, 12)}…
                      </option>
                    ))}
                  </select>
                )}
              />
              <p className="text-xs text-slate-500 mt-1">
                Bỏ trống = backend pick theo rule. Chọn 1 project = ép worker chạy trong project đó.
              </p>
            </div>
          )}
        </div>

        <div>
          <div className="flex items-center justify-between mb-1">
            <label className="text-sm font-medium">{t("grok.create_job_prompt")}</label>
            <PromptHistoryDropdown
              jobType={jobType}
              onPick={(p) => setValue("prompt", p, { shouldDirty: true })}
            />
          </div>
          <textarea
            className="input min-h-[100px]"
            placeholder={t("grok.create_job_prompt_placeholder")}
            {...register("prompt", { required: true, maxLength: 16000 })}
          />
        </div>

        {(jobType === "image" || (provider === "grok" && jobType === "video")) && (
          <CreateJobReferenceImagePicker
            jobType={jobType}
            allowed={jobType === "image" ? canImg2Img : canImg2Vid}
            value={inputImages}
            onChange={setInputImages}
            onPendingChange={setUploadPending}
          />
        )}

        <div className="grid grid-cols-4 gap-3">
          <div>
            <label className="text-sm font-medium">{t("grok.create_job_aspect")}</label>
            <select className="input" {...register("aspect")}>
              {ASPECT_OPTIONS.map((a) => <option key={a.v} value={a.v}>{a.label}</option>)}
            </select>
          </div>

          {/* Image-only: Speed / Quality (matches Grok's prompt bar in Image mode) */}
          {jobType === "image" && (
            <CreateJobImageFields register={register} canQualityHigh={canQualityHigh} />
          )}

          {/* Video-only: Resolution 480p|720p + Duration 6s|10s */}
          {jobType === "video" && (
            <CreateJobVideoResolutionAndDuration
              register={register}
              can720p={can720p}
              can10s={can10s}
            />
          )}

        </div>

        {jobType === "video" && (
          <CreateJobVideoModeField
            register={register}
            canSpicy={canSpicy}
            canFun={canFun}
            canCustom={canCustom}
          />
        )}

        <p className="text-xs text-slate-500">
          {t("grok.create_job_variants_note_prefix")} <strong>{t("grok.create_job_variants_note_strong")}</strong> {t("grok.create_job_variants_note_suffix")}
        </p>

        <div className="flex justify-end gap-2 pt-3 border-t">
          <button type="button" onClick={onClose} className="btn-ghost">{t("grok.create_job_cancel")}</button>
          <button className="btn-primary" disabled={isSubmitting || uploadPending}>
            {uploadPending
              ? "Đang upload ảnh…"
              : isSubmitting
              ? t("grok.create_job_submitting")
              : t("grok.create_job_submit")}
          </button>
        </div>
      </form>
    </div>
  );
}
