/**
 * Emote — 单表情渲染组件（v2：3D 黏土贴纸 webp）。
 *
 * 直接按 id 加载 emotes 目录下的透明底 webp，内联在聊天气泡里。
 * v2 废弃多 Pack 机制：只有一套「xiaoju-clay」，所有表情统一画风。
 */

import type { CSSProperties } from "react";
import { EMOTE_IMAGES } from "./emotes";
import { emoteLabel } from "./emoteManifest";

interface Props {
  /** 表情名，对应 emotes.manifest.json 的 id */
  name: string;
  /** 像素尺寸（正方形）。不传则使用 CSS 内联尺寸。 */
  size?: number;
  /** 覆盖默认 title */
  title?: string;
}

/** 聊天气泡内联表情尺寸（em 单位，相对于正文字号）。 */
const INLINE_SIZE = "2.2em";

export function Emote({ name, size, title }: Props) {
  const src = EMOTE_IMAGES[name.toLowerCase()];
  const label = emoteLabel(name);
  const dimension = size ?? INLINE_SIZE;
  const style: CSSProperties = {
    display: "inline-block",
    width: dimension,
    height: dimension,
    verticalAlign: "middle",
    lineHeight: 0,
  };

  if (!src) {
    return (
      <span
        className="deskmate-emote deskmate-emote-missing"
        title={title ?? label}
        role="img"
        aria-label={label}
        style={{ ...style, display: "inline-flex", alignItems: "center", justifyContent: "center" }}
      >
        <span
          style={{
            fontSize: "0.62em",
            lineHeight: 1,
            padding: "0.18em 0.32em",
            borderRadius: "0.4em",
            background: "var(--paper, #f3f0ea)",
            border: "1px solid var(--line, #e3ddd2)",
            color: "var(--muted, #8a8175)",
            whiteSpace: "nowrap",
          }}
        >
          {title ?? label}
        </span>
      </span>
    );
  }

  return (
    <span className="deskmate-emote" title={title ?? label} role="img" aria-label={label} style={style}>
      <img src={src} alt={label} width="100%" height="100%" style={{ objectFit: "contain", display: "block" }} draggable={false} />
    </span>
  );
}

// 表情 token：v2 只有一套 pack，带包名写法仍兼容但包名被忽略。
const INLINE_EMOTE_RE =
  /\[\[emote:([a-zA-Z0-9_-]+)(?::([a-zA-Z0-9_-]+))?\]\]|!\[[^\]]*\]\(emote:([a-zA-Z0-9_-]+)(?::([a-zA-Z0-9_-]+))?\)/g;

function parseEmoteToken(m: RegExpExecArray): { name: string; pack?: string } {
  if (m[2]) return { pack: m[1], name: m[2] };
  if (m[4]) return { pack: m[3], name: m[4] };
  return { name: (m[1] ?? m[3]) ?? "" };
}

export function renderEmoteInline(text: string, size?: number): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  INLINE_EMOTE_RE.lastIndex = 0;
  let i = 0;
  while ((m = INLINE_EMOTE_RE.exec(text)) !== null) {
    const { name } = parseEmoteToken(m);
    if (m.index > last) out.push(<span key={`t${i++}`}>{text.slice(last, m.index)}</span>);
    out.push(<Emote key={`e${i++}`} name={name} size={size} />);
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(<span key={`t${i++}`}>{text.slice(last)}</span>);
  return out;
}
