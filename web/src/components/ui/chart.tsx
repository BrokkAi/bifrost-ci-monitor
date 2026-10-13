import {
  createContext,
  useContext,
  useId,
  type ComponentProps,
  type ReactNode,
} from "react";
import { ResponsiveContainer, Tooltip } from "recharts";
import { cn } from "@/lib/utils";
export type ChartConfig = Record<string, { label: string; color: string }>;
const ChartContext = createContext<ChartConfig>({});
export function ChartContainer({
  config,
  className,
  children,
}: {
  config: ChartConfig;
  className?: string;
  children: ComponentProps<typeof ResponsiveContainer>["children"];
}) {
  const id = useId().replaceAll(":", "");
  return (
    <ChartContext.Provider value={config}>
      <div
        data-chart={id}
        className={cn(
          "chart h-52 w-full [&_.recharts-cartesian-axis-tick_text]:fill-muted-foreground [&_.recharts-cartesian-grid_line]:stroke-border [&_.recharts-tooltip-cursor]:stroke-border [&_.recharts-layer]:outline-none",
          className,
        )}
        style={Object.fromEntries(
          Object.entries(config).map(([key, value]) => [
            `--color-${key}`,
            value.color,
          ]),
        )}
      >
        <ResponsiveContainer width="100%" height="100%">
          {children}
        </ResponsiveContainer>
      </div>
    </ChartContext.Provider>
  );
}
export const ChartTooltip = Tooltip;
export function ChartTooltipContent({
  active,
  payload,
  label,
  valueFormatter,
}: {
  active?: boolean;
  payload?: {
    dataKey?: string | number;
    value?: string | number;
    color?: string;
  }[];
  label?: ReactNode;
  valueFormatter?: (value: string | number) => ReactNode;
}) {
  const config = useContext(ChartContext);
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-lg border bg-popover px-3 py-2 shadow-lg">
      <div className="mb-2 text-xs text-muted-foreground">{label}</div>
      {payload.map((p, i) => (
        <div key={i} className="flex items-center gap-3 text-xs">
          <span className="h-0.5 w-3" style={{ backgroundColor: p.color }} />
          <strong className="text-foreground">
            {valueFormatter && p.value != null ? valueFormatter(p.value) : p.value}
          </strong>
          <span className="text-muted-foreground">
            {config[String(p.dataKey)]?.label ?? p.dataKey}
          </span>
        </div>
      ))}
    </div>
  );
}
