import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRight,
  Check,
  Clock3,
  FlaskConical,
  GitBranch,
  Info,
  Layers3,
  LoaderCircle,
  PackageCheck,
  PackageOpen,
  ShieldCheck,
  OctagonX,
  X,
} from "lucide-react";
import type { ColumnDef } from "@tanstack/react-table";
import type { Campaign, Resolution } from "@/api/types";
import {
  abortRelease,
  ApiError,
  createRelease,
  isMock,
  resolveRelease,
  useApi,
} from "@/api/client";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  AlertDialogRoot,
  AlertDialogTrigger,
  AlertDialogContent,
  AlertDialogTitle,
  AlertDialogDescription,
  AlertDialogCancel,
  AlertDialogAction,
} from "@/components/ui/alert-dialog";
import { DataTable } from "@/components/data-table";
import {
  PageHeading,
  Loading,
  ErrorState,
  ExternalLink,
  Sha,
  RelativeTime,
  EmptyState,
} from "@/components/common";
import { sentenceCase } from "@/lib/utils";
import { StatusBadge, normalizeStatus } from "@/components/status";
function stageRun(
  value: unknown,
): { url: string | null; run_id: string; conclusion: string | null } | null {
  if (!value || typeof value !== "object") return null;
  const r = value as Record<string, unknown>;
  return {
    url:
      typeof r.url === "string"
        ? r.url
        : typeof r.html_url === "string"
          ? r.html_url
          : null,
    run_id: String(r.run_id ?? r.id ?? "—"),
    conclusion: typeof r.conclusion === "string" ? r.conclusion : null,
  };
}
const liveStatuses = ["starting", "running", "paused"];
const stages = [
  { key: "push", label: "Push CI", icon: GitBranch },
  { key: "hourly", label: "Hourly CI", icon: Clock3 },
  { key: "nightly", label: "Nightly CI", icon: FlaskConical },
  { key: "complete", label: "Complete", icon: PackageCheck },
];
const columns: ColumnDef<Campaign>[] = [
  {
    accessorKey: "id",
    header: "Campaign",
    cell: ({ row }) => (
      <div>
        <ExternalLink href={row.original.branch_url}>
          {row.original.id}
        </ExternalLink>
        <p className="text-[9px] text-muted-foreground mt-1">
          {row.original.branch_url?.split("/tree/")[1] ?? "Stabilize branch"}
        </p>
      </div>
    ),
  },
  {
    accessorKey: "status",
    header: "Outcome",
    cell: ({ row }) => (
      <StatusBadge
        value={row.original.status}
        label={
          row.original.status === "complete"
            ? "Complete"
            : row.original.status === "aborted"
              ? "Aborted"
              : undefined
        }
      />
    ),
  },
  { accessorKey: "round", header: "Rounds" },
  {
    accessorKey: "tip_sha",
    header: "Final tip",
    cell: ({ row }) => <Sha value={row.original.tip_sha} />,
  },
  {
    accessorKey: "finished_at",
    header: "Finished",
    cell: ({ row }) => (
      <span className="text-muted-foreground">
        <RelativeTime value={row.original.finished_at} />
      </span>
    ),
  },
];
function LiveCampaign({
  campaign,
  canRelease,
}: {
  campaign: Campaign;
  canRelease: boolean;
}) {
  const client = useQueryClient();
  const abort = useMutation({
    mutationFn: () => abortRelease(campaign.id),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["/releases"] });
      client.invalidateQueries({ queryKey: ["release-anchor"] });
    },
  });
  const currentIndex = stages.findIndex((s) => s.key === campaign.stage);
  return (
    <Card>
      <CardHeader>
        <div>
          <div className="metadata-label mb-2">Live campaign</div>
          <CardTitle className="text-base">{campaign.id}</CardTitle>
          <p className="section-caption">
            Round {campaign.round} <span className="mx-1">·</span> Started{" "}
            <RelativeTime value={campaign.created_at} />
          </p>
        </div>
        <StatusBadge
          value={campaign.status}
          label={
            campaign.status === "paused"
              ? "Paused"
              : campaign.status === "starting"
                ? "Starting"
                : "Running"
          }
        />
      </CardHeader>
      <CardContent>
        <div className="stage-pipeline mt-4">
          {stages.map((s, i) => {
            const run = stageRun(campaign.latest_runs?.[s.key]);
            const done = i < currentIndex;
            return (
              <div
                key={s.key}
                className={`stage-node ${done ? "done" : ""} ${i === currentIndex ? "current" : ""}`}
              >
                <div className="stage-icon">
                  {done ? <Check size={14} /> : <s.icon size={14} />}
                </div>
                {s.label}
                <div className="mt-1.5 font-normal text-[9px] min-h-4">
                  {run ? (
                    <ExternalLink href={run.url}>
                      {done ? "Passed" : (run.conclusion ?? "Running")} #
                      {String(run.run_id).slice(-4)}
                    </ExternalLink>
                  ) : i === 3 ? (
                    campaign.status === "complete" ? (
                      "Complete"
                    ) : (
                      "Not complete"
                    )
                  ) : (
                    "Not started"
                  )}
                </div>
              </div>
            );
          })}
        </div>
        <div className="flex flex-wrap items-center justify-between gap-4 border-t pt-4">
          <div className="flex items-center gap-6">
            <div>
              <div className="metadata-label">Current tip</div>
              <Sha value={campaign.tip_sha} />
            </div>
            <div>
              <div className="metadata-label">Branch</div>
              <div className="text-[10px]">
                <ExternalLink href={campaign.branch_url}>
                  {campaign.branch_url?.split("/tree/")[1] ?? campaign.id}
                </ExternalLink>
              </div>
            </div>
          </div>
          <div className="flex gap-2 items-center">
            {campaign.slack_thread_url && (
              <ExternalLink
                href={campaign.slack_thread_url}
                className="text-[10px] text-muted-foreground"
              >
                Slack thread
              </ExternalLink>
            )}
            {canRelease && (
              <AlertDialogRoot>
                <AlertDialogTrigger asChild>
                  <Button
                    variant="destructive"
                    size="sm"
                    disabled={abort.isPending}
                  >
                    <OctagonX size={14} />
                    {abort.isPending ? "Aborting…" : "Abort campaign"}
                  </Button>
                </AlertDialogTrigger>
                <AlertDialogContent>
                  <AlertDialogTitle className="font-semibold">
                    Abort this stabilization campaign?
                  </AlertDialogTitle>
                  <AlertDialogDescription className="text-sm text-muted-foreground leading-relaxed">
                    This stops campaign {campaign.id} from advancing through its
                    remaining CI stages. Confirm before ending the live release
                    campaign.
                  </AlertDialogDescription>
                  <div className="flex justify-end gap-2">
                    <AlertDialogCancel asChild>
                      <Button variant="outline">Keep running</Button>
                    </AlertDialogCancel>
                    <AlertDialogAction asChild>
                      <Button
                        variant="destructive"
                        onClick={() => abort.mutate()}
                      >
                        Abort campaign
                      </Button>
                    </AlertDialogAction>
                  </div>
                </AlertDialogContent>
              </AlertDialogRoot>
            )}
          </div>
        </div>
        {campaign.pause_reason && (
          <div className="info-banner mt-4">
            <Info size={14} />
            <span>
              <strong className="font-medium text-foreground">
                Campaign paused.
              </strong>{" "}
              {campaign.pause_reason}
            </span>
          </div>
        )}
        {abort.isError && (
          <div role="alert" className="info-banner mt-4">
            {abort.error.message}
          </div>
        )}
        <div className="grid md:grid-cols-2 gap-5 mt-6">
          <div>
            <h3 className="text-[11px] font-semibold mb-3">
              Issues being repaired{" "}
              <span className="ml-1 text-muted-foreground font-normal">
                {(campaign.issues ?? []).length}
              </span>
            </h3>
            {(campaign.issues ?? []).length ? (
              (campaign.issues ?? []).map((issue) => (
                <div key={issue.number} className="border-t py-3 flex gap-2">
                  <StatusBadge
                    value={issue.state === "closed" ? "landed" : "blocked"}
                    label={issue.state === "closed" ? "Closed" : "Open"}
                  />
                  <div className="min-w-0">
                    <div className="text-[11px] font-medium">
                      <ExternalLink href={null}>
                        Issue #{issue.number}
                      </ExternalLink>
                      {issue.pr_number && (
                        <span className="font-normal text-[10px] text-muted-foreground ml-2">
                          Repair PR #{issue.pr_number}
                        </span>
                      )}
                    </div>
                    <p className="text-[9px] font-mono break-all text-muted-foreground mt-1">
                      {issue.source}
                    </p>
                  </div>
                </div>
              ))
            ) : (
              <EmptyState title="No open campaign issues" />
            )}
          </div>
          <div>
            <h3 className="text-[11px] font-semibold mb-3">
              Stage gate pull requests{" "}
              <span className="ml-1 text-muted-foreground font-normal">
                {(campaign.gate_merges ?? []).length}
              </span>
            </h3>
            {(campaign.gate_merges ?? []).length ? (
              (campaign.gate_merges ?? []).map((gate) => (
                <div key={gate.pr_number} className="border-t py-3 flex gap-2">
                  <StatusBadge
                    value={gate.state}
                    label={
                      gate.state
                        ? gate.state[0].toUpperCase() + gate.state.slice(1)
                        : "Unknown"
                    }
                  />
                  <div>
                    <div className="text-[11px] font-medium">
                      <ExternalLink href={null}>
                        {sentenceCase(gate.stage)} gate · #{gate.pr_number}
                      </ExternalLink>
                    </div>
                    <div className="mt-1">
                      <Sha value={gate.tip_sha} />
                    </div>
                  </div>
                </div>
              ))
            ) : (
              <EmptyState title="No passed stage gates yet" />
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
export function ReleasesPage() {
  const client = useQueryClient();
  const me = useApi("/me");
  const history = useApi("/releases");
  const anchor = useQuery({
    queryKey: ["release-anchor"],
    queryFn: () => resolveRelease(),
    retry: false,
  });
  const [commitish, setCommitish] = useState("");
  const [resolution, setResolution] = useState<Resolution | null>(null);
  const idempotencyKeys = useRef(new Map<string, string>());
  const initialized = useRef(false);
  useEffect(() => {
    if (anchor.data && !initialized.current) {
      initialized.current = true;
      setCommitish(anchor.data.sha);
      setResolution(anchor.data);
    }
  }, [anchor.data]);
  const resolve = useMutation({
    mutationFn: () => resolveRelease(commitish.trim() || undefined),
    onSuccess: (data) => setResolution(data),
    onError: () => setResolution(null),
  });
  const create = useMutation({
    mutationFn: () => {
      const sha = resolution!.sha;
      if (!idempotencyKeys.current.has(sha))
        idempotencyKeys.current.set(
          sha,
          sessionStorage.getItem(`mm-release-${sha}`) ?? crypto.randomUUID(),
        );
      sessionStorage.setItem(
        `mm-release-${sha}`,
        idempotencyKeys.current.get(sha)!,
      );
      return createRelease(sha, idempotencyKeys.current.get(sha)!);
    },
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["/releases"] });
      client.invalidateQueries({ queryKey: ["release-anchor"] });
    },
  });
  const campaign = history.data?.find((c) => liveStatuses.includes(c.status));
  const canRelease = me.data?.can_release ?? false;
  const unavailable =
    history.error instanceof ApiError && history.error.status === 503;
  const liveId =
    campaign?.id ??
    (anchor.data ? anchor.data.live_campaign_id : resolution?.live_campaign_id);
  const disabledReason = !canRelease
    ? "Maintain or admin permission is required to create a release."
    : unavailable
      ? "Release machinery is not available on this host yet."
      : liveId
        ? `Campaign ${liveId} is live. Finish or abort it before starting another.`
        : history.isError || anchor.isError
          ? "Campaign state could not be verified. Refresh before creating a release."
          : !resolution
            ? "Resolve a commit on master before creating a release."
            : !resolution.on_master
              ? "The resolved commit must be on master."
              : null;
  return (
    <>
      <PageHeading
        title="Releases"
        description="Stabilization campaigns run push, hourly and nightly CI on a release branch."
        action={
          <span className="flex items-center gap-1.5 text-[10px] text-muted-foreground mt-3">
            <Layers3 size={13} />
            Stabilize campaigns
          </span>
        }
      />
      {unavailable && (
        <div role="status" className="info-banner mb-5">
          <PackageOpen size={17} />
          <div>
            <strong className="font-medium text-foreground">
              Release machinery is not available yet.
            </strong>
            <p>
              The dashboard is ready; this host cannot start or inspect
              stabilization campaigns until the release service is installed.
            </p>
          </div>
        </div>
      )}
      {history.isError && !unavailable && (
        <div className="mb-5">
          <ErrorState error={history.error} retry={() => history.refetch()} />
        </div>
      )}
      {history.isPending && <Loading label="Loading campaigns" />}
      {campaign && (
        <div className="mb-6">
          <LiveCampaign campaign={campaign} canRelease={canRelease} />
        </div>
      )}
      <div className="detail-grid">
        <Card>
          <CardHeader>
            <div>
              <CardTitle>Create stable release branch</CardTitle>
              <p className="section-caption">
                Only one campaign can be active at a time.
              </p>
            </div>
            <GitBranch size={16} className="text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <label
              htmlFor="commitish"
              className="text-[11px] font-medium block mb-2"
            >
              Commit, branch or tag
            </label>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                resolve.mutate();
              }}
              className="flex gap-2"
            >
              <Input
                id="commitish"
                value={commitish}
                onChange={(e) => {
                  setCommitish(e.target.value);
                  setResolution(null);
                  resolve.reset();
                  create.reset();
                }}
                placeholder="master or a full commit SHA"
                disabled={unavailable || create.isPending}
              />
              <Button
                type="submit"
                variant="outline"
                disabled={unavailable || resolve.isPending || create.isPending}
              >
                {resolve.isPending ? (
                  <LoaderCircle className="animate-spin" />
                ) : (
                  <ArrowRight />
                )}
                Resolve
              </Button>
            </form>
            <p className="text-[10px] text-muted-foreground mt-2 leading-relaxed">
              Default commit: latest master commit with a completed push CI run
              that was not cancelled.
            </p>
            {anchor.isPending && (
              <p className="text-xs text-muted-foreground mt-4">
                Finding the default anchor…
              </p>
            )}
            {anchor.isError && !unavailable && (
              <div role="alert" className="info-banner mt-4">
                {anchor.error.message}
              </div>
            )}
            {resolve.isError && (
              <div role="alert" className="info-banner mt-4">
                <X size={13} />
                {resolve.error.message}
              </div>
            )}
            {resolution && (
              <div className="rounded-lg border bg-muted/35 p-4 mt-5">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <Sha value={resolution.sha} />
                  <StatusBadge
                    value={resolution.on_master ? "landed" : "blocked"}
                    label={resolution.on_master ? "On master" : "Not on master"}
                  />
                </div>
                <p className="text-xs font-medium mt-3">{resolution.subject}</p>
                <p className="text-[10px] text-muted-foreground mt-1.5">
                  Committed <RelativeTime value={resolution.committed_at} />
                </p>
                <div className="border-t pt-3 mt-3 flex flex-wrap items-center gap-2">
                  <span className="text-[10px] text-muted-foreground">
                    Push CI
                  </span>
                  <StatusBadge
                    value={resolution.push_ci?.conclusion ?? "unknown"}
                    label={
                      resolution.push_ci
                        ? `${normalizeStatus(resolution.push_ci.conclusion) === "landed" ? "Passed" : (resolution.push_ci.conclusion ?? "Unknown")}`
                        : "No completed run"
                    }
                  />
                  {resolution.push_ci && (
                    <ExternalLink
                      href={resolution.push_ci.url}
                      className="text-[10px] text-muted-foreground"
                    >
                      View run
                    </ExternalLink>
                  )}
                </div>
              </div>
            )}
            <div className="border-t pt-5 mt-5">
              <Button
                disabled={
                  Boolean(disabledReason) ||
                  create.isPending ||
                  resolve.isPending ||
                  history.isPending ||
                  anchor.isPending
                }
                onClick={() => create.mutate()}
              >
                <GitBranch />
                {create.isPending
                  ? "Starting campaign…"
                  : "Create stable release branch"}
              </Button>
              {disabledReason && (
                <p className="text-[10px] text-muted-foreground mt-3 leading-relaxed">
                  {disabledReason}
                </p>
              )}
              {create.isPending && (
                <p role="status" className="text-xs text-muted-foreground mt-3">
                  Waiting for the campaign lock. This can take up to about two
                  minutes; keep this page open.
                </p>
              )}
              {create.isError && (
                <div role="alert" className="info-banner mt-4">
                  {create.error.message} The same request identity will be used
                  if you retry.
                </div>
              )}
              {create.isSuccess && (
                <div role="status" className="info-banner mt-4">
                  <Check size={14} />
                  Campaign {create.data.id}{" "}
                  {create.data.status === "starting"
                    ? "is starting. Its branch write is being reconciled."
                    : "has started."}
                </div>
              )}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>How stabilization works</CardTitle>
            <ShieldCheck size={16} className="text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="space-y-5">
              {[
                {
                  title: "Create a release branch",
                  text: "The release branch starts from one commit on master. Only one campaign can be active at a time.",
                },
                {
                  title: "Run CI",
                  text: "Push, hourly and nightly run in order. Triage and repair agents fix failures until every stage passes at one tip.",
                },
                {
                  title: "Merge stage results",
                  text: "Each passing stage opens a pull request to merge its tested commit into master.",
                },
              ].map((step, i) => (
                <div key={step.title} className="flex gap-3">
                  <div className="size-6 shrink-0 rounded-full border text-[10px] flex justify-center items-center text-muted-foreground">
                    {i + 1}
                  </div>
                  <div>
                    <h3 className="text-xs font-medium">{step.title}</h3>
                    <p className="text-[11px] text-muted-foreground leading-relaxed mt-1">
                      {step.text}
                    </p>
                  </div>
                </div>
              ))}
            </div>
            <div className="info-banner mt-5">
              <Info size={13} />
              <span>
                Campaigns run until all three CI stages pass at the same commit.
              </span>
            </div>
          </CardContent>
        </Card>
      </div>
      <Card className="mt-6">
        <CardHeader>
          <div>
            <CardTitle>Campaign history</CardTitle>
            <p className="section-caption">
              Finished and aborted stabilization campaigns.
            </p>
          </div>
          <PackageCheck size={15} className="text-muted-foreground" />
        </CardHeader>
        {unavailable ? (
          <EmptyState title="Campaign history is unavailable">
            Release machinery is not installed on this host.
          </EmptyState>
        ) : (
          <DataTable
            data={
              history.data?.filter((c) => !liveStatuses.includes(c.status)) ??
              []
            }
            columns={columns}
          />
        )}
      </Card>
      {isMock && (
        <div className="mt-4 text-[10px] text-muted-foreground">
          <a
            href="/releases?unavailable=1"
            className="underline underline-offset-3"
          >
            Preview the unavailable-service state
          </a>
        </div>
      )}
    </>
  );
}
