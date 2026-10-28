/**
 * AutoTasks — P1-3 计划任务面板（对标 Spark Skills / Replika「你在忙什么」）。
 *
 * 让小桔的自动任务对用户「可见 + 可掌控」：
 *   - 列出所有已登记的计划任务（名称/分类/描述/累计次数/最近运行）
 *   - 每条带启用/暂停开关（写入 registry.json，并通过 recorder 短路真正生效）
 *   - 展示每条最近 3 次执行摘要（可感知「小桔在自动做什么」）
 */

import { useState, useEffect, useCallback } from "react";
import { Icon } from "../Icon";
import {
  getDeskmateAutoTasks,
  toggleDeskmateAutoTask,
  type AutoTaskItem,
} from "../../api";

const CATEGORY_LABEL: Record<string, { label: string; icon: string }> = {
  writing: { label: "写作", icon: "✍️" },
  self: { label: "自我", icon: "🌱" },
  works: { label: "作品", icon: "📚" },
  other: { label: "其他", icon: "🧩" },
};

export function AutoTasks({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [tasks, setTasks] = useState<AutoTaskItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [toggling, setToggling] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setErr(null);
    getDeskmateAutoTasks(3)
      .then((t) => setTasks(t))
      .catch(() => setErr("加载失败，请稍后重试"))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (open) load();
  }, [open, load]);

  const handleToggle = useCallback(async (t: AutoTaskItem) => {
    setToggling(t.task_type);
    try {
      const next = await toggleDeskmateAutoTask(t.task_type, !t.enabled);
      if (next === null) {
        setErr("切换失败：任务未登记");
      } else {
        setTasks((prev) =>
          prev.map((x) => (x.task_type === t.task_type ? { ...x, enabled: next } : x)),
        );
      }
    } finally {
      setToggling(null);
    }
  }, []);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center animate-in fade-in duration-150"
      style={{ background: "rgba(0,0,0,0.35)" }}
      onClick={onClose}
    >
      <div
        className="rounded-2xl overflow-hidden flex flex-col"
        style={{
          width: "min(560px, 92vw)",
          maxHeight: "84vh",
          background: "white",
          border: "0.5px solid rgba(0,0,0,0.08)",
          boxShadow: "0 12px 48px rgba(0,0,0,0.18)",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* header */}
        <div
          className="flex items-center justify-between px-4 py-3"
          style={{
            borderBottom: "0.5px solid rgba(0,0,0,0.06)",
            background: "linear-gradient(to bottom, rgba(255,255,255,0.8), rgba(255,255,255,0.6))",
          }}
        >
          <div className="flex items-center gap-2">
            <span className="text-sm">📋</span>
            <span className="text-ui font-semibold text-gray-900">计划任务</span>
            <span className="text-[10px] text-gray-400">小桔在自动为你做什么</span>
          </div>
          <button
            className="w-6 h-6 rounded-lg flex items-center justify-center text-gray-400 transition-colors duration-100 hover:text-gray-600 hover:bg-gray-100"
            onClick={onClose}
            aria-label="关闭"
          >
            <Icon name="x" size={14} />
          </button>
        </div>

        {/* body */}
        <div
          className="p-3 overflow-y-auto flex flex-col gap-2.5"
          style={{ maxHeight: "calc(84vh - 52px)" }}
        >
          {loading && <div className="text-label text-gray-400 px-1 py-2">加载中…</div>}
          {!loading && tasks.length === 0 && !err && (
            <div className="text-label text-gray-400 px-1 py-8 text-center">
              暂无已登记的计划任务
            </div>
          )}
          {err && <div className="text-label text-red-500 px-1 py-1">{err}</div>}

          {tasks.map((t) => {
            const cat = CATEGORY_LABEL[t.category] ?? CATEGORY_LABEL.other;
            return (
              <div
                key={t.task_type}
                className="rounded-xl p-3"
                style={{
                  background: "#fafafa",
                  border: "0.5px solid rgba(0,0,0,0.06)",
                }}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5">
                      <span className="text-label">{cat.icon}</span>
                      <span className="text-meta font-semibold text-gray-900 truncate">
                        {t.task_name}
                      </span>
                      <span className="text-[9px] font-medium text-gray-400 bg-gray-100 px-1.5 py-0.5 rounded-full">
                        {cat.label}
                      </span>
                    </div>
                    {t.description && (
                      <div className="text-[10px] text-gray-500 mt-1 leading-relaxed">
                        {t.description}
                      </div>
                    )}
                    <div className="flex items-center gap-3 mt-1.5 text-[10px] text-gray-400">
                      <span>累计 {t.run_count} 次</span>
                      {t.last_run_at && <span>最近 {t.last_run_at}</span>}
                    </div>
                  </div>

                  {/* 启用/暂停开关 */}
                  <button
                    className="relative w-10 h-6 rounded-full transition-colors duration-200 flex-shrink-0"
                    style={{ background: t.enabled ? "#6366f1" : "#e5e7eb" }}
                    onClick={() => handleToggle(t)}
                    disabled={toggling === t.task_type}
                    aria-label={t.enabled ? "暂停" : "启用"}
                  >
                    <div
                      className="absolute top-1 w-4 h-4 rounded-full bg-white shadow-sm transition-transform duration-200"
                      style={{
                        left: t.enabled ? "calc(100% - 1.25rem)" : "0.25rem",
                      }}
                    />
                  </button>
                </div>

                {/* 最近执行摘要 */}
                {t.recent.length > 0 && (
                  <div
                    className="mt-2 pt-2"
                    style={{ borderTop: "0.5px solid rgba(0,0,0,0.05)" }}
                  >
                    <div className="text-[9px] text-gray-400 font-medium mb-1">最近执行</div>
                    {t.recent.map((r, i) => (
                      <div
                        key={`${r.date}-${i}`}
                        className="text-[10px] text-gray-600 leading-relaxed flex gap-1.5"
                      >
                        <span className="text-gray-400 flex-shrink-0">{r.date}</span>
                        <span className="truncate">{r.summary || r.target}</span>
                      </div>
                    ))}
                  </div>
                )}

                {!t.enabled && (
                  <div className="mt-2 text-[10px] text-amber-600">
                    已暂停 · 小桔不再自动执行此任务
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
