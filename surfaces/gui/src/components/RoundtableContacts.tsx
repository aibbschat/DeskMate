/**
 * RoundtableContacts — 右侧栏「我的伙伴」联系人列表（QQ 好友式）。
 *
 * 数据源：上游 persona 注册表（/v1/personas），分为两组：
 *   - 创作：roundtable:true 的圆桌专家（7 位中文创作向专家），新增后自动出现。
 *   - 工程：上游官方安全/审计类角色（cloud-posture / dep-audit / security）。
 * 白名单走 fork 侧常量 ENGINEERING_IDS，不修改 upstream builtin 文件。
 * 点击联系人 = 以该 persona 开独立会话；当前会话正对应某角色时该行高亮。
 * 折叠交互复用 RightRail 的 RailSection。
 * 注意：小桔(deskmate) 仅出现在左侧新建会话，不在本栏（避免左右重复）。
 */

import type { Persona } from "../api";
import { shortPersonaName } from "../personaScope";
import { PersonaGlyph } from "./personaIcon";

import historianImg from "../assets/avatars/historian.png";
import litEditorImg from "../assets/avatars/lit-editor.png";
import proofreaderImg from "../assets/avatars/proofreader.png";
import readerImg from "../assets/avatars/reader.png";
import publisherImg from "../assets/avatars/publisher.png";
import adaptationImg from "../assets/avatars/adaptation.png";
import genreImg from "../assets/avatars/genre.png";

const AVATAR: Record<string, string> = {
  historian: historianImg,
  "lit-editor": litEditorImg,
  proofreader: proofreaderImg,
  reader: readerImg,
  publisher: publisherImg,
  adaptation: adaptationImg,
  genre: genreImg,
};

// 工程组白名单：fork 侧定义，仅前端过滤用，绝不回写到 upstream builtin manifest。
export const ENGINEERING_IDS = ["cloud-posture", "dep-audit", "security"];

interface Props {
  personas: Persona[];
  /** 当前会话的 persona id，命中行高亮 */
  activePersonaId?: string;
  /** 点击联系人：以该 persona 开独立会话 */
  onOpenSession: (personaId: string) => void;
}

function ContactAvatar({ p }: { p: Persona }) {
  const img = AVATAR[p.id];
  if (img) {
    return <img src={img} alt={p.name} className="rail-contact-img" loading="lazy" />;
  }
  return <PersonaGlyph icon={p.icon} family={p.family} size={15} />;
}

function ContactRow({
  p,
  active,
  onOpenSession,
}: {
  p: Persona;
  active: boolean;
  onOpenSession: (personaId: string) => void;
}) {
  return (
    <button
      key={p.id}
      className={"rail-contact" + (active ? " is-active" : "")}
      onClick={() => onOpenSession(p.id)}
      title={p.tagline}
      type="button"
    >
      <span className="rail-contact-avatar">
        <ContactAvatar p={p} />
      </span>
      <span className="rail-contact-meta">
        <span className="rail-contact-name">{shortPersonaName(p.name, p.id)}</span>
        {p.tagline && <span className="rail-contact-tagline">{p.tagline}</span>}
      </span>
      {active && <span className="rail-contact-live" title="当前会话" />}
    </button>
  );
}

export function RoundtableContacts({ personas, activePersonaId, onOpenSession }: Props) {
  const creation = personas.filter((p) => p.roundtable);
  const engineering = personas.filter((p) => ENGINEERING_IDS.includes(p.id));
  if (creation.length === 0 && engineering.length === 0) return null;
  return (
    <div className="rail-contacts">
      {creation.length > 0 && <div className="rail-group-label">创作</div>}
      {creation.map((p) => (
        <ContactRow key={p.id} p={p} active={p.id === activePersonaId} onOpenSession={onOpenSession} />
      ))}
      {engineering.length > 0 && <div className="rail-group-label">工程</div>}
      {engineering.map((p) => (
        <ContactRow key={p.id} p={p} active={p.id === activePersonaId} onOpenSession={onOpenSession} />
      ))}
      <div className="rail-muted rail-contact-hint">点击开聊 · 聊天中 @名字 可临时请其他专家补位</div>
    </div>
  );
}
