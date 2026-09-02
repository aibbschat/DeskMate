/**
 * GardenStoryBook — 羁绊故事簿（P1）。
 *
 * 花园里的一本「我们走到哪儿了」：bond 阶段 + 阶段叙事 + 一路走来的数字
 * （对话/连续/勋章/花开）+「它眼中的我」画像栏目聚合。
 *
 * 全部来自后端 /v1/deskmate/garden/storybook（零 LLM：阶段叙事为静态模板，
 * 数字真实，画像聚合真实 portrait）。数据未到/为空时不渲染，不编造。
 */

export interface StoryBook {
  bond?: {
    stage: string;
    title: string;
    narrative: string;
    depth: number;
    progress: number;
    maxed: boolean;
  } | null;
  numbers?: {
    total_chats: number;
    streak_days: number;
    badge_count: number;
    flower_count: number;
  } | null;
  portrait?: Array<{
    title: string;
    status: string;
    summary: string;
    article_count: number;
  }>;
}

const STATUS_LABEL: Record<string, string> = {
  ready: "熟识",
  learning: "学习中",
  empty: "待了解",
};

export function GardenStoryBook({ story }: { story?: StoryBook | null }) {
  if (!story || !story.bond) return null;
  const { bond, numbers, portrait } = story;

  const progressPct = Math.round((bond.progress ?? 0) * 100);
  const statItems = [
    { label: "对话", value: numbers?.total_chats ?? 0, icon: "💬" },
    { label: "连续", value: numbers?.streak_days ?? 0, icon: "🔥" },
    { label: "勋章", value: numbers?.badge_count ?? 0, icon: "🏅" },
    { label: "花开", value: numbers?.flower_count ?? 0, icon: "🌸" },
  ];

  return (
    <div className="garden-storybook">
      <div className="garden-storybook-head">
        <span className="garden-storybook-title">📖 羁绊故事簿</span>
        <span className="garden-storybook-stage">{bond.stage}</span>
      </div>

      <div className="garden-storybook-narrative">{bond.narrative}</div>

      {!bond.maxed && progressPct >= 0 && (
        <div className="garden-storybook-progress">
          <div className="garden-storybook-bar">
            <div className="garden-storybook-bar-fill" style={{ width: `${Math.min(100, progressPct)}%` }} />
          </div>
          <span className="garden-storybook-bar-label">{progressPct}%</span>
        </div>
      )}

      <div className="garden-storybook-stats">
        {statItems.map((s) => (
          <div key={s.label} className="garden-storybook-stat">
            <span className="garden-storybook-stat-icon">{s.icon}</span>
            <span className="garden-storybook-stat-value">{s.value}</span>
            <span className="garden-storybook-stat-label">{s.label}</span>
          </div>
        ))}
      </div>

      {portrait && portrait.length > 0 && (
        <div className="garden-storybook-portrait">
          <div className="garden-storybook-portrait-head">它眼中的我</div>
          <div className="garden-storybook-portrait-list">
            {portrait.map((s) => (
              <div key={s.title} className={`garden-storybook-portrait-item status-${s.status}`}>
                <div className="garden-storybook-portrait-name">
                  {s.title}
                  <span className="garden-storybook-portrait-status">
                    {STATUS_LABEL[s.status] ?? s.status}
                    {s.article_count > 0 ? ` · ${s.article_count}` : ""}
                  </span>
                </div>
                {s.summary && <div className="garden-storybook-portrait-summary">{s.summary}</div>}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
