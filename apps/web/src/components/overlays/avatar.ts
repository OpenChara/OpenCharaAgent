/* Avatar — upload limits for the visual editor's avatar slot. (The old SVG-gen +
 * dual-theme AvatarControls/AvatarEditor were retired; their helpers —
 * safeSvgForPreview, avatarMime, avatarFileError, utf8ToB64 — went with them. The
 * R9 VisualEditor does its own inline ext/size validation against these two.) */

export const AVATAR_UPLOAD_MAX = 1024 * 1024;
export const AVATAR_EXTS = ["png", "jpg", "jpeg", "svg"] as const;
