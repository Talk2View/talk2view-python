// vendored-from: Talk2View/talk2view-typescript src/constants.ts
// vendored-commit: b042b005611c59e8bc6fbd304ea1f028d11fb88b
// local-delta: none
//
// A copy of the public TS SDK's attachment constants (the page's upload `accept` list).
// Below this header the file is the canonical one, byte for byte, and CI fails when it
// differs from canonical main (scripts/check-vendored-sdk.mjs). Never edit it here:
// re-vendor it (see README.md, "Where the TS SDK code comes from").
// end of vendoring header
/**
 * Shared attachment constraints — the single source of truth for which file
 * types the SDK will upload. Both the public `Talk2View.uploadAttachment()`
 * guard and the bundled Composer derive from these, so partners with their own
 * composer get the same client-side validation as the built-in UI.
 */

/** MIME whitelist (mirrors the server). Also drives the file input's `accept`. */
export const ATTACHMENT_ACCEPT = 'image/png,image/jpeg,image/webp,image/gif,application/pdf';

/** The same whitelist as a Set, for O(1) membership checks. */
export const ALLOWED_ATTACHMENT_MIME = new Set(ATTACHMENT_ACCEPT.split(','));

/** Human-readable form of the whitelist, shown in hints and error messages. */
export const ATTACHMENT_TYPES_LABEL = 'PNG, JPEG, WebP, GIF, or PDF';

/** True when `type` is an attachment MIME type the SDK (and server) accepts. */
export function isAllowedAttachmentType(type: string): boolean {
  return ALLOWED_ATTACHMENT_MIME.has(type);
}
