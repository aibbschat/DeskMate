/**
 * RoundtableContacts — 右侧栏「Agent」好友栏（QQ 好友式联系人列表）。
 *
 * 数据源：上游 persona 注册表（/v1/personas），过滤 roundtable: true 的专家，
 * 新增圆桌 persona 后这里自动出现，无需改代码。点击联系人 = 以该 persona
 * 开独立会话（同「召唤专家」逻辑）；当前会话正对应某专家时该行高亮
 * （「正在聊天」效果）。折叠交互复用 RightRail 的 RailSection。
 */

import type { Persona } from "../api";
import { shortPersonaName } from "../personaScope";

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

interface Props {
  personas: Persona[];
  /** 当前会话的 persona id，命中行高亮 */
  activePersonaId?: string;
  /** 点击联系人：以该 persona 开独立会话 */
  onOpenSession: (personaId: string) => void;
}

export function RoundtableContacts({ personas, activePersonaId, onOpenSession }: Props) {
  const roster = personas.filter((p) => p.roundtable);
  if (roster.length === 0) return null;
  return (
    <div className="rail-contacts">
      {roster.map((p) => {
        const active = p.id === activePersonaId;
        return (
          <button
            key={p.id}
            className={"rail-contact" + (active ? " is-active" : "")}
            onClick={() => onOpenSession(p.id)}
            title={p.tagline}
            type="button"
          >
            <span className="rail-contact-avatar">
              <img
                src={AVATAR[p.id]}
                alt={p.name}
                className="rail-contact-img"
                loading="lazy"
              />
            </span>
            <span className="rail-contact-meta">
              <span className="rail-contact-name">{shortPersonaName(p.name, p.id)}</span>
              {p.tagline && <span className="rail-contact-tagline">{p.tagline}</span>}
            </span>
            {active && <span className="rail-contact-live" title="当前会话" />}
          </button>
        );
      })}
      <div className="rail-muted rail-contact-hint">点击开聊 · 聊天中 @名字 可临时请其他专家补位</div>
    </div>
  );
}
