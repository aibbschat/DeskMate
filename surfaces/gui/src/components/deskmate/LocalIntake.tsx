import { useState, useEffect, useCallback } from "react";
import { Icon } from "../Icon";
import { chooseFolder } from "../../tauri";
import {
  getIngestDirs,
  saveIngestDirs,
  scanIngestDirs,
  ingestFiles,
  type IngestCandidate,
} from "../../api";

// ---------------------------------------------------------------------------
// P2-3：本地摄入面板
//   用户主动添加本地写作 / 笔记目录 → 扫描预览 → 勾选确认 → 沉淀进小桔的记忆。
//   国子监秘闻录等神圣稿件由后端扫描时强制排除，前端也给出提示。
// ---------------------------------------------------------------------------

export function LocalIntake({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [dirs, setDirs] = useState<string[]>([]);
  const [candidates, setCandidates] = useState<IngestCandidate[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [scanning, setScanning] = useState(false);
  const [ingesting, setIngesting] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      getIngestDirs()
        .then(setDirs)
        .catch(() => {});
      setCandidates([]);
      setSelected(new Set());
      setResult(null);
    }
  }, [open]);

  const addDir = useCallback(async () => {
    const p = await chooseFolder();
    if (p && !dirs.includes(p)) {
      const next = [...dirs, p];
      setDirs(next);
      saveIngestDirs(next)
        .then(setDirs)
        .catch(() => {});
    }
  }, [dirs]);

  const removeDir = useCallback(
    (p: string) => {
      const next = dirs.filter((d) => d !== p);
      setDirs(next);
      saveIngestDirs(next)
        .then(setDirs)
        .catch(() => {});
    },
    [dirs],
  );

  const handleScan = useCallback(async () => {
    if (dirs.length === 0) {
      setResult("请先添加至少一个目录");
      return;
    }
    setScanning(true);
    setResult(null);
    try {
      const c = await scanIngestDirs(dirs);
      setCandidates(c);
      setSelected(new Set(c.map((x) => x.path)));
    } finally {
      setScanning(false);
    }
  }, [dirs]);

  const toggle = (p: string) => {
    setSelected((prev) => {
      const n = new Set(prev);
      if (n.has(p)) n.delete(p);
      else n.add(p);
      return n;
    });
  };

  const handleIngest = useCallback(async () => {
    const paths = candidates.filter((c) => selected.has(c.path)).map((c) => c.path);
    if (paths.length === 0) return;
    setIngesting(true);
    try {
      const r = await ingestFiles(paths);
      const errs = r.errors?.length ?? 0;
      setResult(
        `已沉淀 ${r.ingested} 条、更新 ${r.updated} 条、跳过 ${r.skipped} 条` +
          (errs ? `，${errs} 个出错` : ""),
      );
      setCandidates([]);
      setSelected(new Set());
    } finally {
      setIngesting(false);
    }
  }, [candidates, selected]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center p-4"
      style={{ background: "rgba(0,0,0,0.45)" }}
      onClick={onClose}
    >
      <div
        className="flex flex-col w-full max-w-2xl h-[80vh] max-h-[80vh] rounded-2xl overflow-hidden"
        style={{
          background: "var(--panel)",
          border: "1px solid var(--line)",
          boxShadow: "0 24px 64px rgba(0,0,0,0.3)",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* 顶部栏 */}
        <div
          className="flex items-center justify-between px-4 py-3 flex-shrink-0"
          style={{ borderBottom: "1px solid var(--line)" }}
        >
          <div className="flex items-center gap-2">
            <span style={{ color: "var(--muted)" }}>
              <Icon name="file" size={16} className="opacity-60" />
            </span>
            <span className="text-[14px] font-semibold" style={{ color: "var(--ink)" }}>
              本地摄入
            </span>
          </div>
          <button
            className="w-7 h-7 rounded-lg flex items-center justify-center"
            style={{ color: "var(--faint)" }}
            onClick={onClose}
            aria-label="关闭"
          >
            <Icon name="x" size={16} />
          </button>
        </div>

        {/* 内容区 */}
        <div className="flex-1 min-h-0 overflow-y-auto px-4 py-3 flex flex-col gap-3">
          {/* 摄入目录配置 */}
          <div
            className="rounded-xl p-3"
            style={{ border: "1px solid var(--line)", background: "var(--paper)" }}
          >
            <div className="text-[12px] font-medium mb-2" style={{ color: "var(--ink)" }}>
              摄入目录
            </div>
            <div className="flex flex-col gap-1.5">
              {dirs.length === 0 && (
                <div className="text-[11.5px]" style={{ color: "var(--faint)" }}>
                  尚未添加目录（点击下方「添加目录」选择你存放写作 / 笔记的文件夹）
                </div>
              )}
              {dirs.map((d) => (
                <div
                  key={d}
                  className="flex items-center gap-2 text-[11.5px]"
                  style={{ color: "var(--muted)" }}
                >
                  <span className="flex-1 truncate" style={{ fontFamily: "var(--mono)" }}>
                    {d}
                  </span>
                  <button
                    onClick={() => removeDir(d)}
                    className="px-1.5 py-0.5 rounded"
                    style={{ color: "var(--accent)" }}
                  >
                    移除
                  </button>
                </div>
              ))}
            </div>
            <button
              onClick={() => void addDir()}
              className="mt-2 text-[12px] flex items-center gap-1 px-2.5 py-1 rounded-lg"
              style={{ background: "var(--accent-soft)", color: "var(--accent)" }}
            >
              <Icon name="plus" size={12} /> 添加目录
            </button>
          </div>

          {/* 扫描按钮 */}
          <button
            onClick={() => void handleScan()}
            disabled={scanning || dirs.length === 0}
            className="text-[12px] px-3 py-2 rounded-lg self-start"
            style={{
              background: "var(--accent)",
              color: "#fff",
              opacity: scanning || dirs.length === 0 ? 0.5 : 1,
            }}
          >
            {scanning ? "扫描中…" : "扫描本地笔记"}
          </button>

          {/* 神圣稿件提示 */}
          <div className="text-[11px]" style={{ color: "var(--faint)" }}>
            国子监秘闻录等神圣稿件会被自动排除，绝不读取。
          </div>

          {/* 候选列表 */}
          {candidates.length > 0 && (
            <div className="flex flex-col gap-2">
              <div className="flex items-center justify-between">
                <span className="text-[12px]" style={{ color: "var(--muted)" }}>
                  发现 {candidates.length} 份笔记（勾选要沉淀的）
                </span>
                <button
                  onClick={() => setSelected(new Set(candidates.map((c) => c.path)))}
                  className="text-[11px]"
                  style={{ color: "var(--accent)" }}
                >
                  全选
                </button>
              </div>
              {candidates.map((c) => (
                <label
                  key={c.path}
                  className="flex items-start gap-2 rounded-lg p-2"
                  style={{ border: "1px solid var(--line)", background: "var(--paper)" }}
                >
                  <input
                    type="checkbox"
                    checked={selected.has(c.path)}
                    onChange={() => toggle(c.path)}
                    className="mt-1"
                  />
                  <div className="flex-1 min-w-0">
                    <div className="text-[12px] font-medium" style={{ color: "var(--ink)" }}>
                      {c.name}
                    </div>
                    <div
                      className="text-[11px] mt-0.5 truncate"
                      style={{ color: "var(--faint)", fontFamily: "var(--mono)" }}
                    >
                      {c.path}
                    </div>
                    <div
                      className="text-[11.5px] mt-1 leading-snug"
                      style={{ color: "var(--muted)" }}
                    >
                      {c.excerpt}
                    </div>
                  </div>
                </label>
              ))}
            </div>
          )}

          {result && (
            <div className="text-[12px]" style={{ color: "var(--accent)" }}>
              {result}
            </div>
          )}
        </div>

        {/* 底部操作 */}
        {candidates.length > 0 && (
          <div
            className="flex items-center justify-between px-4 py-3 flex-shrink-0"
            style={{ borderTop: "1px solid var(--line)" }}
          >
            <span className="text-[11.5px]" style={{ color: "var(--faint)" }}>
              已选 {selected.size} 份
            </span>
            <button
              onClick={() => void handleIngest()}
              disabled={ingesting || selected.size === 0}
              className="text-[12px] px-3 py-2 rounded-lg"
              style={{
                background: "var(--accent)",
                color: "#fff",
                opacity: ingesting || selected.size === 0 ? 0.5 : 1,
              }}
            >
              {ingesting ? "沉淀中…" : "沉淀到记忆"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
