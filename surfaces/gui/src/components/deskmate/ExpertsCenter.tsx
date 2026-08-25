/**
 * ExpertsCenter — OpenWorker 独立「专家中心」（侧边栏「专家」导航入口）。
 *
 * 直接读取 EXPERTS 注册表渲染卡片网格：就绪的专家可「进入专家模式」
 *（复用既有的 deskmate:summon-expert 事件 + 切回会话视图）；即将上线的专家
 * 占位展示、召唤按钮禁用。新增专家只要在 experts.ts 里 push 一项，这里自动出现。
 */

import { useTranslation } from "react-i18next";
import { Icon } from "../Icon";
import { ExpertAvatar } from "./ExpertAvatar";
import type { Expert } from "./experts";

interface Props {
  experts: Expert[];
  /** 当前已激活的专家 id（用于高亮），无则 null */
  activeExpertId: string | null;
  /** 召唤某专家进入专家模式 */
  onSummon: (id: string) => void;
  /** 返回（回到会话视图） */
  onBack: () => void;
}

export function ExpertsCenter({ experts, activeExpertId, onSummon, onBack }: Props) {
  const { t } = useTranslation();

  return (
    <div className="experts-center">
      <div className="experts-head">
        <button
          className="experts-back"
          onClick={onBack}
          aria-label={t("experts.back")}
          type="button"
        >
          <Icon name="arrowLeft" size={16} />
        </button>
        <div className="experts-head-text">
          <h1 className="experts-title">{t("experts.title")}</h1>
          <p className="experts-subtitle">{t("experts.subtitle")}</p>
        </div>
      </div>

      <div className="experts-grid">
        {experts.map((e) => {
          const ready = (e.status ?? "ready") === "ready";
          const active = activeExpertId === e.id;
          return (
            <div
              key={e.id}
              className={"expert-card" + (active ? " is-active" : "") + (ready ? "" : " is-soon")}
            >
              <div className="expert-card-header">
                <ExpertAvatar expert={e} size={48} className="expert-card-avatar" />
                <div className="expert-card-meta">
                  <div className="expert-card-name-row">
                    <h2 className="expert-card-name">{e.displayName}</h2>
                    <span className={"expert-badge " + (ready ? "ready" : "soon")}>
                      {ready ? t("experts.ready") : t("experts.comingSoon")}
                    </span>
                  </div>
                  <p className="expert-card-desc">{e.description}</p>
                  {e.metadata?.skillId && (
                    <p
                      className="expert-card-skill"
                      style={{
                        margin: "6px 0 0",
                        fontSize: 11,
                        color: "var(--ink-muted)",
                        display: "flex",
                        alignItems: "center",
                        gap: 4,
                      }}
                    >
                      🔧 关联技能：{e.metadata.skillId}
                    </p>
                  )}
                </div>
              </div>

              {e.capabilities && e.capabilities.length > 0 && (
                <div className="expert-capabilities">
                  {e.capabilities.map((c) => (
                    <span key={c} className="expert-cap">
                      {c}
                    </span>
                  ))}
                </div>
              )}

              <div className="expert-card-foot">
                {ready ? (
                  <button className="expert-enter" onClick={() => onSummon(e.id)} type="button">
                    <Icon name="sparkle" size={14} />
                    {t("experts.enter")}
                  </button>
                ) : (
                  <button className="expert-enter soon" disabled type="button">
                    {t("experts.comingSoon")}
                  </button>
                )}
                {active && <span className="expert-active-tag">{t("experts.active")}</span>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
