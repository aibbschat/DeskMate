/**
 * PositioningCard — 关于小桔 / 合规轻陪伴定位卡（P2-8）。
 *
 * 用「工具属性 + 可爱外形 + 可控记忆」三要素，明确小桔是 AI 创作辅助工具
 * 而非虚拟伴侣，避开相关红线。数据来自后端 GET /v1/deskmate/positioning。
 *
 * 设计原则（8.0 红线「懂你 = 可感可看」）：
 * - 暖米白 + 暖橙配色，与 DiaryPanel 一致的温度感
 * - 诚实：API 失败静默降级，显示兜底文案
 */

import { useEffect, useState } from "react";
import { authedFetch, httpBase } from "../../api";
import { Icon } from "../Icon";

interface Pillar {
  key: string;
  title: string;
  desc: string;
}

interface Positioning {
  name: string;
  role: string;
  tagline: string;
  not_a_companion: boolean;
  pillars: Pillar[];
  disclaimer: string;
}

const FALLBACK: Positioning = {
  name: "小桔",
  role: "AI 创作辅助桌宠",
  tagline: "你的写作搭档，不是虚拟伴侣",
  not_a_companion: true,
  pillars: [
    { key: "tool", title: "工具属性", desc: "专注写作辅助、史料查证、灵感陪伴式提醒——是生产力工具，而非情感对象。" },
    { key: "cute", title: "可爱外形", desc: "桌宠「小桔」以轻松可爱的形象陪伴工作，降低使用门槛、增添一点温度。" },
    { key: "controllable", title: "可控记忆", desc: "记忆由你掌控——可随时在记忆面板查看、追溯与删除；敏感信息已分级、不落日志。" },
  ],
  disclaimer:
    "小桔是 AI 创作辅助工具，提供写作 / 史料 / 陪伴式提醒服务，不构成虚拟伴侣或情感陪伴关系。所有记忆均由你本人掌控，可随时查看与删除。",
};

const PILLAR_ICON: Record<string, string> = {
  tool: "tools",
  cute: "sparkle",
  controllable: "shield",
};

export function PositioningCard() {
  const [data, setData] = useState<Positioning | null>(null);

  useEffect(() => {
    authedFetch(`${httpBase()}/v1/deskmate/positioning`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => setData(d && d.name ? (d as Positioning) : FALLBACK))
      .catch(() => setData(FALLBACK));
  }, []);

  const p = data ?? FALLBACK;

  return (
    <div className="dm-positioning-card">
      <div className="dm-positioning-head">
        <Icon name="sparkle" size={18} />
        <div>
          <div className="dm-positioning-name">{p.name}</div>
          <div className="dm-positioning-role">{p.role}</div>
        </div>
      </div>
      <p className="dm-positioning-tagline">{p.tagline}</p>

      <div className="dm-positioning-pillars">
        {p.pillars.map((pill) => (
          <div key={pill.key} className="dm-positioning-pillar">
            <Icon name={(PILLAR_ICON[pill.key] ?? "sparkle") as never} size={15} />
            <div>
              <div className="dm-positioning-pillar-title">{pill.title}</div>
              <div className="dm-positioning-pillar-desc">{pill.desc}</div>
            </div>
          </div>
        ))}
      </div>

      <p className="dm-positioning-disclaimer">{p.disclaimer}</p>
    </div>
  );
}
