import { DropdownMenu } from "radix-ui";
import type { ComponentProps } from "react";
import { cn } from "@/lib/utils";
export const DropdownMenuRoot = DropdownMenu.Root;
export const DropdownMenuTrigger = DropdownMenu.Trigger;
export function DropdownMenuContent({
  className,
  ...props
}: ComponentProps<typeof DropdownMenu.Content>) {
  return (
    <DropdownMenu.Portal>
      <DropdownMenu.Content
        sideOffset={8}
        className={cn(
          "z-50 min-w-48 rounded-xl border bg-popover p-1.5 text-popover-foreground shadow-xl",
          className,
        )}
        {...props}
      />
    </DropdownMenu.Portal>
  );
}
export function DropdownMenuItem({
  className,
  ...props
}: ComponentProps<typeof DropdownMenu.Item>) {
  return (
    <DropdownMenu.Item
      className={cn(
        "flex cursor-pointer items-center gap-2 rounded-md px-3 py-2 text-sm outline-none focus:bg-muted [&_svg]:size-4",
        className,
      )}
      {...props}
    />
  );
}
export const DropdownMenuSeparator = () => (
  <DropdownMenu.Separator className="my-1 h-px bg-border" />
);
