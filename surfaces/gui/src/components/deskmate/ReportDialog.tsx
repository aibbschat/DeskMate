import { useEffect, useRef, useState } from "react";
import { Icon } from "../Icon";
import {
  listManuscripts,
  listManuscriptChapters,
  loadManuscript,
  type ManuscriptInfo,
  type ManuscriptChapter,
} from "../../api";

type Source = "manuscript" | "text" | "file" | "local";

/**
 * 「生成深度审阅报告」对话框：先选文稿来源（已存书稿 / 粘贴文本 / 上传 / 本地路径），
 * 再调用后端自动分章，用户勾选要审阅的章节后逐章细抠 + 总报告（不必一次性审全本，长书可分多次）。
 *
 * 生成完成后不再用模态预览，而是把「提交文稿」用户消息 + 「报告」助手消息写进当前会话，
 * 报告卡片直接出现在聊天里（workbuddy 专家模式即在对话中完成任务），重开软件后仍在。
 *
 * 本地路径是最便捷的方式：直接填（或浏览）一个本地 .txt/.docx 文件，或一个小说文件夹。
 * 若路径所在文件夹含 analyze_novel 的圣经（.deskmate/），后端会自动并入报告作为一致性准绳。
 */
export function ReportDialog({
  onClose,
  onDone,
}: {
  onClose: () => void;
  onDone: (info: {
    source: string;
    name?: string;
    text?: string;
    path?: string;
    content_base64?: string;
    filename?: string;
    chapter_indices?: number[];
    chapters?: { idx: number; title: string; char_count: number }[];
  }) => void;
}) {
  const [manuscripts, setManuscripts] = useState<ManuscriptInfo[]>([]);
  const [source, setSource] = useState<Source>("manuscript");
  const [selectedName, setSelectedName] = useState("");
  const [pastedText, setPastedText] = useState("");
  const [fileBase64, setFileBase64] = useState("");
  const [fileName, setFileName] = useState("");
  const [localPath, setLocalPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [chapters, setChapters] = useState<{ idx: number; title: string; char_count: number }[]>([]);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [parsed, setParsed] = useState(false);
  const [parsing, setParsing] = useState(false);
  const fileRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    listManuscripts()
      .then((list) => {
        setManuscripts(list);
        if (list.length > 0) setSelectedName(list[0].name);
      })
      .catch(() => setManuscripts([]));
  }, []);

  const onFilePicked = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => {
      const b64 = (reader.result as string).split(",")[1] || "";
      setFileBase64(b64);
      setFileName(f.name);
    };
    reader.readAsDataURL(f);
    e.target.value = "";
  };

  // Tauri 文件/文件夹选择（便捷路径录入）。非 Tauri 环境（浏览器调试）下静默失败，仍可手动填路径。
  const browse = async (directory: boolean) => {
    try {
      const { open } = await import("@tauri-apps/plugin-dialog");
      const picked = await open({
        directory,
        multiple: false,
        title: directory ? "选择小说文件夹" : "选择 .txt / .docx 文件",
      });
      if (typeof picked === "string" && picked) {
        setLocalPath(picked);
        setError(null);
      }
    } catch {
      setError("无法唤起系统选择框，请直接在输入框填写本地路径");
    }
  };

  // 解析文稿章节：调用后端自动分章，收集章节元信息供用户勾选（长书不必一次审全本）。
  const parseChapters = async () => {
    setParsing(true);
    setError(null);
    try {
      let chs: ManuscriptChapter[] = [];
      if (source === "manuscript") {
        if (!selectedName) throw new Error("请选择一本已存书稿");
        chs = await loadManuscript(selectedName);
      } else if (source === "text") {
        if (!pastedText.trim()) throw new Error("请先粘贴文稿文本");
        chs = await listManuscriptChapters({ text: pastedText });
      } else if (source === "file") {
        if (!fileBase64) throw new Error("请先选择 .txt / .docx 文件");
        chs = await listManuscriptChapters({ content_base64: fileBase64, filename: fileName });
      } else {
        if (!localPath.trim()) throw new Error("请先填写本地文件或文件夹路径");
        chs = await listManuscriptChapters({ path: localPath.trim() });
      }
      const meta = chs
        .map((c) => ({ idx: c.idx, title: c.title, char_count: c.char_count ?? c.text?.length ?? 0 }))
        .filter((c) => c.idx > 0);
      if (meta.length === 0) {
        throw new Error("未能从文稿中识别章节（需含「第X章 / 回 / 节」等标题）");
      }
      setChapters(meta);
      setSelected(new Set(meta.map((c) => c.idx)));
      setParsed(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "章节解析失败");
    } finally {
      setParsing(false);
    }
  };

  const toggleChapter = (idx: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx);
      else next.add(idx);
      return next;
    });
  };
  const selectAllChapters = () => setSelected(new Set(chapters.map((c) => c.idx)));
  const clearAllChapters = () => setSelected(new Set());
  const resetParse = () => {
    setParsed(false);
    setChapters([]);
    setSelected(new Set());
  };

  const handleGenerate = async () => {
    setBusy(true);
    setError(null);
    try {
      const info: {
        source: string;
        name?: string;
        text?: string;
        path?: string;
        content_base64?: string;
        filename?: string;
        chapter_indices?: number[];
        chapters?: { idx: number; title: string; char_count: number }[];
      } = { source };
      if (source === "manuscript") {
        if (!selectedName) throw new Error("请选择一本已存书稿");
        info.name = selectedName;
      } else if (source === "text") {
        if (!pastedText.trim()) throw new Error("请粘贴文稿文本");
        info.text = pastedText;
      } else if (source === "file") {
        if (!fileBase64) throw new Error("请选择 .txt / .docx 文件");
        info.content_base64 = fileBase64;
        info.filename = fileName;
      } else {
        if (!localPath.trim()) throw new Error("请填写本地文件或文件夹路径");
        info.path = localPath.trim();
      }
      if (parsed) {
        const idxs = [...selected].sort((a, b) => a - b);
        if (idxs.length === 0) throw new Error("请至少勾选一章");
        info.chapter_indices = idxs;
        info.chapters = chapters;
      }
      // 把文稿信息传给调用方，由 Agent Loop 接管审阅
      onDone(info);
    } catch (e) {
      setError(e instanceof Error ? e.message : "操作失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal report-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="modal-title">
            <Icon name="audit" size={16} /> 生成深度审阅报告
          </span>
          <button className="modal-x" onClick={onClose} aria-label="关闭">
            ✕
          </button>
        </div>

        <div className="report-source">
            <p className="report-source-hint">
              编辑专家将按章节审阅书稿。选好来源后点「解析章节」，会列出全部章节，你只需勾选要审阅的章节（不必一次性审全本，长书可分多次送审）。
            </p>

            <div className="source-tabs">
              <button
                className={"source-tab" + (source === "manuscript" ? " active" : "")}
                onClick={() => setSource("manuscript")}
              >
                已存书稿
              </button>
              <button
                className={"source-tab" + (source === "text" ? " active" : "")}
                onClick={() => setSource("text")}
              >
                粘贴文本
              </button>
              <button
                className={"source-tab" + (source === "file" ? " active" : "")}
                onClick={() => setSource("file")}
              >
                上传文件
              </button>
              <button
                className={"source-tab" + (source === "local" ? " active" : "")}
                onClick={() => setSource("local")}
              >
                本地路径
              </button>
            </div>

            {source === "manuscript" && (
              <div className="source-pane">
                {manuscripts.length === 0 ? (
                  <div className="source-empty">
                    暂无已存书稿。可在「文稿编校」面板中保存书稿，或改用「粘贴文本 / 上传文件 / 本地路径」。
                  </div>
                ) : (
                  <select
                    className="source-select"
                    value={selectedName}
                    onChange={(e) => setSelectedName(e.target.value)}
                  >
                    {manuscripts.map((m) => (
                      <option key={m.name} value={m.name}>
                        {m.name}（{m.chapter_count} 章）
                      </option>
                    ))}
                  </select>
                )}
              </div>
            )}

            {source === "text" && (
              <div className="source-pane">
                <textarea
                  className="source-textarea"
                  placeholder="把小说全文（含「第X章 / 回 / 节」标题）粘贴到这里…"
                  value={pastedText}
                  onChange={(e) => setPastedText(e.target.value)}
                  rows={8}
                />
              </div>
            )}

            {source === "file" && (
              <div className="source-pane">
                <input
                  ref={fileRef}
                  type="file"
                  accept=".txt,.docx"
                  style={{ display: "none" }}
                  onChange={onFilePicked}
                />
                <button className="btn ghost" onClick={() => fileRef.current?.click()}>
                  <Icon name="file" size={14} /> 选择 .txt / .docx
                </button>
                {fileName && <span className="source-filename">{fileName}</span>}
              </div>
            )}

            {source === "local" && (
              <div className="source-pane">
                <div className="local-path-row">
                  <input
                    className="source-input"
                    placeholder="本地文件或文件夹路径，例如 /Users/你/小说/兰陵王 或 …/兰陵王.txt"
                    value={localPath}
                    onChange={(e) => setLocalPath(e.target.value)}
                    spellCheck={false}
                  />
                </div>
                <div className="local-path-actions">
                  <button className="btn ghost sm" onClick={() => browse(false)}>
                    <Icon name="file" size={13} /> 选文件
                  </button>
                  <button className="btn ghost sm" onClick={() => browse(true)}>
                    <Icon name="folder" size={13} /> 选文件夹
                  </button>
                </div>
                <p className="local-path-note">
                  填文件夹（或其下任意文件）时，若该处已跑过「分析小说」，会一并载入人物/大纲/时间线圣经，
                  让逐章问题更贴全书设定；无圣经则自动跳过。
                </p>
              </div>
            )}

            {error && <div className="report-error">{error}</div>}

            {parsed && (
              <div className="chapter-select">
                <div className="chapter-select-head">
                  <span className="chapter-select-title">
                    选择要审阅的章节（共 {chapters.length} 章，已选 {selected.size} 章）
                  </span>
                  <div className="chapter-select-actions">
                    <button
                      type="button"
                      className="btn ghost sm"
                      onClick={selectAllChapters}
                      disabled={parsing}
                    >
                      全选
                    </button>
                    <button
                      type="button"
                      className="btn ghost sm"
                      onClick={clearAllChapters}
                      disabled={parsing}
                    >
                      取消全选
                    </button>
                  </div>
                </div>
                <div className="chapter-list">
                  {chapters.map((c) => (
                    <label
                      key={c.idx}
                      className={"chapter-item" + (selected.has(c.idx) ? " on" : "")}
                    >
                      <input
                        type="checkbox"
                        checked={selected.has(c.idx)}
                        onChange={() => toggleChapter(c.idx)}
                      />
                      <span className="chapter-idx">第 {c.idx} 章</span>
                      <span className="chapter-title">{c.title}</span>
                      <span className="chapter-count">{c.char_count.toLocaleString()} 字</span>
                    </label>
                  ))}
                </div>
              </div>
            )}

            <div className="report-actions">
              {!parsed ? (
                <button
                  className="btn primary"
                  disabled={busy || parsing}
                  onClick={parseChapters}
                >
                  {parsing ? "正在识别章节…" : "解析章节并选择"}
                </button>
              ) : (
                <>
                  <button
                    type="button"
                    className="btn ghost sm"
                    onClick={resetParse}
                    disabled={busy}
                  >
                    重新选择来源
                  </button>
                  <button
                    type="button"
                    className="btn primary"
                    disabled={busy || selected.size === 0}
                    onClick={handleGenerate}
                  >
                    {busy ? "准备中…" : `开始审阅（已选 ${selected.size} 章）`}
                  </button>
                </>
              )}
            </div>
          </div>
      </div>
    </div>
  );
}
