import { memo } from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import { Icon } from "./Icon";
import { Emote } from "./deskmate/emotes/Emote";

// §34 (UX-016): the agent ends a deliverable turn with plain markdown —
// [Title](artifact:relative/path) — and the renderer turns it into a chip that opens the
// artifact viewer in place. Plumbing is a window event (the viewer lives in RightRail;
// this component renders deep inside the transcript): RightRail resolves the path against
// the session's artifact list, App un-hides the rail.
export const OPEN_ARTIFACT_EVENT = "ocw-open-artifact";

// board: 深链：agent 一次性提及看板 [Board · 5 items](board:)，渲染为打开右侧栏 Board 区段的
// 药丸。与 artifact chip 同源事件总线：App 取消隐藏右侧栏并展开对应区段。
export const OPEN_BOARD_EVENT = "ocw-open-board";

function BoardChip({ label }: { label: string }) {
  return (
    <button
      className="boardlink-chip"
      data-testid="board-chip"
      title="Open the board"
      onClick={() => window.dispatchEvent(new CustomEvent(OPEN_BOARD_EVENT))}
    >
      <Icon name="table" size={12} />
      <span>{label || "Board"}</span>
    </button>
  );
}

// 表情协议：标准 markdown 图片语法 ![alt](emote:NAME)（alt 通常是中文表情名，如
// 「招手问好」）。这是 LLM 原生语法，且 alt 自带语义——即使模型不识别 emote: 协议，
// 也能从 alt 读懂情绪。components.img 拦截 emote: 协议渲染成小桔 <Emote> 小图；
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

// ---------------------------------------------------------------- 中文加粗修复
// CommonMark 的强调规则（flanking delimiter run）：闭合 `**` 前面若是标点、后面又紧跟
// 非空白非标点的字符（汉字正是这一类），这个 `**` 就不被认作闭合标记，于是整对 `**`
// 原样输出——界面上看到的就是「**三、……当钩子用。**敌人……」。而「**小标题。**正文」
// 恰恰是中文写作里最常见的句式，所以这不是偶发，是必现。
// 修法：只把「按规则判定必然失效」的那一对标记去掉——丢的是加粗，一个字都不丢；
// 合法加粗（`**敌人**十`、`**一**，`）一律不动。未闭合的孤星、以及 fenced 代码块里的
// 星号另有处理：孤星删掉，代码块原样保留。
const PUNCT_OR_SYMBOL = /[\p{P}\p{S}]/u;
const FENCED_CODE = /(```[\s\S]*?```)/;

function isFlankingOk(text: string, pos: number, opening: boolean): boolean {
  // opening: 看后一个字符；closing: 看前一个字符。
  const inner = opening ? text[pos + 2] ?? "" : text[pos - 1] ?? "";
  const outer = opening ? text[pos - 1] ?? "" : text[pos + 2] ?? "";
  if (!inner || /\s/.test(inner)) return false;
  if (!PUNCT_OR_SYMBOL.test(inner)) return true;
  // 内侧是标点时，外侧必须是空白或标点才成立（CommonMark 的 flanking 条件）
  return !outer || /\s/.test(outer) || PUNCT_OR_SYMBOL.test(outer);
}

function repairSegment(text: string): string {
  if (text.indexOf("**") === -1) return text;
  const marks: number[] = [];
  for (let i = 0; i + 1 < text.length; i += 1) {
    if (text[i] === "*" && text[i + 1] === "*" && text[i + 2] !== "*" && text[i - 1] !== "*") {
      marks.push(i);
      i += 1; // 跳过第二个星号，避免把 `**` 的尾星当成新的开头
    }
  }
  if (marks.length === 0) return text;

  const dead = new Set<number>();
  for (let k = 0; k + 1 < marks.length; k += 2) {
    const open = marks[k];
    const close = marks[k + 1];
    if (!isFlankingOk(text, open, true) || !isFlankingOk(text, close, false)) {
      dead.add(open);
      dead.add(close);
    }
  }
  // 落单的奇数个标记：没有配对就是裸星号，同样删掉
  if (marks.length % 2 === 1) dead.add(marks[marks.length - 1]);
  if (dead.size === 0) return text;

  let out = "";
  let prev = 0;
  for (const pos of [...dead].sort((a, b) => a - b)) {
    out += text.slice(prev, pos);
    prev = pos + 2;
  }
  return out + text.slice(prev);
}

/** 修掉中文标点导致的加粗失效（详见上方注释）。代码块原样返回。 */
export function repairCjkEmphasis(src: string): string {
  const text = String(src ?? "");
  if (text.indexOf("**") === -1) return text;
  return text
    .split(FENCED_CODE)
    .map((part, i) => (i % 2 === 1 ? part : repairSegment(part)))
    .join("");
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

/** P1-6：引用回显——检测 AI 回复中是否引用了用户记忆的句式。 */
const MEMORY_REF_PATTERNS = [
  /因为你说过/,
  /记得你说过/,
  /你之前说过/,
  /你告诉过我/,
  /你提到过/,
  /根据你的/,
  /按照你的/,
  /正如你/,
  /你之前提到/,
  /你曾说/,
  /你以前说/,
  /你上次说/,
  /还记得你/,
  /就像你/,
];

function detectMemoryReference(text: string): boolean {
  return MEMORY_REF_PATTERNS.some((p) => p.test(text));
}

// PR20(#49)：remarkPlugins / urlTransform / components 每次 render 新建会让
// ReactMarkdown 内部缓存全部失效（每帧重解析**全部历史消息**）。三者都不依赖
// props，提升为模块常量；组件本体再 memo，text 不变就完全不重渲染。
const REMARK_PLUGINS = [remarkGfm];

const URL_TRANSFORM = (url: string) =>
  url.startsWith("artifact:") || url.startsWith(EMOTE_PROTOCOL) || url.startsWith("board:")
    ? url
    : defaultUrlTransform(url);

const MD_COMPONENTS = {
  a: ({ node: _n, href, children, ...props }: any) => {
    if (href?.startsWith("artifact:")) {
      const title = Array.isArray(children) ? children.join("") : String(children ?? "");
      return <ArtifactChip path={href.slice("artifact:".length)} title={title} />;
    }
    if (href?.startsWith("board:")) {
      const label = Array.isArray(children) ? children.join("") : String(children ?? "");
      return <BoardChip label={label} />;
    }
    return (
      <a href={href} {...props} target="_blank" rel="noreferrer">
        {children}
      </a>
    );
  },
  img: ({ node: _n, src, alt, ...props }: any) => {
    if (typeof src === "string" && src.startsWith(EMOTE_PROTOCOL)) {
      const raw = src.slice(EMOTE_PROTOCOL.length);
      const ci = raw.indexOf(":");
      const name = ci === -1 ? raw : raw.slice(ci + 1);
      return (
        <Emote name={name} size={48} title={typeof alt === "string" ? alt : undefined} />
      );
    }
    return <img src={src} alt={alt} {...props} />;
  },
};

export const Markdown = memo(function Markdown({ text }: { text: string }) {
  const rendered = repairCjkEmphasis(withEmoteSyntax(text));
  const hasMemoryRef = detectMemoryReference(text || "");
  return (
    <div className="md">
      {hasMemoryRef && (
        <div
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 5,
            fontSize: 11,
            color: "#b45309",
            background: "rgba(180,83,9,0.08)",
            padding: "3px 9px",
            borderRadius: 8,
            marginBottom: 8,
            fontWeight: 500,
          }}
          title="这条回复参考了你的记忆"
        >
          📌 基于你的记忆
        </div>
      )}
      <ReactMarkdown
        remarkPlugins={REMARK_PLUGINS}
        // artifact: and emote: are ours — keep them through the sanitizer (everything else
        // gets the default http/https/mailto policy). emote: is intercepted in components.img.
        urlTransform={URL_TRANSFORM}
        components={MD_COMPONENTS}
      >
        {rendered}
      </ReactMarkdown>
    </div>
  );
});
