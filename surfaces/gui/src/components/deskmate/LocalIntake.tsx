import { useState, useEffect, useCallback } from "react";
import { Icon } from "../Icon";
import { chooseFolder } from "../../tauri";
import {
  getIngestDirs,
  saveIngestDirs,
  scanPrivateDirs,
  getPrivateCategories,
  ingestPrivate,
  cleanPrivate,
  type PrivateCandidate,
} from "../../api";

// ---------------------------------------------------------------------------
// 本地资料摄入面板（私人通道，P2-3 演化）
//   用户主动添加本地目录 → 轻量扫描（只列文件，不读内容）→ 选分类 → 勾选确认
//   → 灌库到 private-<分类名>.db（复用史料 FTS 引擎，逻辑隔离）。
//   国子监秘闻录等神圣稿件由后端扫描时强制排除，前端也给出提示。
//   设计原则：分类=库（中文分类名直接做 .db 文件名），随时加分类零代码改动；
//   限定分类是可选参数、默认跨全部，不增加查询步骤。
// ---------------------------------------------------------------------------

function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function LocalIntake({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [dirs, setDirs] = useState<string[]>([]);
  const [categories, setCategories] = useState<string[]>([]);
  const [category, setCategory] = useState<string>("");
  const [candidates, setCandidates] = useState<PrivateCandidate[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [scanning, setScanning] = useState(false);
  const [ingesting, setIngesting] = useState(false);
  const [cleaning, setCleaning] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      getIngestDirs().then(setDirs).catch(() => {});
      getPrivateCategories().then(setCategories).catch(() => {});
      setCategory("");
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
      saveIngestDirs(next).then(setDirs).catch(() => {});
    }
  }, [dirs]);

  const removeDir = useCallback(
    (p: string) => {
      const next = dirs.filter((d) => d !== p);
      setDirs(next);
      saveIngestDirs(next).then(setDirs).catch(() => {});
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
      const c = await scanPrivateDirs(dirs);
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
    const cat = category.trim();
    if (!cat) {
      setResult("请先填写或选择一个分类（分类名即资料库名）");
      return;
    }
    const paths = candidates.filter((c) => selected.has(c.path)).map((c) => c.path);
    if (paths.length === 0) return;
    setIngesting(true);
    try {
      const r = await ingestPrivate(paths, cat);
      const errs = r.errors?.length ?? 0;
      setResult(
        `已入库 ${r.ingested} 份、更新 ${r.updated} 份、跳过 ${r.skipped} 份` +
          (errs ? `，${errs} 个出错` : ""),
      );
      // 同步最新分类列表（可能新建了分类）
      getPrivateCategories().then(setCategories).catch(() => {});
      setCandidates([]);
      setSelected(new Set());
    } finally {
      setIngesting(false);
    }
  }, [candidates, selected, category]);

  const handleClean = useCallback(async () => {
    const cat = category.trim();
    if (!cat) {
      setResult("请先选择要清理的分类");
      return;
    }
    setCleaning(true);
    try {
      const removed = await cleanPrivate(cat);
      setResult(`已清理 ${removed} 份源文件已删除的资料`);
    } finally {
      setCleaning(false);
    }
  }, [category]);

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
              本地资料库
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
          <div className="text-[11.5px]" style={{ color: "var(--faint)" }}>
            把你主动指定目录里的写作教程 / 随笔 / 非史料书籍摄入，按分类归档。
            小桔会把它当「私人资料」检索引用（不当史料）。分类即资料库，随时新建零代码。
          </div>

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
                  尚未添加目录（点击下方「添加目录」选择你存放资料的文件夹）
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

          {/* 分类选择 */}
          <div
            className="rounded-xl p-3"
            style={{ border: "1px solid var(--line)", background: "var(--paper)" }}
          >
            <div className="text-[12px] font-medium mb-2" style={{ color: "var(--ink)" }}>
              归档分类
            </div>
            <input
              list="private-categories"
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              placeholder="输入或选择分类名（如：写作教程 / 随笔 / 非史料书）"
              className="w-full px-2.5 py-1.5 rounded-lg text-[12px] outline-none"
              style={{
                background: "var(--panel)",
                border: "1px solid var(--line)",
                color: "var(--ink)",
                fontFamily: "var(--mono)",
              }}
            />
            <datalist id="private-categories">
              {categories.map((c) => (
                <option key={c} value={c} />
              ))}
            </datalist>
            <div className="text-[11px] mt-1.5" style={{ color: "var(--faint)" }}>
              已有分类：{categories.length ? categories.join("、") : "（暂无，填写即新建）"}
            </div>
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
            {scanning ? "扫描中…" : "扫描本地资料"}
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
                  发现 {candidates.length} 份文件（勾选要摄入的）
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
                    <div className="flex items-center gap-1.5">
                      <div className="text-[12px] font-medium" style={{ color: "var(--ink)" }}>
                        {c.name}
                      </div>
                      <span
                        className="text-[10px] px-1 rounded"
                        style={{
                          background: "var(--accent-soft)",
                          color: "var(--accent)",
                          fontFamily: "var(--mono)",
                        }}
                      >
                        {c.ext}
                      </span>
                      <span className="text-[10px]" style={{ color: "var(--faint)" }}>
                        {fmtSize(c.size)}
                      </span>
                    </div>
                    <div
                      className="text-[11px] mt-0.5 truncate"
                      style={{ color: "var(--faint)", fontFamily: "var(--mono)" }}
                    >
                      {c.path}
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
            <button
              onClick={() => void handleClean()}
              disabled={cleaning || !category.trim()}
              className="text-[11.5px] px-2.5 py-2 rounded-lg"
              style={{
                color: "var(--muted)",
                border: "1px solid var(--line)",
                opacity: cleaning || !category.trim() ? 0.5 : 1,
              }}
              title="清理该分类下源文件已删除的资料"
            >
              {cleaning ? "清理中…" : "清理已删源"}
            </button>
            <div className="flex items-center gap-3">
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
                {ingesting ? "入库中…" : "摄入到资料库"}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
