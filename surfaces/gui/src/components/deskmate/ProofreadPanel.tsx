import { useEffect, useRef, useState } from "react";
import {
  calibrateWritingStyle,
  getWritingStyle,
  listManuscriptChapters,
  listManuscripts,
  loadManuscript,
  proofreadDraft,
  saveManuscript,
  type ManuscriptChapter,
  type ManuscriptInfo,
  type ProofreadItem,
  type ProofreadResult,
  type StyleProfile,
} from "../../api";
import { Icon } from "../Icon";

/** 聊天「审阅第X章」触发时，由 DeskMateOverlay 传入，用于预载面板到指定章节。 */
export interface ProofreadSeed {
  name: string;
  chapter_idx: number | null;
  chapter_title: string;
  chapters: ManuscriptChapter[];
  draft: string;
  result: ProofreadResult;
}

interface Props {
  open: boolean;
  onClose: () => void;
  seed?: ProofreadSeed | null;
  /** 首页 chip 进入焦点：edit 常规 / manuscripts 书稿库（自动载入最近一本）/ style 风格库（未校准则唤起校准）。 */
  initialFocus?: "edit" | "manuscripts" | "style";
}

// 六个编辑维度对应的标签色
const TYPE_COLOR: Record<string, string> = {
  冗余: "#ef9a9a",
  节奏: "#ffcc80",
  用词: "#a5d6a7",
  连贯: "#90caf9",
  错字: "#ce93d8",
  考据: "#ffab91",
};

function bufToBase64(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(
      null,
      Array.from(bytes.subarray(i, i + chunk)),
    );
  }
  return btoa(binary);
}

async function readFileInput(file: File): Promise<{
  text?: string;
  content_base64?: string;
  filename: string;
}> {
  const name = file.name;
  const lower = name.toLowerCase();
  if (lower.endsWith(".txt")) {
    const t = await file.text();
    return { text: t, filename: name };
  }
  if (lower.endsWith(".docx")) {
    const buf = await file.arrayBuffer();
    return { content_base64: bufToBase64(buf), filename: name };
  }
  throw new Error("仅支持 .txt / .docx 文件");
}

export function ProofreadPanel({ open, onClose, seed, initialFocus }: Props) {
  const [draft, setDraft] = useState("");
  const [draftFile, setDraftFile] = useState<{ content_base64: string; filename: string } | null>(null);
  const [manuscriptText, setManuscriptText] = useState<string | null>(null);
  const [manuscriptName, setManuscriptName] = useState<string | null>(null);
  const [savedManuscripts, setSavedManuscripts] = useState<ManuscriptInfo[]>([]);
  const [chapters, setChapters] = useState<ManuscriptChapter[]>([]);
  const [selectedChapter, setSelectedChapter] = useState<number | null>(null);
  const [profile, setProfile] = useState<StyleProfile | null>(null);
  const [profileSource, setProfileSource] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<ProofreadResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [applied, setApplied] = useState<ProofreadItem[]>([]);
  const [toast, setToast] = useState<string | null>(null);
  const toastTimer = useRef<number | null>(null);
  const draftInputRef = useRef<HTMLInputElement>(null);
  const sampleInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    setError(null);
    getWritingStyle()
      .then((d) => setProfile(d.profile))
      .catch(() => {/* 静默 */});
    listManuscripts().then(setSavedManuscripts).catch(() => {});
  }, [open]);

  // 聊天「审阅第X章」触发：用 seed 预载到指定章节
  useEffect(() => {
    if (!seed) return;
    setChapters(seed.chapters ?? []);
    setSelectedChapter(seed.chapter_idx ?? null);
    setDraft(seed.draft ?? "");
    setManuscriptText(seed.draft ?? null);
    setManuscriptName(seed.name ?? null);
    setResult(seed.result ?? null);
    setDraftFile(null);
    setApplied([]);
    setHidden(new Set());
    setError(null);
  }, [seed]);

  // 粘贴/输入时（非 docx、未缩放至某章）自动识别章节，填充下拉
  useEffect(() => {
    if (draftFile) return;
    if (selectedChapter !== null) return;
    if (!draft.trim() || draft.startsWith("（已载入")) return;
    const t = window.setTimeout(() => {
      listManuscriptChapters({ text: draft })
        .then(setChapters)
        .catch(() => {});
    }, 800);
    return () => window.clearTimeout(t);
  }, [draft, draftFile, selectedChapter]);

  // 首页 chip 进入焦点：书稿库 → 自动载入最近一本；风格库且未校准 → 唤起校准文件选择。
  const autoFocusDone = useRef(false);
  useEffect(() => {
    if (!open) {
      autoFocusDone.current = false;
      return;
    }
    if (autoFocusDone.current) return;
    if (initialFocus === "manuscripts" && !manuscriptName && savedManuscripts.length > 0) {
      autoFocusDone.current = true;
      onLoadManuscript(savedManuscripts[0].name);
    } else if (initialFocus === "style" && !profile) {
      autoFocusDone.current = true;
      const tmr = window.setTimeout(() => sampleInputRef.current?.click(), 80);
      return () => window.clearTimeout(tmr);
    }
  }, [open, initialFocus, savedManuscripts, profile, manuscriptName]);

  if (!open) return null;

  const emitEmotion = (emotion: string) =>
    window.dispatchEvent(new CustomEvent("deskmate:emotion", { detail: { emotion } }));

  const showToast = (msg: string) => {
    setToast(msg);
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 3200);
  };

  const zoomToChapter = (idx: number | null) => {
    if (idx === null) {
      setSelectedChapter(null);
      setDraft(draftFile ? `（已载入 Word 文档：${draftFile.filename}，已按章拆分；从上方下拉选某章审单章，或保持「全部」审整篇）` : (manuscriptText ?? ""));
      return;
    }
    const ch = chapters.find((c) => c.idx === idx);
    if (!ch) return;
    setSelectedChapter(idx);
    if (ch.text) {
      setDraft(ch.text);
    } else if (manuscriptName) {
      // 聊天审阅触发的章节无 text 字段，从已存书稿按需加载
      loadManuscript(manuscriptName).then((cs) => {
        const loaded = cs.find((c) => c.idx === idx);
        if (loaded) {
          setDraft(loaded.text);
          setChapters((prev) => prev.map((c) => c.idx === idx ? { ...loaded } : c));
        }
      }).catch(() => {});
    }
  };

  const onPickDraft = async (file: File) => {
    setError(null);
    try {
      const r = await readFileInput(file);
      setResult(null);
      setApplied([]);
      setHidden(new Set());
      if (r.text !== undefined) {
        setManuscriptText(r.text);
        setDraftFile(null);
        setDraft(r.text);
        setSelectedChapter(null);
        const cs = await listManuscriptChapters({ text: r.text });
        setChapters(cs);
        const base = r.filename.replace(/\.(docx|txt)$/i, "");
        setManuscriptName(base);
        saveManuscript(base, cs).catch(() => {});
        listManuscripts().then(setSavedManuscripts).catch(() => {});
      } else {
        setDraftFile({ content_base64: r.content_base64!, filename: r.filename });
        setManuscriptText(null);
        const cs = await listManuscriptChapters({ content_base64: r.content_base64, filename: r.filename });
        setChapters(cs);
        const base = r.filename.replace(/\.(docx|txt)$/i, "");
        setManuscriptName(base);
        saveManuscript(base, cs).catch(() => {});
        listManuscripts().then(setSavedManuscripts).catch(() => {});
        if (cs.length <= 1) {
          setSelectedChapter(null);
          setDraft(cs[0]?.text ?? `（已载入 Word 文档：${r.filename}）`);
        } else {
          setSelectedChapter(cs[0].idx);
          setDraft(cs[0].text);
        }
      }
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const onLoadManuscript = async (name: string) => {
    if (!name) return;
    setError(null);
    try {
      const cs = await loadManuscript(name);
      setManuscriptName(name);
      setChapters(cs);
      setDraftFile(null);
      setSelectedChapter(null);
      const full = cs.map((c) => c.text).join("\n\n");
      setManuscriptText(full);
      setDraft(full);
      setResult(null);
      setApplied([]);
      setHidden(new Set());
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const onProofread = async () => {
    setError(null);
    setApplied([]);
    setHidden(new Set());
    const opts: Record<string, unknown> = {};
    if (selectedChapter !== null) {
      const ch = chapters.find((c) => c.idx === selectedChapter);
      if (ch) {
        opts.text = ch.text;
        opts.chapter = selectedChapter;
      }
    }
    if (!opts.text) {
      if (draftFile) {
        opts.content_base64 = draftFile.content_base64;
        opts.filename = draftFile.filename;
      } else if (draft.trim()) {
        opts.text = draft;
      } else {
        setError("请先粘贴文稿，或点「打开文件」选择 .txt / .docx");
        return;
      }
    }
    setRunning(true);
    emitEmotion("thinking");
    try {
      const res = await proofreadDraft(opts);
      setResult(res);
      // 同步章节元信息（含后端重新识别的），便于下拉展示与定位
      // 保留已有章节文本，仅更新后端返回的元信息（title, char_count 等）
      if (res.chapters && res.chapters.length) {
        // 前端 chapters 基于全本 split 且带 text，最可信；后端 res.chapters 既无 text、
        // 又可能因传入单章文本被重新切分导致序号错位，故前端已有章节时直接保留、不覆盖。
        setChapters((prev) => {
          if (prev.length && prev[0].text) return prev;
          return res.chapters as unknown as ManuscriptChapter[];
        });
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRunning(false);
      emitEmotion("happy");
    }
  };

  const onCalibrate = async (file: File) => {
    setError(null);
    try {
      const r = await readFileInput(file);
      const opts: Record<string, unknown> = {};
      if (r.text !== undefined) opts.text = r.text;
      else {
        opts.content_base64 = r.content_base64;
        opts.filename = r.filename;
      }
      const res = await calibrateWritingStyle(opts);
      setProfile(res.profile);
      setProfileSource(`${r.filename}（${res.chars} 字）`);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const onAccept = (item: ProofreadItem) => {
    setApplied((prev) => (prev.some((x) => x.id === item.id) ? prev : [...prev, item]));
    if (draft.includes(item.excerpt)) {
      const next = draft.replace(item.excerpt, item.suggestion);
      setDraft(next);
      // 若当前在某章内，回写也同步进章节缓存，再次缩放不丢改动
      if (selectedChapter !== null) {
        setChapters((prev) =>
          prev.map((c) => (c.idx === selectedChapter ? { ...c, text: next } : c)),
        );
      }
      showToast("已采纳，已改回左侧草稿");
    } else {
      navigator.clipboard?.writeText(item.suggestion).catch(() => {});
      showToast("已采纳并记入清单，建议已复制到剪贴板");
    }
    setHidden((prev) => new Set(prev).add(item.id));
  };

  const onReject = (item: ProofreadItem) => {
    setHidden((prev) => new Set(prev).add(item.id));
  };

  const exportName = () => {
    if (selectedChapter !== null) {
      const ch = chapters.find((c) => c.idx === selectedChapter);
      if (ch) return ch.title;
    }
    return draftFile?.filename?.replace(/\.(docx|txt)$/i, "") || "manuscript";
  };

  const onExportApplied = () => {
    if (applied.length === 0) return;
    const lines = applied.map((it, i) => {
      const head = `【${i + 1}】(${it.type})`;
      const reason = it.reason ? `\n理由：${it.reason}` : "";
      return `${head}\n原文：${it.excerpt}\n建议：${it.suggestion}${reason}\n`;
    });
    const stamp = new Date().toLocaleString("zh-CN");
    const title = selectedChapter !== null ? `（第 ${selectedChapter} 章）` : "（全篇）";
    const header = `《${exportName()}》${title}已采纳的修改（共 ${applied.length} 条）\n导出时间：${stamp}\n\n`;
    const blob = new Blob([header + lines.join("\n")], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `采纳修改_${exportName()}.txt`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  const onExportChapter = () => {
    if (!draft.trim() || draft.startsWith("（已载入")) {
      showToast("请先选择具体章节，再导出本章");
      return;
    }
    const blob = new Blob([draft], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${exportName()}.txt`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  const visibleItems = result?.items.filter((it) => !hidden.has(it.id)) ?? [];
  const scopeLabel =
    selectedChapter !== null ? `审：第 ${selectedChapter} 章` : "审：整篇 / 全部";
  const canPickChapter = chapters.length > 1;

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center"
      style={{ background: "rgba(0,0,0,0.4)" }}
    >
      <div
        className="rounded-2xl border shadow-2xl flex flex-col overflow-hidden"
        style={{
          width: "min(1040px, 94vw)",
          height: "min(86vh, 820px)",
          background: "var(--panel)",
          borderColor: "var(--line)",
          position: "relative",
        }}
      >
        {/* Header */}
        <div
          className="flex items-center justify-between px-4 py-3"
          style={{ borderBottom: "1px solid var(--line)" }}
        >
          <div className="flex items-center gap-2">
            <Icon name="pencil" size={16} />
            <span className="text-[14px] font-semibold" style={{ color: "var(--ink)" }}>
              文稿编校
            </span>
            {manuscriptName && (
              <span
                className="inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded"
                style={{ color: "var(--accent)", background: "var(--paper)" }}
              >
                <Icon name="archive" size={12} />
                {manuscriptName}
              </span>
            )}
            {profile?.summary && (
              <span
                className="inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded"
                style={{ color: "var(--accent)", background: "var(--paper)" }}
              >
                <Icon name="sparkle" size={12} />
                已校准风格
              </span>
            )}
          </div>
          <button
            className="w-7 h-7 rounded flex items-center justify-center"
            style={{ color: "var(--faint)" }}
            onClick={onClose}
            aria-label="关闭"
          >
            <Icon name="x" size={16} />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 min-h-0 flex flex-col md:flex-row">
          {/* 左：草稿 */}
          <div
            className="flex-1 min-h-0 flex flex-col p-3 gap-2"
            style={{ borderRight: "1px solid var(--line)" }}
          >
            <div className="flex flex-wrap items-center gap-2">
              <button
                className="inline-flex items-center gap-1.5 text-[12px] px-2.5 py-1.5 rounded border"
                style={{ borderColor: "var(--line)", color: "var(--ink)" }}
                onClick={() => draftInputRef.current?.click()}
              >
                <Icon name="folder" size={13} />
                打开文件
              </button>
              <button
                className="inline-flex items-center gap-1.5 text-[12px] px-2.5 py-1.5 rounded border"
                style={{ borderColor: "var(--line)", color: "var(--ink)" }}
                onClick={() => sampleInputRef.current?.click()}
              >
                <Icon name="refresh" size={13} />
                校准我的风格
              </button>
              {savedManuscripts.length > 0 && (
                <select
                  className="text-[12px] px-2 py-1.5 rounded border"
                  style={{ borderColor: "var(--line)", color: "var(--ink)", background: "var(--paper)" }}
                  value=""
                  onChange={(e) => {
                    const v = e.target.value;
                    if (v) onLoadManuscript(v);
                    e.currentTarget.value = "";
                  }}
                  title="打开已存的书稿"
                >
                  <option value="">书稿…</option>
                  {savedManuscripts.map((m) => (
                    <option key={m.name} value={m.name}>
                      {m.name}（{m.chapter_count}章）
                    </option>
                  ))}
                </select>
              )}
              {canPickChapter && (
                <select
                  className="text-[12px] px-2 py-1.5 rounded border"
                  style={{ borderColor: "var(--line)", color: "var(--ink)", background: "var(--paper)" }}
                  value={selectedChapter ?? "all"}
                  onChange={(e) => {
                    const v = e.target.value;
                    zoomToChapter(v === "all" ? null : Number(v));
                  }}
                >
                  <option value="all">全部 / 整篇</option>
                  {chapters.map((c) => (
                    <option key={c.idx} value={c.idx}>
                      {c.title}（{c.char_count} 字）
                    </option>
                  ))}
                </select>
              )}
              <button
                className="text-[12px] px-3 py-1.5 rounded font-medium"
                style={{ background: "var(--accent)", color: "#fff" }}
                onClick={onProofread}
                disabled={running}
              >
                {running ? "批改中…" : "开始批改"}
              </button>
              <button
                className="inline-flex items-center gap-1.5 text-[12px] px-2.5 py-1.5 rounded border"
                style={{ borderColor: "var(--line)", color: "var(--muted)" }}
                onClick={onExportChapter}
                title="导出当前草稿（或当前章节）为 .txt"
              >
                <Icon name="file" size={13} />
                导出本章
              </button>
              {profileSource && (
                <span className="text-[11px]" style={{ color: "var(--muted)" }}>
                  风格样本：{profileSource}
                </span>
              )}
            </div>
            <div className="text-[11px]" style={{ color: "var(--faint)" }}>
              {scopeLabel}
              {draftFile && chapters.length > 1 && " · Word 文档已按章拆分，选章后可逐章审"}
            </div>
            {error && (
              <div
                className="text-[12px] px-2 py-1.5 rounded"
                style={{ color: "#b00020", background: "rgba(176,0,32,0.08)" }}
              >
                {error}
              </div>
            )}
            <textarea
              className="flex-1 min-h-0 w-full rounded-lg border p-3 text-[13px] leading-relaxed resize-none"
              style={{ borderColor: "var(--line)", background: "var(--paper)", color: "var(--ink)" }}
              placeholder="把要批改的文稿粘贴到这里，或点「打开文件」选择 .txt / .docx…"
              value={draft}
              onChange={(e) => {
                const v = e.target.value;
                setDraft(v);
                setDraftFile(null);
                if (selectedChapter === null) setManuscriptText(v);
              }}
            />
            <input
              ref={draftInputRef}
              type="file"
              accept=".txt,.docx"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) onPickDraft(f);
                e.currentTarget.value = "";
              }}
            />
            <input
              ref={sampleInputRef}
              type="file"
              accept=".txt,.docx"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) onCalibrate(f);
                e.currentTarget.value = "";
              }}
            />
          </div>

          {/* 右：报告 */}
          <div className="flex-1 min-h-0 flex flex-col p-3 gap-2 overflow-y-auto">
            {running && (
              <div className="text-[13px] italic" style={{ color: "var(--muted)" }}>
                桌搭正在逐句审读你的文稿…
              </div>
            )}
            {!running && !result && (
              <div className="text-[13px] italic" style={{ color: "var(--faint)" }}>
                批改结果会显示在这里：先是一封整体编辑评语，再是逐条建议。点「采纳」可直接改回你的草稿。
              </div>
            )}
            {result && result.letter && (
              <div
                className="rounded-lg border p-3 text-[13px] leading-relaxed"
                style={{
                  borderColor: "var(--line)",
                  background: "var(--paper)",
                  color: "var(--ink)",
                  whiteSpace: "pre-wrap",
                }}
              >
                <div
                  className="text-[10px] uppercase tracking-wider mb-1"
                  style={{ color: "var(--faint)" }}
                >
                  编辑评语
                </div>
                {result.letter}
              </div>
            )}
            {visibleItems.map((it) => (
              <div
                key={it.id}
                className="rounded-lg border p-3"
                style={{ borderColor: "var(--line)", background: "var(--paper)" }}
              >
                <div className="flex items-center gap-2 mb-1.5">
                  <span
                    className="text-[11px] px-1.5 py-0.5 rounded"
                    style={{ background: TYPE_COLOR[it.type] || "#ccc", color: "#222" }}
                  >
                    {it.type}
                  </span>
                </div>
                <div className="text-[12.5px] mb-1" style={{ color: "var(--ink)" }}>
                  <span style={{ color: "var(--faint)" }}>原文：</span>
                  {it.excerpt}
                </div>
                <div className="text-[12.5px] mb-1" style={{ color: "var(--ink)" }}>
                  <span style={{ color: "var(--accent)" }}>建议：</span>
                  {it.suggestion}
                </div>
                {it.reason && (
                  <div className="text-[12px] mb-2" style={{ color: "var(--muted)" }}>
                    理由：{it.reason}
                  </div>
                )}
                <div className="flex gap-2">
                  <button
                    className="text-[12px] px-2.5 py-1 rounded font-medium"
                    style={{ background: "var(--accent)", color: "#fff" }}
                    onClick={() => onAccept(it)}
                  >
                    采纳
                  </button>
                  <button
                    className="text-[12px] px-2.5 py-1 rounded border"
                    style={{ borderColor: "var(--line)", color: "var(--muted)" }}
                    onClick={() => onReject(it)}
                  >
                    打回
                  </button>
                </div>
              </div>
            ))}
            {applied.length > 0 && (
              <div
                className="rounded-lg border p-3"
                style={{ borderColor: "var(--accent)", background: "var(--paper)" }}
              >
                <div className="flex items-center justify-between mb-2">
                  <span className="text-[12px] font-semibold" style={{ color: "var(--ink)" }}>
                    已采纳的修改（{applied.length}）
                  </span>
                  <button
                    className="text-[11.5px] px-2 py-1 rounded border"
                    style={{ borderColor: "var(--line)", color: "var(--accent)" }}
                    onClick={onExportApplied}
                  >
                    ⬇ 导出清单
                  </button>
                </div>
                <div className="flex flex-col gap-2">
                  {applied.map((it) => (
                    <div
                      key={it.id}
                      className="text-[12px] rounded p-2"
                      style={{
                        background: "var(--panel)",
                        borderLeft: `3px solid ${TYPE_COLOR[it.type] || "#ccc"}`,
                      }}
                    >
                      <div style={{ color: "var(--faint)" }}>原文：{it.excerpt}</div>
                      <div style={{ color: "var(--accent)" }}>建议：{it.suggestion}</div>
                      <button
                        className="text-[11px] mt-1 underline"
                        style={{ color: "var(--muted)" }}
                        onClick={() =>
                          navigator.clipboard?.writeText(it.suggestion).catch(() => {})
                        }
                      >
                        复制建议
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {result && visibleItems.length === 0 && applied.length === 0 && (
              <div className="text-[13px] italic" style={{ color: "var(--faint)" }}>
                这篇文稿没有明显可改的地方，或建议都已处理。👍
              </div>
            )}
          </div>
        </div>
        {toast && (
          <div
            className="absolute left-1/2 -translate-x-1/2 px-3 py-1.5 rounded-full text-[12px] shadow"
            style={{ bottom: 14, background: "var(--ink)", color: "var(--panel)", zIndex: 5 }}
          >
            {toast}
          </div>
        )}
      </div>
    </div>
  );
}
