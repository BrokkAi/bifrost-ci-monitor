import {
  Clock3,
  GitMerge,
  GitPullRequest,
  FlaskConical,
  Send,
  Bot,
} from "lucide-react";
import {
  BarChart,
  Bar,
  CartesianGrid,
  XAxis,
  YAxis,
  LineChart,
  Line,
  ReferenceDot,
} from "recharts";
import { useApi } from "@/api/client";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
} from "@/components/ui/chart";
import { PageHeading, Loading, ErrorState } from "@/components/common";
import { durationFromMinutes, plural } from "@/lib/utils";

export function StatsPage() {
  const overview = useApi("/overview");
  const stats = useApi("/stats", "days=30");
  if (overview.isPending || stats.isPending)
    return <Loading label="Loading statistics" />;
  if (overview.isError)
    return (
      <ErrorState error={overview.error} retry={() => overview.refetch()} />
    );
  if (stats.isError)
    return <ErrorState error={stats.error} retry={() => stats.refetch()} />;
  const d = overview.data;
  const kpis = [
    {
      label: "Landed today",
      value: d.kpis.landed_today,
      footnote: "Pull requests on master",
      icon: GitMerge,
    },
    {
      label: "Landed · 7 days",
      value: d.kpis.landed_7_days,
      footnote: "Across integration batches",
      icon: GitPullRequest,
    },
    {
      label: "Median duration",
      value: durationFromMinutes(d.kpis.median_batch_minutes_7_days),
      footnote: "Completed batches · 7 days",
      icon: Clock3,
    },
    {
      label: "Open CI failures",
      value: d.kpis.open_ci_failures,
      footnote: "In the shared failure ledger",
      icon: FlaskConical,
    },
    {
      label: "Pending writes",
      value: d.kpis.pending_outbox_writes,
      footnote: `${plural(d.outbox.failing, "delivery needs", "deliveries need")} attention`,
      icon: Send,
    },
    {
      label: "Active repairs",
      value: d.kpis.active_repairs,
      footnote: `Of ${d.repairs.concurrency_limit} available slots`,
      icon: Bot,
    },
  ];
  const days = stats.data.days;
  return (
    <>
      <PageHeading
        title="Stats"
        description="Pull requests landed and batch duration over the last 30 days."
      />
      <div className="kpi-grid">
        {kpis.map((k) => (
          <Card key={k.label} className="kpi">
            <div className="kpi-label">
              {k.label}
              <k.icon size={12} strokeWidth={1.5} />
            </div>
            <div className="kpi-value">{k.value}</div>
            <div className="kpi-footnote">{k.footnote}</div>
          </Card>
        ))}
      </div>
      <div
        className={`chart-grid ${stats.isFetching && !stats.isPending ? "opacity-65" : ""}`}
      >
        <Card>
          <CardHeader>
            <div>
              <CardTitle>Pull requests landed</CardTitle>
              <p className="section-caption">
                Pull requests landed per day · last 30 days
              </p>
            </div>
            <GitMerge size={15} className="text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <ChartContainer
              config={{
                landed_prs: {
                  label: "Pull requests",
                  color: "var(--series-1)",
                },
              }}
            >
              <BarChart
                data={days}
                accessibilityLayer
                margin={{ top: 10, left: -26, right: 0, bottom: 0 }}
                barSize={12}
              >
                <CartesianGrid vertical={false} />
                <XAxis
                  dataKey="date"
                  tickLine={false}
                  axisLine={false}
                  tickMargin={10}
                  minTickGap={37}
                  tickFormatter={(s) =>
                    new Date(`${s}T00:00:00`).toLocaleDateString("en", {
                      month: "short",
                      day: "numeric",
                    })
                  }
                />
                <YAxis
                  tickLine={false}
                  axisLine={false}
                  allowDecimals={false}
                />
                <ChartTooltip
                  shared={false}
                  cursor={false}
                  content={<ChartTooltipContent />}
                />
                <Bar
                  isAnimationActive={false}
                  dataKey="landed_prs"
                  fill="var(--color-landed_prs)"
                  radius={[3, 3, 0, 0]}
                />
              </BarChart>
            </ChartContainer>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <div>
              <CardTitle>Batch duration</CardTitle>
              <p className="section-caption">Daily median duration · minutes</p>
            </div>
            <Clock3 size={15} className="text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <ChartContainer
              config={{
                median_batch_minutes: {
                  label: "Median",
                  color: "var(--series-1)",
                },
              }}
            >
              <LineChart
                data={days}
                accessibilityLayer
                margin={{ top: 10, left: -26, right: 25, bottom: 0 }}
              >
                <CartesianGrid vertical={false} />
                <XAxis
                  dataKey="date"
                  tickLine={false}
                  axisLine={false}
                  tickMargin={10}
                  minTickGap={42}
                  tickFormatter={(s) =>
                    new Date(`${s}T00:00:00`).toLocaleDateString("en", {
                      month: "short",
                      day: "numeric",
                    })
                  }
                />
                <YAxis
                  tickLine={false}
                  axisLine={false}
                  domain={[0, "auto"]}
                  tickFormatter={(n) => String(n)}
                />
                <ChartTooltip
                  cursor={{ strokeWidth: 1 }}
                  content={
                    <ChartTooltipContent
                      valueFormatter={(v) => durationFromMinutes(Number(v))}
                    />
                  }
                />
                <Line
                  isAnimationActive={false}
                  dataKey="median_batch_minutes"
                  type="linear"
                  stroke="var(--color-median_batch_minutes)"
                  strokeWidth={2}
                  dot={false}
                  activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--card)" }}
                  connectNulls={false}
                />
                {days.length > 0 &&
                  days[days.length - 1].median_batch_minutes != null && (
                    <ReferenceDot
                      x={days[days.length - 1].date}
                      y={days[days.length - 1].median_batch_minutes!}
                      r={4}
                      fill="var(--series-1)"
                      stroke="var(--card)"
                      strokeWidth={2}
                      label={{
                        value: durationFromMinutes(
                          days[days.length - 1].median_batch_minutes,
                        ),
                        position: "top",
                        fill: "var(--foreground)",
                        fontSize: 10,
                      }}
                    />
                  )}
              </LineChart>
            </ChartContainer>
          </CardContent>
        </Card>
      </div>
      <details className="mt-3 text-[10px] text-muted-foreground">
        <summary className="cursor-pointer">
          View daily statistics as a table
        </summary>
        <Card className="mt-3 max-h-60 overflow-auto">
          <table className="data-table">
            <thead>
              <tr>
                <th>Date</th>
                <th>Landed PRs</th>
                <th>Median duration</th>
              </tr>
            </thead>
            <tbody>
              {days.map((day) => (
                <tr key={day.date}>
                  <td>{day.date}</td>
                  <td>{day.landed_prs}</td>
                  <td>{durationFromMinutes(day.median_batch_minutes)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      </details>
    </>
  );
}
