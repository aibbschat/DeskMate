/**
 * DiaryPanel — 小桔日记（P1-3 桌宠日记产品化的前端可视化）。
 *
 * 两个视图：
 * - 今日：左侧当月日历（有日记的日期实心、今天高亮、可翻月），右侧第一人称日记全文
 * - 我们的回忆：7天/30天叙事化汇总
 *
 * 设计原则（8.0 红线「懂你 = 可感可看」）：
 * - 暖米白 + 暖橙配色，衬线字体，营造「翻日记本」的温度感
 * - 第一人称（我=小桔，你=用户），不堆砌数据
 * - 诚实：无日记显示空态引导，不编造；API 失败静默降级
 * - 后端 API：GET /v1/deskmate/diary、/diary/{date}、/diary/recap、POST /diary/today
 */

import { useEffect, useState } from "react";
import { authedFetch, httpBase } from "../../api";
import { Icon } from "../Icon";

type View = "today" | "recap";

/** 今日灵感进度：素材攒到什么程度了，够不够小桔动笔写一篇。 */
type DiaryInspiration = {
  date: string;
  score: number;
  threshold: number;
  detail: Record<string, number>;
  should_write: boolean;
  is_refresh: boolean;
  materials_count: number;
  reason: string;
};

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

function isoOf(y: number, m: number, d: number): string {
  return `${y}-${pad2(m + 1)}-${pad2(d)}`;
}

function todayISO(): string {
  const n = new Date();
  return isoOf(n.getFullYear(), n.getMonth(), n.getDate());
}

function monthDayCount(year: number, month: number): number {
  return new Date(year, month + 1, 0).getDate();
}

/** 极简 markdown 渲染：# 标题 / - 列表 / > 引用 / 段落。日记内容结构简单，不引第三方库。 */
function renderMarkdown(text: string): React.ReactNode {
  const lines = text.split("\n");
  const blocks: React.ReactNode[] = [];
  let listItems: string[] = [];
  const flushList = () => {
    if (listItems.length > 0) {
      blocks.push(
        <ul key={`ul-${blocks.length}`}>
          {listItems.map((it, i) => (
            <li key={i}>{it}</li>
          ))}
        </ul>,
      );
      listItems = [];
    }
  };
  lines.forEach((line, i) => {
    if (line.startsWith("# ")) {
      flushList();
      blocks.push(<h3 key={`h-${i}`}>{line.slice(2)}</h3>);
    } else if (line.startsWith("- ")) {
      listItems.push(line.slice(2));
    } else if (line.startsWith("> ")) {
      flushList();
      blocks.push(<blockquote key={`q-${i}`}>{line.slice(2)}</blockquote>);
    } else if (line.trim() === "") {
      flushList();
    } else {
      flushList();
      blocks.push(<p key={`p-${i}`}>{line}</p>);
    }
  });
  flushList();
  return <>{blocks}</>;
}

export function DiaryPanel() {
  const [view, setView] = useState<View>("today");
  const [dates, setDates] = useState<string[]>([]);
  const [selected, setSelected] = useState<string>(todayISO());
  const [content, setContent] = useState<string | null>(null);
  const [recap, setRecap] = useState<string>("");
  const [recapDays, setRecapDays] = useState<number>(7);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [inspiration, setInspiration] = useState<DiaryInspiration | null>(null);
  const [viewMonth, setViewMonth] = useState<Date>(() => {
    const n = new Date();
    return new Date(n.getFullYear(), n.getMonth(), 1);
  });

  const fetchDates = async () => {
    try {
      const r = await authedFetch(`${httpBase()}/v1/deskmate/diary`);
      if (r.ok) {
        const d = await r.json();
        setDates(Array.isArray(d.dates) ? d.dates : []);
      }
    } catch {
      /* 静默降级 */
    }
  };

  const fetchContent = async (date: string) => {
    setLoading(true);
    try {
      const r = await authedFetch(`${httpBase()}/v1/deskmate/diary/${date}`);
      if (r.ok) {
        const d = await r.json();
        setContent(d.content || null);
      } else {
        setContent(null);
      }
    } catch {
      setContent(null);
    } finally {
      setLoading(false);
    }
  };

  const fetchRecap = async (days: number) => {
    try {
      const r = await authedFetch(`${httpBase()}/v1/deskmate/diary/recap?days=${days}`);
      if (r.ok) {
        const d = await r.json();
        setRecap(d.recap || "");
      }
    } catch {
      /* 静默降级 */
    }
  };

  /**
   * 今日灵感进度。日记不是定时任务——素材攒够才动笔，没攒够就安静等着，
   * 这里的进度条是让"攒素材"这件事本身变得可见（萧哥 2026-09-02 定调）。
   */
  const fetchInspiration = async () => {
    try {
      const r = await authedFetch(`${httpBase()}/v1/deskmate/diary/inspiration`);
      if (r.ok) setInspiration(await r.json());
    } catch {
      /* 静默降级：拿不到就不显示进度 */
    }
  };

  const generateToday = async () => {
    setGenerating(true);
    try {
      const r = await authedFetch(`${httpBase()}/v1/deskmate/diary/today`, { method: "POST" });
      if (r.ok) {
        const d = await r.json();
        setContent(d.content || null);
        await fetchDates();
        await fetchInspiration();
        setSelected(todayISO());
      }
    } catch {
      /* 静默降级 */
    } finally {
      setGenerating(false);
    }
  };

  useEffect(() => {
    fetchDates();
    fetchInspiration();
  }, []);

  useEffect(() => {
    if (view === "today") fetchContent(selected);
  }, [selected, view]);

  useEffect(() => {
    if (view === "recap") fetchRecap(recapDays);
  }, [recapDays, view]);

  const year = viewMonth.getFullYear();
  const month = viewMonth.getMonth();
  const today = todayISO();
  const selectedIndex = dates.indexOf(selected);

  const prevMonth = () => setViewMonth(new Date(year, month - 1, 1));
  const nextMonth = () => setViewMonth(new Date(year, month + 1, 1));

  return (
    <div className="dm-diary-panel">
      {/* 顶部：标题 + tab */}
      <div className="dm-diary-header">
        <span className="dm-diary-title">
          <Icon name="book" size={16} />
          小桔的日记
        </span>
        <div className="dm-diary-tabs">
          <button
            type="button"
            className={view === "today" ? "active" : ""}
            onClick={() => setView("today")}
          >
            今日
          </button>
          <button
            type="button"
            className={view === "recap" ? "active" : ""}
            onClick={() => setView("recap")}
          >
            我们的回忆
          </button>
        </div>
      </div>

      {view === "today" ? (
        <div className="dm-diary-body">
          {/* 左侧日历 */}
          <div className="dm-diary-calendar">
            <div className="dm-diary-cal-header">
              <button type="button" onClick={prevMonth} aria-label="上个月">
                ◀
              </button>
              <span>
                {year}年{month + 1}月
              </span>
              <button type="button" onClick={nextMonth} aria-label="下个月">
                ▶
              </button>
            </div>
            <div className="dm-diary-cal-grid">
              {Array.from({ length: monthDayCount(year, month) }, (_, i) => i + 1).map((d) => {
                const iso = isoOf(year, month, d);
                const has = dates.includes(iso);
                const isToday = iso === today;
                const isSel = iso === selected;
                const cls = [
                  "dm-diary-cal-day",
                  has ? "has" : "",
                  isToday ? "today" : "",
                  isSel ? "selected" : "",
                ]
                  .filter(Boolean)
                  .join(" ");
                return (
                  <button
                    key={iso}
                    type="button"
                    className={cls}
                    onClick={() => setSelected(iso)}
                    title={has ? `${iso} 有日记` : iso}
                  >
                    {d}
                  </button>
                );
              })}
            </div>

            {/* 今日灵感进度：让「攒素材」这件事本身可见——
                日记不是定时任务，攒够才动笔，没攒够就安静等着。 */}
            {inspiration && (
              <div className="dm-diary-inspiration" title={inspiration.reason}>
                <div className="dm-diary-insp-head">
                  <span className="dm-diary-insp-label">今日灵感</span>
                  <span className="dm-diary-insp-score">
                    {inspiration.score.toFixed(1)} / {inspiration.threshold}
                  </span>
                </div>
                <div className="dm-diary-insp-track">
                  <div
                    className={
                      "dm-diary-insp-fill" +
                      (inspiration.score >= inspiration.threshold ? " ready" : "")
                    }
                    style={{
                      width: `${Math.min(
                        100,
                        (inspiration.score / Math.max(0.1, inspiration.threshold)) * 100,
                      )}%`,
                    }}
                  />
                </div>
                <p className="dm-diary-insp-hint">
                  {inspiration.is_refresh
                    ? "今天已经记下一笔了"
                    : "还在攒素材，攒够了我才写"}
                </p>
              </div>
            )}
          </div>

          {/* 右侧正文 */}
          <div className="dm-diary-content">
            {loading ? (
              <p className="dm-diary-loading">翻开日记中…</p>
            ) : content ? (
              renderMarkdown(content)
            ) : (
              <div className="dm-diary-empty">
                <p>这一天还没有日记。</p>
                <p className="dm-diary-empty-hint">多和我聊两句，回忆就会长出来。</p>
                <button type="button" className="dm-diary-generate" onClick={generateToday} disabled={generating}>
                  {generating ? "写日记中…" : "✎ 生成今日日记"}
                </button>
              </div>
            )}
          </div>
        </div>
      ) : (
        /* 我们的回忆视图 */
        <div className="dm-diary-recap">
          <div className="dm-diary-recap-header">
            <span className="dm-diary-recap-title">最近 {recapDays} 天</span>
            <div className="dm-diary-recap-days">
              <button
                type="button"
                className={recapDays === 7 ? "active" : ""}
                onClick={() => setRecapDays(7)}
              >
                7天
              </button>
              <button
                type="button"
                className={recapDays === 30 ? "active" : ""}
                onClick={() => setRecapDays(30)}
              >
                30天
              </button>
            </div>
          </div>
          <div className="dm-diary-recap-body">
            {recap ? renderMarkdown(recap) : <p className="dm-diary-loading">回忆整理中…</p>}
          </div>
        </div>
      )}

      {/* 底部：生成按钮 + 分页 */}
      {view === "today" && (
        <div className="dm-diary-footer">
          <button
            type="button"
            className="dm-diary-generate"
            onClick={generateToday}
            disabled={generating || !!content}
          >
            {generating ? "写日记中…" : content ? "今日已写" : "✎ 生成今日日记"}
          </button>
          <span className="dm-diary-pager">
            {selectedIndex >= 0 ? `第 ${selectedIndex + 1} / ${dates.length} 篇` : `共 ${dates.length} 篇`}
          </span>
        </div>
      )}
    </div>
  );
}
