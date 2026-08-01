/**
 * UnderstandingCard — 桌搭「了解度」轻量卡（重构版）。
 *
 * 与首页 chip 视觉语言统一：单色线性图标 + CSS 变量主题 + 留白 + 圆角。
 * 打开时自取学习历史（了解度统计 + 最近学到的），以精致卡片呈现。
 */

import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Icon } from "../Icon";
import { authedFetch, httpBase } from "../../api";
import type { LearnedHistory } from "./StatusPanel";

/** 相对时间（中文）：刚刚 / X 分钟前 / X 小时前 / 今天 HH:MM / M月D日 */
function relTime(ts: string): string {
  const then = new Date(ts).getTime();
  if (Number.isNaN(then)) return "";
  const diff = Date.now() - then;
  const min = Math.floor(diff / 60000);
  if (min < 1) return "刚刚";
  if (min < 60) return `${min} 分钟前`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} 小时前`;
  const d = new Date(then);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) {
    return `今天 ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  }
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}

export function UnderstandingCard({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  const [data, setData] = useState<LearnedHistory | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    authedFetch(`${httpBase()}/v1/deskmate/learned-history`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (alive && d?.items) setData(d); })
      .catch(() => {/* 静默降级 */})
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, []);

  const stats = data?.stats ?? { total: 0, by_category: {} as Record<string, number> };

  return (
    <div
      className="fixed inset-0 z-[55] flex items-center justify-center"
      style={{ background: "rgba(0,0,0,0.34)" }}
      onClick={onClose}
    >
      <div
        className="rounded-2xl border overflow-hidden"
        style={{
          width: 440,
          maxWidth: "92vw",
          background: "var(--panel)",
          borderColor: "var(--line)",
          boxShadow: "0 24px 64px rgba(0,0,0,0.22)",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div
          className="flex items-center justify-between px-4 py-3"
          style={{ borderBottom: "1px solid var(--line)" }}
        >
          <div className="flex items-center gap-2.5">
            <span
              className="w-7 h-7 rounded-lg flex items-center justify-center"
              style={{ background: "var(--accent-soft)", color: "var(--accent)" }}
            >
              <Icon name="sparkle" size={15} />
            </span>
            <span className="text-[14px] font-semibold" style={{ color: "var(--ink)" }}>
              {t("deskmate.understandingTitle")}
            </span>
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
        <div className="px-4 py-4">
          {/* 大数字 + 分类 */}
          <div className="flex items-end gap-6 mb-4">
            <div>
              <div
                className="text-[40px] leading-none font-semibold"
                style={{ color: "var(--ink)", fontFamily: "var(--serif)" }}
              >
                {stats.total}
              </div>
              <div className="text-[12px] mt-1" style={{ color: "var(--muted)" }}>
                {t("deskmate.totalKnown")}
              </div>
            </div>
            <div className="flex flex-wrap gap-2 pb-1">
              <span
                className="text-[11.5px] px-2.5 py-1 rounded-full"
                style={{ background: "var(--paper)", color: "var(--muted)" }}
              >
                {t("deskmate.preference")} {stats.by_category.preference || 0}
              </span>
              <span
                className="text-[11.5px] px-2.5 py-1 rounded-full"
                style={{ background: "var(--paper)", color: "var(--muted)" }}
              >
                {t("deskmate.habit")} {stats.by_category.habit || 0}
              </span>
              <span
                className="text-[11.5px] px-2.5 py-1 rounded-full"
                style={{ background: "var(--paper)", color: "var(--muted)" }}
              >
                {t("deskmate.tool")} {stats.by_category.tool || 0}
              </span>
            </div>
          </div>

          {/* 最近学到的 */}
          <div
            className="text-[10px] uppercase tracking-wider mb-2"
            style={{ color: "var(--faint)" }}
          >
            {t("deskmate.recentlyLearned")}
          </div>
          {loading ? (
            <div className="text-[12px]" style={{ color: "var(--faint)" }}>
              …
            </div>
          ) : data && data.items.length > 0 ? (
            <div className="flex flex-col gap-1.5 max-h-56 overflow-y-auto pr-1">
              {[...data.items].reverse().map((it, i) => (
                <div
                  key={`${it.ts}-${i}`}
                  className="text-[12.5px] px-3 py-2 rounded-lg"
                  style={{ color: "var(--muted)", background: "var(--paper)", lineHeight: 1.45 }}
                >
                  <span style={{ color: "var(--faint)" }} className="mr-1.5">
                    {relTime(it.ts)}
                  </span>
                  {it.fact}
                </div>
              ))}
            </div>
          ) : (
            <div className="text-[12.5px] italic" style={{ color: "var(--faint)" }}>
              {t("deskmate.emptyLearned")}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
