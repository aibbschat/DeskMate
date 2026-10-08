/**
 * DeskHome — 桌搭模式空会话首页。
 *
 * 仅当 agent === "deskmate" 且会话为空（idle）时由 App.tsx 渲染。
 *
 * 首页只保留「小桔在忙」——展示小桔自主层正在做什么；并在这个空会话时刻递上
 * 她云端/手机端主动产出的「几件还没收的事」（对话式开场白，非弹窗）：每条带
 * 证据 + 理解，可「聊这个」起聊（§11.16 层三：短句柄 prefill `#xxx 这条怎么回事？`，
 * 她自己 proactive_lookup 回查展开，不把她的整句话塞回她嘴里）。
 */

import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { AgencyPanel } from "./AgencyPanel";
import { getDeskmateHandoff, ackDeskmateHandoff, type HandoffItem } from "../../api";

export function DeskHome({
  onPrefill,
  onIntentExecuted,
}: {
  onPrefill?: (text: string) => void;
  onIntentExecuted?: (intentId: string) => void;
}) {
  const { t, i18n } = useTranslation();
  const [handoff, setHandoff] = useState<{ text: string; items: HandoffItem[] } | null>(null);
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());

  useEffect(() => {
    let alive = true;
    getDeskmateHandoff()
      .then((h) => {
        if (alive && h) setHandoff(h);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  // 每次回到首页（组件重新挂载）随机抽一条问候，制造新鲜感。
  const greeting = useMemo(() => {
    const list = t("deskmate.greetings", { returnObjects: true }) as unknown as string[];
    if (!Array.isArray(list) || list.length === 0) return "";
    return list[Math.floor(Math.random() * list.length)];
  }, [t, i18n.language]);

  const visibleItems = handoff ? handoff.items.filter((it) => !dismissed.has(it.id)) : [];

  return (
    <div className="hero">
      <h1 className="dm-home-greeting">
        <span className="mark">✦</span>
        {greeting}
      </h1>

      {/* 小桔主动产出台账的对话式对接开场白：云端/手机端主动产出 → 本地开聊时递出。
          非浮层，是空会话首页内的一条引导卡片，保证用户能顺着她提出的主题接得上。 */}
      {visibleItems.length > 0 && (
        <div className="dm-handoff">
          <p className="dm-handoff-text">{handoff!.text}</p>
          <ul className="dm-handoff-list">
            {visibleItems.map((it) => (
              <li key={it.id} className="dm-handoff-item">
                <div className="dm-handoff-content">
                  <span className="dm-handoff-id">#{it.id.split(":").pop()?.slice(0, 6)}</span>
                  {it.content}
                </div>
                {it.evidence ? (
                  <div className="dm-handoff-evidence">依据：{it.evidence}</div>
                ) : null}
                {it.understanding ? (
                  <div className="dm-handoff-understanding">她的理解：{it.understanding}</div>
                ) : null}
                <div className="dm-handoff-actions">
                  <button
                    type="button"
                    className="dm-handoff-chat"
                    onClick={() => {
                      // §11.16 层三：空会话首页没有「她说」的落点，退而求其次——
                      // 只递短句柄让她自己回查展开（全文她 proactive_lookup 按 #id 调），
                      // 不再把她的整句话塞回她嘴里。
                      const short = it.id.split(":").pop()?.slice(0, 6) ?? "";
                      onPrefill?.(`#${short} 这条怎么回事？`);
                    }}
                  >
                    聊这个
                  </button>
                  <button
                    type="button"
                    className="dm-handoff-later"
                    onClick={async () => {
                      // 修⑥：「稍后」→ snoozed（3 天退避后自动复活轻递），
                      // 不再用 acknowledged 承接（那等于永久拉黑）。
                      await ackDeskmateHandoff(it.id, "snoozed");
                      setDismissed((s) => new Set(s).add(it.id));
                    }}
                  >
                    稍后
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* L2 自主层「小桔在忙」——常驻展示自主行为 + 高风险提议卡待裁决 */}
      <AgencyPanel onJumpToChat={onIntentExecuted} />
    </div>
  );
}
