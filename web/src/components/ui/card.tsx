import type { ComponentProps } from "react";
import { cn } from "@/lib/utils";
export function Card({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      className={cn(
        "rounded-xl border border-border bg-card text-card-foreground shadow-[0_1px_3px_0_rgb(0_0_0/0.025)]",
        className,
      )}
      {...props}
    />
  );
}
export function CardHeader({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      className={cn(
        "flex items-start justify-between gap-4 px-5 pt-5 pb-4",
        className,
      )}
      {...props}
    />
  );
}
export function CardTitle({ className, ...props }: ComponentProps<"h2">) {
  return (
    <h2
      className={cn("font-semibold text-sm tracking-tight", className)}
      {...props}
    />
  );
}
export function CardContent({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("px-5 pb-5", className)} {...props} />;
}
