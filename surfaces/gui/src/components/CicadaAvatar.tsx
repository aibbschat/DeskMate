import type { CSSProperties } from "react";

interface CicadaAvatarProps {
  size?: number;
  className?: string;
  style?: CSSProperties;
}

/**
 * 小桔（蝉）头像 —— 水墨风极简剪影，用于聊天气泡的发言者标识。
 * 使用 currentColor，颜色由父级 .who-avatar 的 color 决定（默认取品牌色）。
 */
export default function CicadaAvatar({ size = 18, className, style }: CicadaAvatarProps) {
  return (
    <svg
      className={className}
      style={style}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
      focusable="false"
    >
      {/* 双翅 */}
      <path
        d="M11 8 C4.5 6 2 11 4 15.5 C6.8 13.6 9.6 11.8 11 10.6 Z"
        fill="currentColor"
        opacity="0.2"
      />
      <path
        d="M13 8 C19.5 6 22 11 20 15.5 C17.2 13.6 14.4 11.8 13 10.6 Z"
        fill="currentColor"
        opacity="0.2"
      />
      {/* 身体 */}
      <ellipse cx="12" cy="13.4" rx="2.3" ry="4.8" fill="currentColor" />
      {/* 头部 */}
      <circle cx="12" cy="6.6" r="1.9" fill="currentColor" />
      {/* 眼睛（用面板底色做镂空，明暗主题都适配） */}
      <circle cx="11.1" cy="6.3" r="0.42" fill="var(--panel)" />
      <circle cx="12.9" cy="6.3" r="0.42" fill="var(--panel)" />
      {/* 足 */}
      <path
        d="M9.9 12 L7.6 13.4 M9.9 14 L7.6 15.8 M14.1 12 L16.4 13.4 M14.1 14 L16.4 15.8"
        stroke="currentColor"
        strokeWidth="0.6"
        strokeLinecap="round"
      />
    </svg>
  );
}
