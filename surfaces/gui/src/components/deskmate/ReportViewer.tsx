import { useState } from "react";
import { Icon } from "../Icon";
import { downloadReportDocx, type ReviewReport } from "../../api";

// 防御式读取：报告可能来自不同来源（嵌套对象 / JSON 字符串 / 字段缺失），
// 任一缺失都不应让这张卡片抛错——否则 ErrorBoundary 会用整屏红屏替换整个 App。
const asObj = (x: any): Record<string, any> =>
  x && typeof x === "object" && !Array.isArray(x) ? x : {};
const asArr = (x: any): any[] => (Array.isArray(x) ? x : []);

/** 把结构化审阅报告渲染为可交互预览（总评分条形 + 逐章可折叠 + 问题清单 + 下载 Word）。
 *  可独立使用：作为聊天中的报告卡片（来自会话消息）时，name 即报告存储名。 */
export function ReportViewer({
  report,
  name,
}: {
  report: ReviewReport;
  name: string;
}) {
  // 归一化报告结构：兼容字符串 / 对象 / 字段缺失。
  const r =
    typeof report === "string"
      ? (() => {
          try {
            return JSON.parse(report);
          } catch {
            return {};
          }
        })()
      : report && typeof report === "object"
        ? report
        : {};
  const o = asObj(r.overall);
  const meta = asObj(r.meta);
  const chapters = asArr(r.per_chapter);
  const scores = asObj(o.scores);
  const maxScore = 10;

  const [open, setOpen] = useState<Set<number>>(new Set());
  const [allOpen, setAllOpen] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [dlError, setDlError] = useState<string | null>(null);

  const toggle = (idx: number) => {
    setOpen((cur) => {
      const next = new Set(cur);
      if (next.has(idx)) next.delete(idx);
      else next.add(idx);
      return next;
    });
  };

  const toggleAll = () => {
    if (allOpen) setOpen(new Set());
    else setOpen(new Set(chapters.map((c) => asObj(c).idx)));
    setAllOpen(!allOpen);
  };

  const onDownload = async () => {
    setDownloading(true);
    setDlError(null);
    try {
      await downloadReportDocx(name);
    } catch (e) {
      // 温和提示，绝不抛到外层导致软件崩溃（之前裸 URL 直连会触发此崩溃）。
      setDlError(e instanceof Error ? e.message : "下载失败");
    } finally {
      setDownloading(false);
    }
  };

  const bibleTags: string[] = Array.isArray(meta.bible) ? meta.bible : [];

  return (
    <div className="report-viewer">
      <div className="report-toolbar">
        <div className="ml-auto flex items-center gap-2">
          <button className="btn ghost" onClick={toggleAll} disabled={chapters.length === 0}>
            {allOpen ? "收起全部" : "展开全部"}
          </button>
          <button className="btn primary" onClick={onDownload} disabled={downloading}>
            <Icon name="file" size={14} />
            {downloading ? "生成中…" : "下载 Word"}
          </button>
        </div>
      </div>
      {dlError && <div className="report-error">{dlError}</div>}

      <h2 className="report-title">{r.title || "审阅报告"}</h2>
      {meta.truncated && (
        <div className="report-note">
          本书章数较多，已对前 {meta.detailed_count ?? chapters.length} 章做逐章细抠，总评为基于已评析章节的综合判断。
        </div>
      )}
      {bibleTags.length > 0 && (
        <div className="report-note bible">
          <Icon name="audit" size={13} />
          已参考 analyze_novel 圣经：
          {bibleTags
            .map((t) =>
              t === "CHARACTERS"
                ? "人物"
                : t === "OUTLINE"
                  ? "大纲"
                  : t === "TIMELINE"
                    ? "时间线"
                    : t === "NOTES"
                      ? "设定"
                      : t,
            )
            .join(" · ")}
          ，逐章与总报告已据此核对设定一致性。
        </div>
      )}

      {/* —— 总体评价 —— */}
      <section className="report-section">
        <h3 className="report-h3">一、总体评价</h3>
        {o.summary && <p className="report-paragraph">{o.summary}</p>}

        {Object.keys(scores).length > 0 && (
          <div className="score-block">
            <div className="score-block-title">评分（满分 10）</div>
            {Object.entries(scores).map(([dim, sc]) => (
              <div className="score-row" key={dim}>
                <span className="score-dim">{dim}</span>
                <span className="score-bar">
                  <span
                    className="score-fill"
                    style={{
                      width: `${Math.max(0, Math.min(100, (Number(sc) / maxScore) * 100))}%`,
                    }}
                  />
                </span>
                <span className="score-val">{sc as number}</span>
              </div>
            ))}
          </div>
        )}

        <BulletGroup title="亮点" items={asArr(o.strengths)} tone="good" />
        <BulletGroup title="不足" items={asArr(o.weaknesses)} tone="warn" />
        <BulletGroup title="人物一致性观察" items={asArr(o.character_consistency)} />
        <BulletGroup title="修改建议" items={asArr(o.recommendations)} tone="accent" />
      </section>

      {/* —— 逐章细抠 —— */}
      <section className="report-section">
        <h3 className="report-h3">二、逐章细抠（{chapters.length} 章）</h3>
        <div className="chapter-list">
          {chapters.map((rawC, i) => {
            const c = asObj(rawC);
            const key = c.idx ?? i;
            const isOpen = open.has(key);
            const issues = asArr(c.issues);
            return (
              <div className={"chapter-card" + (isOpen ? " open" : "")} key={key}>
                <button className="chapter-head" onClick={() => toggle(key)}>
                  <Icon
                    name={isOpen ? "chevronDown" : "chevronRight"}
                    size={14}
                    className="chapter-caret"
                  />
                  <span className="chapter-idx">{c.idx ?? ""}</span>
                  <span className="chapter-title">{c.title ?? "未命名章节"}</span>
                  {issues.length > 0 && (
                    <span className="chapter-issue-count">{issues.length} 处</span>
                  )}
                </button>
                {isOpen && (
                  <div className="chapter-body">
                    {c.summary && <p className="chapter-summary">{c.summary}</p>}
                    {c.analysis && <p className="chapter-analysis">{c.analysis}</p>}
                    {issues.length > 0 && (
                      <ul className="issue-list">
                        {issues.map((it, j) => {
                          const issue = asObj(it);
                          return (
                            <li className="issue-item" key={j}>
                              <span className="issue-type">{issue.type ?? ""}</span>
                              {issue.excerpt && (
                                <span className="issue-excerpt">「{issue.excerpt}」</span>
                              )}
                              {issue.comment && (
                                <span className="issue-comment">{issue.comment}</span>
                              )}
                            </li>
                          );
                        })}
                      </ul>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </section>
    </div>
  );
}

function BulletGroup({
  title,
  items,
  tone,
}: {
  title: string;
  items: string[];
  tone?: "good" | "warn" | "accent";
}) {
  if (!items || items.length === 0) return null;
  return (
    <div className="bullet-group">
      <div className={"bullet-title" + (tone ? " " + tone : "")}>{title}</div>
      <ul className="bullet-list">
        {items.map((s, i) => (
          <li key={i}>{s}</li>
        ))}
      </ul>
    </div>
  );
}
