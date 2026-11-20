/** The product mark and wordmark, shared by the public site and the app. Server-render safe. */

import { PRODUCT } from "../hooks";

/** Four bands, deepest flagged: the product mark. */
export function Mark({ size = 16 }: { size?: number }) {
  return (
    <svg className="mark" width={size} height={size} viewBox="0 0 16 16" aria-hidden focusable="false">
      <rect y="0" width="16" height="4" fill="var(--stratum-1)" />
      <rect y="4" width="16" height="4" fill="var(--stratum-2)" />
      <rect y="8" width="16" height="4" fill="var(--stratum-3)" />
      <rect y="12" width="16" height="4" fill="var(--gley-deep)" />
      <circle cx="11" cy="14" r="1.6" fill="var(--flag)" />
    </svg>
  );
}

/** Mark + product name, for headers. */
export function Wordmark() {
  return (
    <>
      <Mark />
      <span>{PRODUCT}</span>
    </>
  );
}
