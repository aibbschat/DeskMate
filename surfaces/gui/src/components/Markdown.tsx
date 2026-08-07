import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import { Icon } from "./Icon";
import { Emote } from "./deskmate/emotes/Emote";
import { EMOTE_MANIFEST } from "./deskmate/emotes/emoteManifest";

// §34 (UX-016): the agent ends a deliverable turn with plain markdown —
// [Title](artifact:relative/path) — and the renderer turns it into a chip that opens the
// artifact viewer in place. Plumbing is a window event (the viewer lives in RightRail;
// this component renders deep inside the transcript): RightRail resolves the path against
// the session's artifact list, App un-hides the rail.
export const OPEN_ARTIFACT_EVENT = "ocw-open-artifact";

// 表情协议：标准 markdown 图片语法 ![alt](emote:NAME)（alt 通常是中文表情名，如
// 「招手问好」）。这是 LLM 原生语法，且 alt 自带语义——即使模型不识别 emote: 协议，
// 也能从 alt 读懂情绪。components.img 拦截 emote: 协议渲染成墨灵 <Emote> 小图；
// withEmoteSyntax 仍保留，用于把历史遗留的 [[emote:NAME]] 写法兼容渲染。
export const EMOTE_PROTOCOL = "emote:";
const EMOTE_RE = /\[\[emote:([a-zA-Z0-9_-]+)\]\]/g;

function withEmoteSyntax(text: string): string {
  // 协议名统一转小写，避免 LLM 大小写不一导致表情丢失（Emote 查表用全小写键）。
  return text.replace(EMOTE_RE, (_m, name) => `![${name}](${EMOTE_PROTOCOL}${String(name).toLowerCase()})`);
}

// 复制到剪贴板时，两种写法都要剥离：历史 [[emote:NAME]] / [[emote:PACK:NAME]] 与
// 标准 ![alt](emote:NAME) / ![alt](emote:PACK:NAME)。
const EMOTE_STRIP_RE =
  /(\[\[emote:[a-zA-Z0-9_-]+(?::[a-zA-Z0-9_-]+)?\]\])|(!\[[^\]]*\]\(emote:[a-zA-Z0-9_-]+(?::[a-zA-Z0-9_-]+)?\))/g;

/** 从文本中剥离表情协议标记（用于复制到剪贴板等非渲染场景，避免把协议暴露给用户）。 */
export function stripEmote(text: string): string {
  return text.replace(EMOTE_STRIP_RE, "").replace(/\s{2,}/g, " ").trim();
}

function ArtifactChip({ path, title }: { path: string; title: string }) {
  const file = path.split("/").pop() || path;
  return (
    <button
      className="art-chip"
      data-testid="artifact-chip"
      title={path}
      onClick={() =>
        window.dispatchEvent(new CustomEvent(OPEN_ARTIFACT_EVENT, { detail: { path } }))
      }
    >
      <span className="art-chip-ico">
        <Icon name="file" size={14} />
      </span>
      <span className="art-chip-meta">
        <b>{title || file}</b>
        {title && title !== file && <span>{file}</span>}
      </span>
      <span className="art-chip-open">Open ›</span>
    </button>
  );
}

// Assistant messages rendered as GitHub-flavored markdown (headings, lists, tables, code,
// links). Links open externally — never navigate the app shell — except artifact: links,
// which open the session's artifact viewer.
export function Markdown({ text }: { text: string }) {
  const rendered = withEmoteSyntax(text);
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        // artifact: and emote: are ours — keep them through the sanitizer (everything else
        // gets the default http/https/mailto policy). emote: is intercepted in components.img.
        urlTransform={(url) =>
          url.startsWith("artifact:") || url.startsWith(EMOTE_PROTOCOL)
            ? url
            : defaultUrlTransform(url)
        }
        components={{
          a: ({ node: _n, href, children, ...props }) => {
            if (href?.startsWith("artifact:")) {
              const title = Array.isArray(children) ? children.join("") : String(children ?? "");
              return <ArtifactChip path={href.slice("artifact:".length)} title={title} />;
            }
            return (
              <a href={href} {...props} target="_blank" rel="noreferrer">
                {children}
              </a>
            );
          },
          img: ({ node: _n, src, alt, ...props }) => {
            if (typeof src === "string" && src.startsWith(EMOTE_PROTOCOL)) {
              // emote:PACK:NAME（带包）或 emote:NAME（无包 → 回落默认包，冻结不随切换变画风）。
              const raw = src.slice(EMOTE_PROTOCOL.length);
              const ci = raw.indexOf(":");
              const pack = ci === -1 ? undefined : raw.slice(0, ci);
              const name = ci === -1 ? raw : raw.slice(ci + 1);
              return (
                <Emote
                  name={name}
                  pack={pack ?? EMOTE_MANIFEST.defaultPack}
                  size={22}
                  title={typeof alt === "string" ? alt : undefined}
                />
              );
            }
            return <img src={src} alt={alt} {...props} />;
          },
        }}
      >
        {rendered}
      </ReactMarkdown>
    </div>
  );
}
