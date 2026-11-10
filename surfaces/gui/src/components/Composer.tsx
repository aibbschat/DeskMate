import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { Attachment, SessionUsage } from "../types";
import { isPdfFile, readFile } from "../attach";
import { getSettings, inspectPdf, sessionSkills, type SessionSkillRow } from "../api";
import { formatTokens, totalTokens } from "../usage";
import { Dropdown, type Option } from "./Dropdown";
import { Icon } from "./Icon";
import { ProjectBindMenu } from "./ProjectBindMenu";
import { type Expert } from "./deskmate/experts";
import { ExpertAvatar } from "./deskmate/ExpertAvatar";
import { Toggle } from "./Toggle";
import { EmotePicker } from "./deskmate/emotes/EmotePicker";
import { emoteLabel } from "./deskmate/emotes/emoteManifest";
import { getActivePack } from "./deskmate/emotes/emotePack";
import {
  cancelDictation,
  getDictationLevel,
  getDictationStatus,
  isTauri,
  startDictation,
  stopDictation,
  type DictationStatus,
} from "../tauri";

// Plan + Custom hidden for this release (owner ask 2026-07-22): Plan's approval flow isn't
// polished enough to ship, and Custom (config.toml auto-allow rules) is a power-user mode
// with no in-app explanation. The server still honors both — a session already in one of
// those modes keeps working; the picker just doesn't offer them.
const PERMISSION_OPTIONS: (Option & { translationKey: string; descKey: string })[] = [
  { value: "discuss", label: "Discuss", description: "Chat and explore — no edits or commands", translationKey: "composer.discussLabel", descKey: "composer.discussDescription" },
  { value: "interactive", label: "Ask for approval", description: "Ask before edits and commands", translationKey: "composer.askForApprovalLabel", descKey: "composer.askForApprovalDescription" },
  { value: "bypass-approvals", label: "Full access", description: "Run everything without asking", translationKey: "composer.fullAccessLabel", descKey: "composer.fullAccessDescription" },
];

// Which sessions get the Mode menu. Every agent except the pure-chat one runs the same
// TurnEngine and can hit approval-gated tools — including the persona / expert 1v1
// sessions summoned from the Experts center or the contacts rail. The old gate was
// `workspace !== undefined`, which silently excluded those sessions (they have no
// folder workspace) and left them stuck on "Ask for approval" with no way to switch.
// Kept next to PERMISSION_OPTIONS so the menu and its availability rule can't drift.
export const permissionMenuAvailable = (agent: string) => agent !== "chat";

// No hardcoded model fallback: until the server supplies the list (a few seconds after a
// cold app boot), the picker renders a disabled "Loading models…" chip. A baked-in list
// goes stale and silently offers ids the backend never confirmed (caught 2026-07-21).

// Drop the provider prefix for display (anthropic:claude-opus-4-8 → claude-opus-4-8); full id on hover.
const shortModel = (m: string) => (m.includes(":") ? m.split(":").slice(1).join(":") : m);

// Identify an attachment by name + payload size so duplicates (e.g. the same file picked twice,
// or a prefill applied twice) collapse to one chip.
const attKey = (a: Attachment) =>
  a.kind === "text"
    ? `t:${a.name}:${a.text?.length ?? 0}`
    : `${a.kind[0]}:${a.name}:${a.data_url?.length ?? 0}`;
const mergeAttachments = (cur: Attachment[], add: Attachment[]): Attachment[] => {
  const seen = new Set(cur.map(attKey));
  return [...cur, ...add.filter((a) => !seen.has(attKey(a)))].slice(0, 8);
};

interface Props {
  mode: string;
  model: string;
  models?: string[];
  modelLabels?: Record<string, string>; // curated display names (raw id when absent)
  /** {full model id → "free" | "paid"} — splits the picker into 免费 / 付费 two tiers. */
  modelTiers?: Record<string, string>;
  /** {full model id → 厂商显示名}（2026-10-13）：picker 按厂商 × 档位分组折叠。 */
  modelVendors?: Record<string, string>;
  /** 当前会话 id；官方 v0.2.0 用于门控 slash skill 弹窗与 "This session" 绑定子菜单
   *  （无会话不弹）。本 fork 已接官方门控逻辑（见 slash 状态机 + attach 菜单绑定行）。 */
  sessionId?: string;
  // The model is FIXED once the session has history (§17): the picker renders ONLY on a fresh
  // session; after the first turn the fact lives in the topbar subtitle (§22) — no
  // interactive-then-disabled control.
  running: boolean;
  connected: boolean;
  // False when the default model's provider has no key — the composer shows a "connect a model"
  // banner and routes sends to setup (preserving the draft) instead of dropping them.
  modelReady?: boolean;
  onConnectModel?: () => void;
  onConfigureVoiceInput?: () => void;
  // UX-044: "View & edit…" in the Project memory submenu routes to the memory panel.
  onOpenMemory?: () => void;
  onSend: (text: string, attachments?: Attachment[], skill?: string) => void;
  onInterrupt: () => void;
  onModeChange: (mode: string) => void;
  onModelChange: (model: string) => void;
  // Working-area context for project-scoped sessions (Code/Cowork); also gates the
  // "This session" bindings submenu. It is NOT the Mode menu's gate any more — see
  // showModeMenu below: persona / expert 1v1 sessions have no folder workspace but still
  // need to pick a permission mode. The folder/roots + branch controls left the composer
  // for the Session settings drawer (§22) — folder access is standing session config.
  workspace?: string;
  /** 显式控制权限菜单（ModeMenu）显隐；不传时回退旧的 workspace !== undefined 行为。 */
  showModeMenu?: boolean;
  // Unattended / send-approvals-to-Inbox — folded into the Mode menu (§22): "who approves, and
  // when" is one mental model. Absent handler = no toggle (e.g. Chat).
  unattended?: boolean;
  onUnattendedChange?: (on: boolean) => void;
  approvalSlot?: ReactNode;
  // Push text + attachments into the composer (e.g. a start-panel task card). The `nonce` makes
  // repeated identical prefills re-apply; the user can still edit before sending.
  prefill?: { text: string; attachments?: Attachment[]; nonce: number };
  // Changes when the active conversation changes; clears any unsent draft.
  resetKey?: string;
  // Surface-specific hint shown in the empty textarea.
  placeholder?: string;
  /** 当前激活的专家（专家模式）；存在时在 composer 顶部显示专家条。 */
  expert?: Expert | null;
  /** 退出专家模式（专家条的「退出」按钮）。 */
  onExitExpert?: () => void;
  /** 打开文稿选择弹窗（编辑专家的「上传文稿」按钮）。 */
  onOpenReportDialog?: () => void;
  // Per-session token usage (OPE-42) — absent/empty hides the usage chip entirely
  // (older servers, backends that don't report usage, fresh sessions).
  usage?: SessionUsage;
  // Context-window size (tokens) of the ACTIVE model, from the curated matrix;
  // undefined hides the fill meter (unverified/custom models) but keeps the counts.
  contextWindow?: number;
  // Settings toggle (default off): true shows the fill bar instead of the session total.
  contextBar?: boolean;
}

export function Composer(props: Props) {
  const { t } = useTranslation();
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  // "/" force-run popup (SKILLS-SPEC §4.1 #3). The popup derives from the draft: it is open while
  // the text is a bare "/query" (no whitespace yet) and no skill is picked. Selecting a row
  // inserts "/name " INLINE in the box (Claude-Code style — the slash text IS the state);
  // the user keeps typing after it, and on send the prefix is stripped while the skill name
  // rides the user_message as its own field. Editing the prefix away un-picks the skill.
  const [pendingSkill, setPendingSkill] = useState<SessionSkillRow | null>(null);
  const [slashSkills, setSlashSkills] = useState<SessionSkillRow[] | null>(null);
  const [slashIndex, setSlashIndex] = useState(0);
  const prefixIntact =
    pendingSkill !== null &&
    (text === `/${pendingSkill.name}` || text.startsWith(`/${pendingSkill.name} `));
  useEffect(() => {
    if (pendingSkill && !prefixIntact) setPendingSkill(null);
  }, [pendingSkill, prefixIntact]);
  const slashQuery =
    !prefixIntact && props.sessionId && text.startsWith("/") && !/\s/.test(text.slice(1))
      ? text.slice(1).toLowerCase()
      : null;
  const slashMatches = (slashSkills ?? []).filter((s) =>
    s.name.toLowerCase().includes(slashQuery ?? ""),
  );
  useEffect(() => {
    // Fetch on each popup open (fresh menu); drop when closed.
    if (slashQuery === null) {
      setSlashSkills(null);
      setSlashIndex(0);
      return;
    }
    if (slashSkills === null && props.sessionId) {
      sessionSkills(props.sessionId, props.workspace)
        .then((all) => setSlashSkills(all.filter((s) => s.enabled)))
        .catch(() => setSlashSkills([]));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slashQuery === null]);
  const pickSkill = (s: SessionSkillRow) => {
    setPendingSkill(s);
    setText(`/${s.name} `);
    textareaRef.current?.focus();
  };
  // UX-044: which "This session" submenu is open (bindings live server-side).
  const [bindMenu, setBindMenu] = useState<"memory" | "board" | null>(null);
  // Bindings need a session and a workspace surface (Chat has neither).
  const sessionRows = Boolean(props.sessionId && props.workspace !== undefined);
  const bindRow = (icon: "book" | "table", label: string, kind: "memory" | "board") => (
    <button
      className={
        "w-full flex items-center gap-2.5 px-3 py-1.5 text-ui text-left hover:bg-paper" +
        (bindMenu === kind ? " bg-paper" : "")
      }
      onClick={() => setBindMenu(bindMenu === kind ? null : kind)}
    >
      <Icon name={icon} size={15} className="shrink-0 text-muted" />
      <span className="flex-1">{label}</span>
      <Icon name="chevronRight" size={12} className="shrink-0 text-faint" />
    </button>
  );
  const [dragging, setDragging] = useState(false);
  const [attachMenuOpen, setAttachMenuOpen] = useState(false);
  const [dictation, setDictation] = useState<DictationStatus | null>(null);
  const [dictationBusy, setDictationBusy] = useState<string | null>(null);
  const [dictationError, setDictationError] = useState<string | null>(null);
  const [recordingSeconds, setRecordingSeconds] = useState(0);
  const [attachNotice, setAttachNotice] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const noticeTimer = useRef<number | null>(null);
  const [emoteOpen, setEmoteOpen] = useState(false);
  const pendingCursor = useRef<number | null>(null);

  // Rejected-attachment notice: visible ~8s, then clears (or on ✕).
  const showAttachNotice = (message: string) => {
    setAttachNotice(message);
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setAttachNotice(null), 8000);
  };

  useLayoutEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    const max = parseFloat(getComputedStyle(el).lineHeight || "22") * 4;
    const next = Math.min(el.scrollHeight, max);
    el.style.height = `${Math.max(next, 24)}px`;
    el.style.overflowY = el.scrollHeight > max ? "auto" : "hidden";
  }, [text]);

  // Clear the draft when the conversation changes, so a half-typed message / picked file doesn't
  // bleed from one session into another. MUST run before the prefill effect below — a doorway
  // that switches session (resetKey) and pushes a prefill in one render must clear first, then
  // let the prefill win (otherwise the clear wipes the prefill — SKILLS-SPEC §5.2 regression).
  useEffect(() => {
    setText("");
    setAttachments([]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.resetKey]);

  // Apply a prefill (text + attachments) pushed from outside, then focus the composer. Applied at
  // most once per nonce (a ref guards against StrictMode/re-render double-fires), and attachments
  // are de-duplicated so the same file never lands twice.
  const appliedNonce = useRef<number>(-1);
  useEffect(() => {
    const p = props.prefill;
    if (!p || p.nonce === appliedNonce.current) return;
    appliedNonce.current = p.nonce;
    setText(p.text);
    if (p.attachments?.length) setAttachments((cur) => mergeAttachments(cur, p.attachments!));
    textareaRef.current?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.prefill?.nonce]);

  // Dictation is intentionally native-only: the browser/dev build remains a local server client
  // and never turns on the browser microphone or ships audio anywhere.
  useEffect(() => {
    if (!isTauri()) return;
    const refresh = (event?: Event) => {
      const supplied = (event as CustomEvent<DictationStatus> | undefined)?.detail;
      if (supplied) {
        setDictation(supplied);
        return;
      }
      void getDictationStatus().then((status) => status && setDictation(status)).catch(() => {});
    };
    refresh();
    window.addEventListener("coworker:voice-input-changed", refresh);
    return () => window.removeEventListener("coworker:voice-input-changed", refresh);
  }, []);

  useEffect(() => {
    if (!dictation?.recording) {
      setRecordingSeconds(0);
      return;
    }
    const started = Date.now();
    const timer = window.setInterval(() => {
      setRecordingSeconds(Math.floor((Date.now() - started) / 1000));
    }, 250);
    return () => window.clearInterval(timer);
  }, [dictation?.recording]);

  // Live waveform: poll mic loudness at ~10Hz while recording; the bars scroll left so the
  // trace reads as a real input meter (owner catch on DMG #28 — the first cut's bars were
  // decorative constants and read as fake).
  const [levels, setLevels] = useState<number[]>([]);
  useEffect(() => {
    if (!dictation?.recording) {
      setLevels([]);
      return;
    }
    const timer = window.setInterval(() => {
      getDictationLevel().then((level) => {
        if (typeof level === "number") setLevels((cur) => [...cur.slice(-13), level]);
      }).catch(() => {});
    }, 100);
    return () => window.clearInterval(timer);
  }, [dictation?.recording]);

  useEffect(() => {
    if (!dictation?.recording) return;
    const cancelOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      void cancelDictation()
        .catch(() => undefined)
        .finally(() => {
          void getDictationStatus().then((status) => status && setDictation(status));
        });
    };
    window.addEventListener("keydown", cancelOnEscape);
    return () => window.removeEventListener("keydown", cancelOnEscape);
  }, [dictation?.recording]);

  const voiceReady = !!dictation?.supported && !!dictation?.model_verified && !!dictation?.test_passed;
  const recordingTime = `${Math.floor(recordingSeconds / 60)}:${String(recordingSeconds % 60).padStart(2, "0")}`;

  // Attach-time PDF thresholds (Settings → Token savings): a PDF over the user's page or
  // size limit is REJECTED with a visible notice — never attached, never silently dropped.
  // The rationale is token cost: a big PDF re-rides every turn of the conversation.
  const addFiles = async (files: FileList | File[]) => {
    const list = Array.from(files);
    let maxPages = 20;
    let maxMb = 10;
    if (list.some(isPdfFile)) {
      try {
        const s = await getSettings();
        if (s.pdf_max_pages) maxPages = s.pdf_max_pages;
        if (s.pdf_max_mb) maxMb = s.pdf_max_mb;
      } catch {
        /* offline settings fetch — fall back to defaults */
      }
    }
    const accepted: File[] = [];
    for (const file of list) {
      if (isPdfFile(file) && file.size > maxMb * 1024 * 1024) {
        showAttachNotice(
          t("composer.pdfSkippedSize", { name: file.name, actualSize: (file.size / 1024 / 1024).toFixed(1), limit: maxMb }),
        );
        continue;
      }
      accepted.push(file);
    }
    const read = (await Promise.all(accepted.map(readFile))).filter(Boolean) as Attachment[];
    const next: Attachment[] = [];
    for (const a of read) {
      if (a.kind === "pdf" && a.data_url) {
        const info = await inspectPdf(a.data_url).catch(() => null);
        if (info?.ok && (info.pages ?? 0) > maxPages) {
          showAttachNotice(
            t("composer.pdfSkippedPages", { name: a.name, pages: info.pages, limit: maxPages }),
          );
          continue;
        }
        if (info && !info.ok) {
          showAttachNotice(t("composer.pdfSkippedError", { name: a.name, error: info.error || t("composer.couldNotReadPdf") }));
          continue;
        }
      }
      next.push(a);
    }
    if (next.length) setAttachments((a) => mergeAttachments(a, next));
  };

  // The "+" menu offers typed shortcuts; each just narrows the OS picker's filter.
  const pickFiles = (accept: string) => {
    setAttachMenuOpen(false);
    if (fileInput.current) {
      fileInput.current.accept = accept;
      fileInput.current.click();
    }
  };

  const needsModel = props.modelReady === false;

  // Insert an [[emote:NAME]] token at the caret (or append), then restore the caret just
  // after it. pendingCursor is applied by the layout effect below once `text` re-renders.
  const insertEmote = (name: string) => {
    // 用标准 markdown 图片语法：alt 写中文表情名，既可读又能让 LLM 从 alt 直接读懂语义。
    // 占位符烤进当前激活 pack（emote:PACK:NAME）——表情自此自带所属包，渲染冻结，
    // 切换全局 pack 不会改写已发出的这条消息（多种表情可在同一对话共存）。
    const label = emoteLabel(name);
    const token = `![${label}](emote:${getActivePack()}:${name})`;
    const el = textareaRef.current;
    if (!el) {
      setText((d) => (d.trim() ? `${d.trimEnd()} ${token}` : token));
      setEmoteOpen(false);
      return;
    }
    const start = el.selectionStart ?? text.length;
    const end = el.selectionEnd ?? text.length;
    const next = text.slice(0, start) + token + text.slice(end);
    pendingCursor.current = start + token.length;
    setText(next);
    setEmoteOpen(false);
  };

  // Restore the caret after an emote insertion (text changed → re-render → here).
  useLayoutEffect(() => {
    if (pendingCursor.current === null || !textareaRef.current) return;
    const pos = pendingCursor.current;
    pendingCursor.current = null;
    textareaRef.current.focus();
    textareaRef.current.setSelectionRange(pos, pos);
  }, [text]);

  const submit = () => {
    // While the "/" popup is open the draft is a query, not a message — never send it.
    if (slashQuery !== null) return;
    // The visible "/name " prefix is UI state, not message text — strip it for the send;
    // the skill rides as its own field.
    const skill = prefixIntact ? pendingSkill!.name : undefined;
    const msg = (skill ? text.slice(skill.length + 1) : text).trim();
    if ((!msg && attachments.length === 0 && !skill) || props.running || dictation?.recording || dictationBusy) return;
    // No model connected: keep the draft (don't drop it) and send the user to setup instead.
    if (needsModel) {
      props.onConnectModel?.();
      return;
    }
    props.onSend(msg, attachments, skill);
    setText("");
    setAttachments([]);
    setPendingSkill(null);
  };

  const onKey = (e: React.KeyboardEvent) => {
    if (slashQuery !== null) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSlashIndex((i) => Math.min(i + 1, Math.max(slashMatches.length - 1, 0)));
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSlashIndex((i) => Math.max(i - 1, 0));
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setText("");
        return;
      }
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        const chosen = slashMatches[slashIndex];
        if (chosen) pickSkill(chosen);
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  const onPaste = (e: React.ClipboardEvent) => {
    const imgs = Array.from(e.clipboardData.items)
      .filter((it) => it.kind === "file" && it.type.startsWith("image/"))
      .map((it) => it.getAsFile())
      .filter(Boolean) as File[];
    if (imgs.length) {
      e.preventDefault();
      addFiles(imgs);
    }
  };

  const toggleDictation = async () => {
    if (!isTauri() || dictationBusy) return;
    setDictationError(null);
    try {
      if (dictation?.recording) {
        setDictationBusy(t("composer.transcribing"));
        const transcript = await stopDictation();
        if (transcript === null) throw new Error(t("composer.couldNotTranscribe"));
        if (transcript.trim()) {
          setText((draft) => (draft.trim() ? `${draft.trimEnd()} ${transcript.trim()}` : transcript.trim()));
        }
        setDictation(await getDictationStatus());
        textareaRef.current?.focus();
        return;
      }

      const status = dictation || (await getDictationStatus());
      if (!status) throw new Error(t("composer.voiceDictationUnavailable"));
      if (!status.supported || !status.model_verified || !status.test_passed) {
        props.onConfigureVoiceInput?.();
        return;
      }
      setDictationBusy(t("composer.startingMicrophone"));
      const recording = await startDictation();
      if (!recording?.recording) throw new Error(t("composer.couldNotStartMicrophone"));
      setDictation(recording);
    } catch (error) {
      setDictationError(error instanceof Error ? error.message : t("composer.voiceDictationUnavailable"));
      const status = await getDictationStatus();
      if (status) setDictation(status);
    } finally {
      setDictationBusy(null);
    }
  };

  const modelsLoaded = !!(props.models && props.models.length);
  // 模型选择器分组（2026-10-13 萧哥要求重构）：按「上游厂商 × 免费/付费」分组折叠。
  // 旧版只有 免费/付费 两档平铺——免费网关 / 火山 / 小米 / OpenRouter 几百项混列，
  // 更没法看。现在每组一个厂商（后端 model_vendors 供名），
  // 组可折叠（见 Dropdown），含当前选中项的组默认展开、小组（≤4）默认展开。
  const modelGroups = useMemo(() => {
    const tiers = props.modelTiers;
    const tierOf = (m: string): "free" | "paid" => {
      const known = tiers?.[m];
      if (known === "free" || known === "paid") return known;
      // 旧后端缺 model_tiers 时的前缀兜底：keyless 网关判免费。
      // ⚠️ opencode:/kilo:/omniroute: 三个前缀 2026-10-18 / 2026-10-24 已随外网
      // 免费车道删除（OmniRoute 免费层实测全死，2026-10-24），后端不再供这类 id
      // （见 PITFALLS §P4 与 tests/test_omniroute_removed.py），别再加回来。
      return m.startsWith("ollama:")
        ? "free"
        : "paid";
    };
    // 厂商名：后端 model_vendors（descriptor title，设置页同款名字）为准；
    // 旧后端缺字段时从 label 的 "· via X" 后缀抠，再不行归「其他」。
    const vendorOf = (m: string): string => {
      const v = props.modelVendors?.[m];
      if (v) return v;
      const lbl = props.modelLabels?.[m];
      const via = lbl?.split(" · ").slice(1).join(" · ");
      return via || t("composer.otherVendors");
    };
    const all = Array.from(new Set([props.model, ...(props.models || [])]));
    // 分组键 = 厂商 + 档位：OpenRouter 免费档/付费档同厂商不同档，必须分开成组。
    type Bucket = { title: string; tier: "free" | "paid"; options: Option[] };
    const buckets = new Map<string, Bucket>();
    for (const m of all) {
      const tier = tierOf(m);
      const title = vendorOf(m);
      const key = `${title}\u0000${tier}`;
      let bucket = buckets.get(key);
      if (!bucket) {
        bucket = { title, tier, options: [] };
        buckets.set(key, bucket);
      }
      bucket.options.push({ value: m, label: props.modelLabels?.[m] || shortModel(m) });
    }
    const groups = Array.from(buckets.values());
    // 排序：免费档在前；档内按厂商名。当前选中模型所在组置顶（打开先看到自己选的）。
    groups.sort((a, b) => {
      if (a.tier !== b.tier) return a.tier === "free" ? -1 : 1;
      return a.title.localeCompare(b.title);
    });
    const curIdx = groups.findIndex((g) => g.options.some((o) => o.value === props.model));
    if (curIdx > 0) {
      const [cur] = groups.splice(curIdx, 1);
      groups.unshift(cur);
    }
    return groups;
  }, [props.model, props.models, props.modelLabels, props.modelTiers, props.modelVendors, t]);

  const iconBtn =
    "w-7 h-7 grid place-items-center rounded-md text-muted hover:text-ink hover:bg-paper shrink-0";

  // The send button is accent only when there's something to send — subtle grey otherwise, so the
  // composer isn't carrying a constant blue dot.
  const hasContent = text.trim().length > 0 || attachments.length > 0;

  return (
    <div className="composer-wrap px-6 pb-5 pt-4">
      {props.expert && (
        <div className="expert-chip max-w-3xl mx-auto mb-1">
          <ExpertAvatar expert={props.expert} size={26} />
          <span className="expert-name">{props.expert.displayName}</span>
          {props.onOpenReportDialog && (
            <button
              type="button"
              className="expert-exit"
              onClick={props.onOpenReportDialog}
              title="选择文稿"
              aria-label="选择文稿"
            >
              <Icon name="file" size={14} />
            </button>
          )}
          <button
            type="button"
            className="expert-exit"
            onClick={props.onExitExpert}
            title={t("composer.exitExpert")}
            aria-label={t("composer.exitExpert")}
          >
            <Icon name="x" size={14} />
          </button>
        </div>
      )}
      {props.approvalSlot}

      {dictationError && (
        <div className="max-w-3xl mx-auto mb-2 px-1 text-meta text-red-600" role="alert">
          {dictationError}
        </div>
      )}

      {/* Rejected-attachment notice (PDF over the user's Token-savings thresholds). */}
      {attachNotice && (
        <div
          data-testid="attach-notice"
          className="max-w-3xl mx-auto mb-1.5 flex items-center gap-2 rounded-lg border border-warnInk/30 bg-warnSoft px-3 py-1.5 text-[12.5px] text-warnInk"
        >
          <span className="flex-1">{attachNotice}</span>
          <button
            className="shrink-0 opacity-60 hover:opacity-100"
            onClick={() => setAttachNotice(null)}
            title={t("composer.dismiss")}
          >
            ✕
          </button>
        </div>
      )}

      {/* Attachments preview — a strip ABOVE the input box (mock/Claude-style). */}
      {attachments.length > 0 && (
        <div className="max-w-3xl mx-auto mb-1.5 flex flex-wrap gap-2">
          {attachments.map((a, i) => (
            <AttachChip key={i} a={a} onRemove={() => setAttachments((all) => all.filter((_, j) => j !== i))} />
          ))}
        </div>
      )}

      <div
        className={
          "composer max-w-3xl mx-auto rounded-2xl border border-line bg-panel shadow-sm" +
          (dragging ? " dragging" : "")
        }
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
        }}
      >
        {/* "/" force-run popup — in-flow above the textarea; rows are the session's
            effective menu only (muted/disabled skills never appear). */}
        {slashQuery !== null && (
          <div className="px-2 pt-2" data-testid="skill-popup" role="listbox" aria-label="Skills">
            {slashSkills === null ? (
              <div className="px-2 py-1.5 text-meta text-faint">Loading skills…</div>
            ) : slashMatches.length === 0 ? (
              <div className="px-2 py-1.5 text-meta text-faint">No matching skills.</div>
            ) : (
              slashMatches.map((s, i) => (
                <button
                  key={s.name}
                  role="option"
                  aria-selected={i === slashIndex}
                  className={
                    "w-full text-left flex items-center gap-2 px-2 py-1.5 rounded-lg " +
                    (i === slashIndex ? "bg-paper" : "hover:bg-paper")
                  }
                  onMouseEnter={() => setSlashIndex(i)}
                  onClick={() => pickSkill(s)}
                >
                  <span className="text-ui font-medium text-accent shrink-0">/{s.name}</span>
                  <span className="text-meta text-faint truncate flex-1">{s.description}</span>
                  <span className="text-label px-1.5 py-0.5 rounded-full border border-line text-faint shrink-0">
                    {s.scope}
                  </span>
                </button>
              ))
            )}
          </div>
        )}
        <textarea
          ref={textareaRef}
          className="w-full block px-3.5 pt-3.5 pb-1.5 text-[14.5px]"
          placeholder={props.placeholder || t("composer.placeholder")}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKey}
          onPaste={onPaste}
          rows={1}
        />

        {/* Three-control row (§22): + attach · Mode ⌄ …(right)… model (fresh only) · send */}
        <div className="px-2.5 pb-2.5 pt-1 flex items-center gap-1.5">
          {/* + attach menu */}
          <div className="relative">
            <button
              className={iconBtn + (attachMenuOpen ? " bg-paper text-ink" : "")}
              title={t("composer.attach")}
              aria-label={t("composer.attach")}
              onClick={() => setAttachMenuOpen((v) => !v)}
            >
              <Icon name="plus" size={17} />
            </button>
            {attachMenuOpen && (
              <>
                <div className="fixed inset-0 z-30" onClick={() => setAttachMenuOpen(false)} />
                <div className="absolute z-40 bottom-full mb-1 left-0 min-w-[180px] rounded-xl border border-line bg-panel shadow-2xl py-1.5">
                  {attachItem("image", t("composer.photoOrImage"), () => pickFiles("image/*"))}
                  {attachItem("file", t("composer.pdf"), () => pickFiles("application/pdf,.pdf"))}
                  {attachItem(
                    "fileCode",
                    t("composer.otherFiles"),
                    () => pickFiles("text/*,.md,.csv,.json,.yaml,.yml,.log,.py,.ts,.tsx,.js,.rs,.go,.toml"),
                  )}
                  {sessionRows && (
                    <>
                      <div className="my-1 border-t border-line" />
                      <div className="px-3 pt-0.5 pb-0.5 text-[10.5px] font-semibold tracking-wide uppercase text-faint">
                        This session
                      </div>
                      {bindRow("book", "Project memory", "memory")}
                      {bindRow("table", "Board", "board")}
                    </>
                  )}
                </div>
              </>
            )}
          </div>

          {bindMenu && props.sessionId && (
            <ProjectBindMenu
              sessionId={props.sessionId}
              kind={bindMenu}
              onClose={() => {
                setBindMenu(null);
                setAttachMenuOpen(false);
              }}
              onOpenMemory={props.onOpenMemory}
            />
          )}

          {/* emote picker */}
          <div className="relative">
            <button
              type="button"
              className={iconBtn + (emoteOpen ? " bg-paper text-ink" : "")}
              title="插入小桔表情"
              aria-label="插入小桔表情"
              onClick={() => setEmoteOpen((v) => !v)}
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <circle cx="12" cy="12" r="9" />
                <path d="M8 14s1.5 2 4 2 4-2 4-2" />
                <line x1="9" y1="9.5" x2="9.01" y2="9.5" />
                <line x1="15" y1="9.5" x2="15.01" y2="9.5" />
              </svg>
            </button>
            {emoteOpen && (
              <>
                <div className="fixed inset-0 z-30" onClick={() => setEmoteOpen(false)} />
                <EmotePicker onPick={insertEmote} />
              </>
            )}
          </div>

          <input
            ref={fileInput}
            type="file"
            multiple
            style={{ display: "none" }}
            onChange={(e) => {
              if (e.target.files) addFiles(e.target.files);
              e.target.value = "";
            }}
          />

          {/* Listening replaces the quiet middle controls with a LIVE waveform (mic RMS,
              polled ~10Hz, scrolling left) + elapsed time (§37). */}
          {dictation?.recording ? (
            <div className="voice-wave-row flex-1 flex items-center gap-2 ml-1" aria-hidden="true">
              <span className="voice-wave-line" />
              <span className="voice-wave-bars">
                {Array.from({ length: 14 }, (_, index) => {
                  const level = levels[levels.length - 14 + index] ?? 0;
                  return <i key={index} style={{ height: Math.round(4 + level * 24) }} />;
                })}
              </span>
              <span className="text-meta text-muted tabular-nums">{recordingTime}</span>
            </div>
          ) : (props.showModeMenu ?? (props.workspace !== undefined)) ? (
            <ModeMenu
              mode={props.mode}
              onModeChange={props.onModeChange}
              unattended={props.unattended}
              onUnattendedChange={props.onUnattendedChange}
            />
          ) : null}

          {dictationBusy === t("composer.transcribing") && <span className="text-[11.5px] text-accent">{t("composer.transcribing")}</span>}

          <span className="ml-auto" />

          {/* token usage (OPE-42) — a quiet chip; hidden until the server reports usage.
              Shows the context-window fill bar alone (the session total lives in the
              popover), or the session total when there's no window / the bar is off. */}
          {!dictation?.recording && props.usage && totalTokens(props.usage) > 0 && (
            <UsageChip
              usage={props.usage}
              contextWindow={props.contextWindow}
              contextBar={props.contextBar}
              model={props.model}
              modelLabels={props.modelLabels}
            />
          )}

          {/* model — a quiet chip, now for the session's whole life (§17 rev 2026-07-22:
              mid-session switching shipped, so the picker stays actionable; the topbar
              subtitle still states the current model). */}
          {!dictation?.recording && (needsModel ? (
            <button
              className="pill model-warn chip"
              onClick={() => props.onConnectModel?.()}
              title={t("composer.connectModel")}
              aria-label={t("composer.connectModelAria")}
            >
              <span className="pill-label">{t("composer.noModel")}</span>
              <span className="model-warn-ico" aria-hidden>⚠</span>
            </button>
          ) : modelsLoaded ? (
            <Dropdown value={props.model} groups={modelGroups} onChange={props.onModelChange} align="right" />
          ) : (
            <button
              className="pill chip text-faint cursor-default"
              disabled
              data-testid="models-loading"
              title={t("composer.loadingModelsFetching")}
            >
              <span className="pill-label">{t("composer.loadingModels")}</span>
            </button>
          ))}

          {/* mic — immediately before send (owner call, DMG #28 walkthrough) */}
          {isTauri() && (
            <button
              className={
                iconBtn +
                (dictation?.recording ? " bg-red-50 text-red-600 hover:bg-red-100" : "") +
                (dictationBusy ? " opacity-60" : "") +
                (!voiceReady && !dictation?.recording ? " opacity-40" : "")
              }
              onClick={() => void toggleDictation()}
              disabled={!!dictationBusy}
              title={
                dictationBusy ||
                (dictation?.recording
                  ? t("composer.stopRecordingTooltip")
                  : voiceReady
                    ? t("composer.startDictationTooltip")
                    : t("composer.configureVoiceTooltip"))
              }
              aria-label={dictation?.recording ? t("composer.stopDictationAria") : voiceReady ? t("composer.startDictationAria") : t("composer.configureVoiceAria")}
              aria-disabled={!voiceReady && !dictation?.recording}
            >
              <Icon name={dictation?.recording ? "stop" : "mic"} size={16} />
            </button>
          )}

          {/* send / stop */}
          {props.running ? (
            <button className="btn danger" onClick={props.onInterrupt}>
              {t("composer.stop")}
            </button>
          ) : (
            <button
              className={
                "w-7 h-7 rounded-full grid place-items-center shrink-0 transition-colors " +
                (hasContent && props.connected && !dictation?.recording && !dictationBusy
                  ? "bg-accent text-white hover:brightness-105"
                  : "bg-paper border border-line text-faint")
              }
              onClick={submit}
              disabled={!props.connected || !!dictation?.recording || !!dictationBusy}
              title={needsModel ? t("composer.connectModelToSend") : undefined}
              aria-label={t("composer.sendAria")}
            >
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M12 19V5M5 12l7-7 7 7" />
              </svg>
            </button>
          )}
        </div>
      </div>
      <span className="sr-only" role="status" aria-live="polite">
        {dictation?.recording ? t("composer.listening", { time: recordingTime }) : dictationBusy || ""}
      </span>
    </div>
  );
}

// Token-usage chip + popover (OPE-42). Trigger: a tiny context-fill meter (only when the
// active model's window is known) + the session's total token count. Click → per-model
// breakdown. Tokens only, never dollars (true cost is unknowable client-side — discounted
// pricing, per-provider cache billing).
function UsageChip({
  usage,
  contextWindow,
  contextBar,
  model,
  modelLabels,
}: {
  usage: SessionUsage;
  contextWindow?: number;
  contextBar?: boolean;
  model: string;
  modelLabels?: Record<string, string>;
}) {
  const [open, setOpen] = useState(false);
  const total = totalTokens(usage);
  const pct = contextWindow
    ? Math.min(100, Math.round((usage.context / contextWindow) * 100))
    : null;
  // Settings can hide the bar; without a known window there is nothing to fill either.
  const showBar = pct !== null && contextBar === true;
  const labelFor = (id: string) =>
    id === "unknown" ? "Unknown model" : modelLabels?.[id] || shortModel(id);
  // One field per line, session-summed (owner ask 2026-07-28). Values are cumulative
  // across the whole session, never just the last turn; "Input" is the fresh
  // (uncached) share — the cached share sits in the cache rows at its own price.
  const stat = (label: string, value: number) => (
    <div className="flex items-baseline justify-between text-[11.5px] leading-snug">
      <span className="text-faint">{label}</span>
      <span className="text-ink tabular-nums">{formatTokens(value)}</span>
    </div>
  );
  return (
    <div className="relative">
      <button
        className="inline-flex items-center gap-1.5 px-2 py-1 rounded-lg text-[11.5px] text-muted hover:text-ink hover:bg-paper shrink-0"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Token usage"
        title={
          showBar
            ? `Context window ${pct}% full · ${formatTokens(total)} tokens this session`
            : `Token usage this session: ${formatTokens(total)}`
        }
        data-testid="usage-chip"
      >
        {/* The bar is the context-window fill; pairing it with the session TOTAL read as
            "total is N% of the window", which it never was. Bar alone when we have a
            window, the session total only when we don't (so the chip is never empty). */}
        {showBar ? (
          <span className="w-12 h-1.5 rounded-full bg-line overflow-hidden" aria-hidden="true">
            <span
              className="block h-full bg-accent transition-all"
              style={{ width: `${Math.max(pct as number, 4)}%` }}
            />
          </span>
        ) : (
          <span className="tabular-nums">{formatTokens(total)}</span>
        )}
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-30" onClick={() => setOpen(false)} />
          <div
            className="absolute z-40 bottom-full mb-1 right-0 w-[280px] rounded-xl border border-line bg-panel shadow-2xl p-3"
            role="menu"
            data-testid="usage-popover"
          >
            {contextWindow ? (
              <div className="mb-2.5">
                <div className="text-[10.5px] uppercase tracking-[0.06em] text-faint font-semibold mb-1">
                  Context window
                </div>
                <div className="h-1.5 rounded-full bg-line overflow-hidden">
                  <div
                    className="h-full bg-accent transition-all"
                    style={{ width: `${pct}%` }}
                  />
                </div>
                <div className="mt-1 text-[11.5px] text-muted tabular-nums">
                  {formatTokens(usage.context)} of {formatTokens(contextWindow)} · {pct}%
                </div>
              </div>
            ) : usage.context > 0 ? (
              <div className="mb-2.5 text-[11.5px] text-muted tabular-nums">
                In context now: {formatTokens(usage.context)} tokens
              </div>
            ) : null}
            <div className="text-[10.5px] uppercase tracking-[0.06em] text-faint font-semibold mb-1">
              Session totals
            </div>
            <div className="flex flex-col gap-1.5">
              {Object.entries(usage.byModel).map(([id, t]) => (
                <div key={id}>
                  <div className="text-meta text-ink font-medium truncate" title={id}>
                    {labelFor(id)}
                  </div>
                  {/* Every row is a session sum. With a cache split, the input rows are
                      the three BILLING CLASSES of input (each priced differently) and
                      read as components: uncached + cache reads + cache writes = total.
                      Without one (Ollama, compat vendors), plain "Input" says it all. */}
                  <div className="mt-0.5 flex flex-col gap-0.5">
                    {t.cache_read + t.cache_write > 0 ? (
                      <>
                        {stat("Uncached input", t.input)}
                        {stat("Cache reads", t.cache_read)}
                        {stat("Cache writes", t.cache_write)}
                        {stat("Total input", t.input + t.cache_read + t.cache_write)}
                      </>
                    ) : (
                      stat("Input", t.input)
                    )}
                    {stat("Output", t.output)}
                  </div>
                </div>
              ))}
            </div>
            <div className="mt-2 pt-2 border-t border-line flex items-baseline justify-between text-[11.5px]">
              <span className="text-faint">Total</span>
              <span className="text-ink tabular-nums">{formatTokens(total)} tokens</span>
            </div>
            {model && !modelLabels?.[model] && contextWindow === undefined && (
              <div className="mt-1 text-[10.5px] text-faint leading-snug">
                Context meter unavailable for custom models.
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

// The composer's Mode menu (§22): a quiet "Mode ⌄" chip opening the five permission options with
// the current one marked, plus — when the session supports it — the "Send approvals to Inbox"
// toggle at the bottom (the old standalone InboxControl, folded in).
function ModeMenu({
  mode,
  onModeChange,
  unattended,
  onUnattendedChange,
}: {
  mode: string;
  onModeChange: (mode: string) => void;
  unattended?: boolean;
  onUnattendedChange?: (on: boolean) => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const current = PERMISSION_OPTIONS.find((o) => o.value === mode);
  return (
    <div className="relative">
      {/* Borderless, and it names the CHOSEN mode (owner ask 2026-07-11, competitor composer
          comparison): "Ask for approval ⌄" not a generic "Mode ⌄" pill. aria-label stays
          "Mode" so the accessible name is stable across mode changes. */}
      <button
        className="inline-flex items-center gap-1 px-2 py-1 rounded-lg text-meta text-muted hover:text-ink hover:bg-paper shrink-0"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t("composer.mode")}
        title={
          t("composer.modeTooltip", { label: current?.label || mode }) +
          (unattended ? t("composer.approvalsGoToInbox") : "")
        }
      >
        {current?.label || mode}
        <Icon name="chevronDown" size={11} className="text-faint" />
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-30" onClick={() => setOpen(false)} />
          <div
            className="absolute z-40 bottom-full mb-1 left-0 w-[260px] rounded-xl border border-line bg-panel shadow-2xl p-1.5"
            role="menu"
            data-testid="mode-menu"
          >
            {PERMISSION_OPTIONS.map((o) => (
              <button
                key={o.value}
                className="w-full flex flex-col items-start px-2.5 py-1.5 rounded-lg text-left hover:bg-paper"
                onClick={() => {
                  onModeChange(o.value);
                  setOpen(false);
                }}
              >
                <span
                  className={
                    "text-ui " + (o.value === mode ? "font-medium text-accent" : "text-ink")
                  }
                >
                  {t(o.translationKey)}
                  {o.value === mode && <span className="ml-1.5">✓</span>}
                </span>
                <span className="text-label text-faint leading-snug">{t(o.descKey)}</span>
              </button>
            ))}
            {onUnattendedChange && (
              <>
                <div className="my-1 border-t border-line" />
                <div className="flex items-center gap-2 px-2.5 py-1.5">
                  <span className="flex-1 min-w-0">
                    <span className="block text-ui text-ink">{t("composer.sendApprovalsToInbox")}</span>
                    <span className="block text-label text-faint leading-snug">
                      {t("composer.sendApprovalsToInboxDescription")}
                    </span>
                  </span>
                  <Toggle
                    checked={!!unattended}
                    onChange={onUnattendedChange}
                    title={t("composer.sendApprovalsToInboxTitle")}
                  />
                </div>
              </>
            )}
          </div>
        </>
      )}
    </div>
  );
}

// A row in the "+" attach menu.
function attachItem(icon: "image" | "file" | "fileCode", label: string, onClick: () => void) {
  return (
    <button
      className="w-full flex items-center gap-2.5 px-3 py-1.5 text-ui text-left hover:bg-paper"
      onClick={onClick}
    >
      <Icon name={icon} size={15} className="shrink-0 text-muted" /> {label}
    </button>
  );
}

function AttachChip({ a, onRemove }: { a: Attachment; onRemove: () => void }) {
  const { t } = useTranslation();
  return (
    <div className={"attach-chip" + (a.kind === "image" ? " img" : "")}>
      {a.kind === "image" ? (
        <img src={a.data_url} alt={a.name} />
      ) : (
        <>
          <Icon name="file" size={13} />
          <span className="attach-name">{a.name}</span>
        </>
      )}
      <button className="attach-x" onClick={onRemove} title={t("composer.remove")}>
        ✕
      </button>
    </div>
  );
}
