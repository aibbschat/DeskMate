/**
 * Emote — 单表情渲染组件。
 *
 * 占位符自 v1.3.10 起可带包名：![中文名](emote:PACK:NAME)（用户发出时由前端烤进 token）。
 * 每条表情自带所属 pack → 渲染端据此冻结画风，切换全局激活 pack 不会改写已发出消息，
 * 同一段对话里可以黄圆脸（moling-default）与方贴纸线描（moling-mono）共存。
 *
 * 旧式 [[emote:NAME]] 与无包名 ![中文名](emote:NAME) 仍兼容：回落默认包并冻结，
 * 同样不随全局切换变画风（历史消息稳定）。只有表情选择面板预览用 live 模式跟随激活 pack。
 */

import type { CSSProperties, ReactNode } from "react";
import { getPackRegistry, PACK_THEMES, PACK_BGS, FaceBg } from "./emotes";
import { emoteLabel, EMOTE_MANIFEST } from "./emoteManifest";
import { useActivePack } from "./emotePack";

interface Props {
  /** 表情名，对应 emotes.manifest.json 的 id */
  name: string;
  /** 像素尺寸（正方形）。不传则使用 CSS 1.4em，inline 时不撑行距。 */
  size?: number;
  /** 覆盖默认 title */
  title?: string;
  /** 强制指定 pack（来自表情 token 的 emote:PACK:NAME，冻结、不随全局切换变画风） */
  pack?: string;
  /** 跟随全局激活 pack 实时变画风（仅表情选择面板预览用；气泡渲染不要传） */
  live?: boolean;
}

export function Emote({ name, size, title, pack, live }: Props) {
  const active = useActivePack();
  // 有显式 pack（消息 token 自带，或回落默认包）→ 冻结，不随切换变画风；
  // 仅 live（表情面板预览）跟随当前激活 pack。
  const packId = pack ?? (live ? active : EMOTE_MANIFEST.defaultPack);
  const registry = getPackRegistry(packId);
  const Face = registry[name.toLowerCase()];
  const label = emoteLabel(name);
  const dimension = size ?? "1.4em";
  const style: CSSProperties = {
    display: "inline-block",
    width: dimension,
    height: dimension,
    verticalAlign: "middle",
    lineHeight: 0,
  };
  // 兜底：表情缺失（如旧构建未含该 id、或误传了不存在的表情名）时，显式渲染一个
  // 带中文名的小徽章，而不是静默 return null——后者会让气泡里凭空少一块，看起来像
  // "表情挂了"。新构建里正常表情不会走到这里。
  if (!Face) {
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
  const theme = PACK_THEMES[packId] ?? PACK_THEMES[EMOTE_MANIFEST.defaultPack];
  const Bg = PACK_BGS[packId] ?? FaceBg;

  return (
    <span className="deskmate-emote" title={title ?? label} role="img" aria-label={label} style={style}>
      <svg viewBox="0 0 64 64" width="100%" height="100%" aria-hidden="true">
        <Bg theme={theme}>
          <Face />
        </Bg>
      </svg>
    </span>
  );
}

// 表情 token：支持 emote:PACK:NAME（带包）与 emote:NAME（无包，回落默认包）。
const INLINE_EMOTE_RE =
  /\[\[emote:([a-zA-Z0-9_-]+)(?::([a-zA-Z0-9_-]+))?\]\]|!\[[^\]]*\]\(emote:([a-zA-Z0-9_-]+)(?::([a-zA-Z0-9_-]+))?\)/g;

/** 从一次正则命中里拆出 { name, pack? }（兼容无包写法）。 */
function parseEmoteToken(m: RegExpExecArray): { name: string; pack?: string } {
  // 括号写法：group1=pack?/name，group2=name?
  // 图片写法：group3=pack?/name，group4=name?
  if (m[2]) return { pack: m[1], name: m[2] }; // [[emote:PACK:NAME]]
  if (m[4]) return { pack: m[3], name: m[4] }; // ![..](emote:PACK:NAME)
  return { name: (m[1] ?? m[3]) ?? "" }; // 无包
}

/**
 * 把文本里的表情标记渲染成 <Emote> 内联节点，其余保持纯文本。
 * 供用户气泡等不走 Markdown 的路径复用（与 Markdown 的 emote 渲染语义一致）。
 * 每个 token 自带 pack（或回落默认包）→ 渲染冻结，不随全局激活 pack 切换变画风。
 */
export function renderEmoteInline(text: string, size?: number): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  INLINE_EMOTE_RE.lastIndex = 0;
  let i = 0;
  while ((m = INLINE_EMOTE_RE.exec(text)) !== null) {
    const { name, pack } = parseEmoteToken(m);
    if (m.index > last) out.push(<span key={`t${i++}`}>{text.slice(last, m.index)}</span>);
    out.push(<Emote key={`e${i++}`} name={name} pack={pack ?? EMOTE_MANIFEST.defaultPack} size={size} />);
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(<span key={`t${i++}`}>{text.slice(last)}</span>);
  return out;
}
