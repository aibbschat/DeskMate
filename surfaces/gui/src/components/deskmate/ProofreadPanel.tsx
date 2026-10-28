import { useEffect, useRef, useState } from "react";
import {
  calibrateWritingStyle,
  getWritingStyle,
  listManuscriptChapters,
  listManuscripts,
  loadManuscript,
  proofreadDraft,
  saveManuscript,
  writingAdvisor,
  type ManuscriptChapter,
  type ManuscriptInfo,
  type StyleProfile,
} from "../../api";
import { Icon } from "../Icon";

interface Props {
  open: boolean;
  onClose: () => void;
  /** 当前会话 id：校对结果作为结构化消息写入该会话（聊天呈现，重启不丢）。 */
  sessionId?: string;
  /** 首页 chip 进入焦点：edit 常规 / manuscripts 书稿库（自动载入最近一本）/ style 风格库（未校准则唤起校准）。 */
  initialFocus?: "edit" | "manuscripts" | "style";
}

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

export function ProofreadPanel({ open, onClose, sessionId, initialFocus }: Props) {
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
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const toastTimer = useRef<number | null>(null);
  const draftInputRef = useRef<HTMLInputElement>(null);
  const sampleInputRef = useRef<HTMLInputElement>(null);

  // —— P1-5 写作顾问模式：与「批改」共用左栏草稿，右侧改为展示小桔的世界观视角 ——
  const [mode, setMode] = useState<"proofread" | "advisor">("proofread");
  const [adviceKind, setAdviceKind] = useState<"plot" | "framework" | "review" | "general">("plot");
  const [adviceText, setAdviceText] = useState<string | null>(null);
  const [adviceLoading, setAdviceLoading] = useState(false);
  const [adviceError, setAdviceError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setError(null);
    getWritingStyle()
      .then((d) => setProfile(d.profile))
      .catch(() => {/* 静默 */});
    listManuscripts().then(setSavedManuscripts).catch(() => {});
  }, [open]);

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
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const onProofread = async () => {
    setError(null);
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
      if (sessionId) opts.sessionId = sessionId;
      if (manuscriptName) opts.title = manuscriptName;
      await proofreadDraft(opts);
      // 编校可能解锁勋章：通知桌宠自检并报喜（兜底仍有 20s 轮询）
      window.dispatchEvent(new CustomEvent("deskmate:badges-refresh"));
      // 结果已由后端作为结构化消息写入当前会话聊天，关闭面板让用户在聊天中查看
      showToast("编校完成，结果已发到聊天 ✅");
      window.setTimeout(() => onClose(), 1400);
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

  const onAdvisor = async () => {
    setAdviceError(null);
    if (!draft.trim() || draft.startsWith("（已载入")) {
      setAdviceError("请先粘贴或打开要拿给她看的文稿（情节 / 框架 / 书评）");
      return;
    }
    setAdviceLoading(true);
    setAdviceText(null);
    emitEmotion("thinking");
    try {
      const res = await writingAdvisor(draft, adviceKind);
      if (!res) {
        setAdviceError("小桔暂时没法给看法（接口无响应），稍后再试");
      } else if (!res.ok) {
        // 后端优雅降级：no_worldview / no_provider / error —— 把她的真实状态讲清楚
        setAdviceText(res.advice || "小桔暂时给不出看法");
      } else {
        setAdviceText(res.advice);
      }
    } catch (e) {
      setAdviceError((e as Error).message);
    } finally {
      setAdviceLoading(false);
      emitEmotion("happy");
    }
  };

  const switchMode = (next: "proofread" | "advisor") => {
    if (next === mode) return;
    setMode(next);
    setAdviceText(null);
    setAdviceError(null);
  };

  const exportName = () => {
    if (selectedChapter !== null) {
      const ch = chapters.find((c) => c.idx === selectedChapter);
      if (ch) return ch.title;
    }
    return draftFile?.filename?.replace(/\.(docx|txt)$/i, "") || "manuscript";
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

  const scopeLabel =
    selectedChapter !== null
      ? `${mode === "advisor" ? "顾问" : "审"}：第 ${selectedChapter} 章`
      : `${mode === "advisor" ? "顾问" : "审"}：整篇 / 全部`;
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
            <span className="text-body font-semibold" style={{ color: "var(--ink)" }}>
              文稿编校
            </span>
            {manuscriptName && (
              <span
                className="inline-flex items-center gap-1 text-label px-2 py-0.5 rounded"
                style={{ color: "var(--accent)", background: "var(--paper)" }}
              >
                <Icon name="archive" size={12} />
                {manuscriptName}
              </span>
            )}
            {profile?.summary && (
              <span
                className="inline-flex items-center gap-1 text-label px-2 py-0.5 rounded"
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
              {/* P1-5 模式切换：批改 / 写作顾问 */}
              <div className="inline-flex rounded-lg border" style={{ borderColor: "var(--line)" }}>
                {(["proofread", "advisor"] as const).map((m) => (
                  <button
                    key={m}
                    className="text-meta px-3 py-1.5 rounded-lg"
                    style={{
                      color: mode === m ? "#fff" : "var(--muted)",
                      background: mode === m ? "var(--accent)" : "transparent",
                      fontWeight: mode === m ? 600 : 400,
                    }}
                    onClick={() => switchMode(m)}
                  >
                    {m === "proofread" ? "批改" : "写作顾问"}
                  </button>
                ))}
              </div>
              <button
                className="inline-flex items-center gap-1.5 text-meta px-2.5 py-1.5 rounded border"
                style={{ borderColor: "var(--line)", color: "var(--ink)" }}
                onClick={() => draftInputRef.current?.click()}
              >
                <Icon name="folder" size={13} />
                打开文件
              </button>
              {mode === "proofread" && (
                <button
                  className="inline-flex items-center gap-1.5 text-meta px-2.5 py-1.5 rounded border"
                  style={{ borderColor: "var(--line)", color: "var(--ink)" }}
                  onClick={() => sampleInputRef.current?.click()}
                >
                  <Icon name="refresh" size={13} />
                  校准我的风格
                </button>
              )}
              {savedManuscripts.length > 0 && (
                <select
                  className="text-meta px-2 py-1.5 rounded border"
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
                  className="text-meta px-2 py-1.5 rounded border"
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
              {mode === "proofread" ? (
                <button
                  className="text-meta px-3 py-1.5 rounded font-medium"
                  style={{ background: "var(--accent)", color: "#fff" }}
                  onClick={onProofread}
                  disabled={running}
                >
                  {running ? "批改中…" : "开始批改"}
                </button>
              ) : (
                <>
                  <select
                    className="text-meta px-2 py-1.5 rounded border"
                    style={{ borderColor: "var(--line)", color: "var(--ink)", background: "var(--paper)" }}
                    value={adviceKind}
                    onChange={(e) => setAdviceKind(e.target.value as typeof adviceKind)}
                    title="你要拿给她看的是什么"
                  >
                    <option value="plot">情节</option>
                    <option value="framework">框架</option>
                    <option value="review">书评</option>
                    <option value="general">想法</option>
                  </select>
                  <button
                    className="text-meta px-3 py-1.5 rounded font-medium"
                    style={{ background: "var(--accent)", color: "#fff" }}
                    onClick={onAdvisor}
                    disabled={adviceLoading}
                  >
                    {adviceLoading ? "她在读…" : "让小桔看看"}
                  </button>
                </>
              )}
              <button
                className="inline-flex items-center gap-1.5 text-meta px-2.5 py-1.5 rounded border"
                style={{ borderColor: "var(--line)", color: "var(--muted)" }}
                onClick={onExportChapter}
                title="导出当前草稿（或当前章节）为 .txt"
              >
                <Icon name="file" size={13} />
                导出本章
              </button>
              {profileSource && (
                <span className="text-label" style={{ color: "var(--muted)" }}>
                  风格样本：{profileSource}
                </span>
              )}
            </div>
            <div className="text-label" style={{ color: "var(--faint)" }}>
              {scopeLabel}
              {draftFile && chapters.length > 1 && " · Word 文档已按章拆分，选章后可逐章审"}
            </div>
            {error && (
              <div
                className="text-meta px-2 py-1.5 rounded"
                style={{ color: "#b00020", background: "rgba(176,0,32,0.08)" }}
              >
                {error}
              </div>
            )}
            <textarea
              className="flex-1 min-h-0 w-full rounded-lg border p-3 text-ui leading-relaxed resize-none"
              style={{ borderColor: "var(--line)", background: "var(--paper)", color: "var(--ink)" }}
              placeholder={mode === "advisor" ? "把要拿给小桔看的情节 / 框架 / 书评粘贴到这里，或点「打开文件」选择 .txt / .docx…" : "把要批改的文稿粘贴到这里，或点「打开文件」选择 .txt / .docx…"}
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

          {/* 右：说明 / 写作顾问视角卡 */}
          <div className="flex-1 min-h-0 flex flex-col p-3 gap-2 overflow-y-auto">
            {mode === "proofread" ? (
              <>
                {running && (
                  <div className="text-ui italic" style={{ color: "var(--muted)" }}>
                    小桔正在逐句审读你的文稿… 完成后结果会发到聊天。
                  </div>
                )}
                {!running && (
                  <div className="text-ui italic leading-relaxed" style={{ color: "var(--faint)" }}>
                    校对完成后，结果会以结构化消息发到聊天（【原文】/【建议】/【理由】按章分组）。
                    你可以在聊天里直接阅读，回到原稿手动修改你认可的条目。
                  </div>
                )}
              </>
            ) : (
              <>
                {adviceLoading && (
                  <div className="text-ui italic" style={{ color: "var(--muted)" }}>
                    小桔正在用她自己的镜头读你的文字… 她不是审字，是聊看法。
                  </div>
                )}
                {!adviceLoading && !adviceText && !adviceError && (
                  <div className="text-ui italic leading-relaxed" style={{ color: "var(--faint)" }}>
                    这是「写作顾问」——不是编辑、不是代笔，是小桔用她看世界长出来的世界观，
                    读你的情节 / 框架 / 书评，给你她的看法。粘贴好文稿、选好类型，点「让小桔看看」。
                    <br /><br />
                    她只有真的形成了自己的看法才会开口；还在长的时候，她会老实说「我还没想透」。
                  </div>
                )}
                {adviceError && (
                  <div
                    className="text-meta px-2 py-1.5 rounded"
                    style={{ color: "#b00020", background: "rgba(176,0,32,0.08)" }}
                  >
                    {adviceError}
                  </div>
                )}
                {adviceText && (
                  <div
                    className="rounded-xl border p-3 text-ui leading-relaxed"
                    style={{ borderColor: "var(--line)", background: "var(--paper)", color: "var(--ink)", whiteSpace: "pre-wrap" }}
                  >
                    <div className="flex items-center gap-1.5 mb-1.5" style={{ color: "var(--accent)" }}>
                      <Icon name="sparkle" size={13} />
                      小桔的视角（{adviceKind === "review" ? "书评" : adviceKind === "framework" ? "框架" : adviceKind === "plot" ? "情节" : "想法"}）
                    </div>
                    {adviceText}
                  </div>
                )}
              </>
            )}
          </div>
        </div>
        {toast && (
          <div
            className="absolute left-1/2 -translate-x-1/2 px-3 py-1.5 rounded-full text-meta shadow"
            style={{ bottom: 14, background: "var(--ink)", color: "var(--panel)", zIndex: 5 }}
          >
            {toast}
          </div>
        )}
      </div>
    </div>
  );
}
