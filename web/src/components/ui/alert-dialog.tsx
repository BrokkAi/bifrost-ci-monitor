import { AlertDialog } from "radix-ui";
import type { ComponentProps } from "react";
export const AlertDialogRoot = AlertDialog.Root;
export const AlertDialogTrigger = AlertDialog.Trigger;
export const AlertDialogTitle = AlertDialog.Title;
export const AlertDialogDescription = AlertDialog.Description;
export const AlertDialogCancel = AlertDialog.Cancel;
export const AlertDialogAction = AlertDialog.Action;
export function AlertDialogContent(
  props: ComponentProps<typeof AlertDialog.Content>,
) {
  return (
    <AlertDialog.Portal>
      <AlertDialog.Overlay className="fixed inset-0 z-50 bg-black/45 backdrop-blur-sm" />
      <AlertDialog.Content
        className="fixed left-1/2 top-1/2 z-50 w-[calc(100%-2rem)] max-w-md -translate-x-1/2 -translate-y-1/2 space-y-4 rounded-xl border bg-card p-6 shadow-xl"
        {...props}
      />
    </AlertDialog.Portal>
  );
}
