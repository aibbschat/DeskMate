/**
 * 待答交互卡（审批 / 目录授权 / 计划 / 提问），挂在输入区上方。
 *
 * 为什么不直接用桌面 ApprovalCard：它把 `always_tool` / `always_command` / `always_domain`
 * / `readonly_session` 这些**大范围放行**平铺在按钮上（ApprovalCard.tsx:238-275）。手机上
 * 拇指一点就永久放行一类工具，是我最不想要的误触后果（设计稿 §三.7）。所以这里只保留
 * 「允许一次 / 拒绝 /（自动化任务里）本任务都允许」。
 *
 * 措辞走 `askPhrase.ts` 的中文层，不走桌面那套硬编码英文。
 */
import { useState } from "react";
import { askEvidence, askPhrase } from "./askPhrase";
import type { Item } from "../types";

interface Props {
  item: Item;
  onApprove: (decision: "once" | "deny" | "always_task") => void;
  onAnswer: (answer: string) => void;
  onDirectory: (granted: boolean) => void;
  onPlan: (approved: boolean) => void;
  /** 这条是自动化任务跑出来的 → 才给「本任务内始终」 */
  fromTask?: boolean;
}

export function InteractiveDock({ item, onApprove, onAnswer, onDirectory, onPlan, fromTask }: Props) {
  if (item.kind === "approval") {
    const phrase = askPhrase(item.name, item.args);
    const evidence = askEvidence(item.args);
    return (
      <div className="mdock">
        <div className="mdock-h">
          <span className="mdock-k">她想动手</span>
          <span className="mdock-title">{phrase}</span>
        </div>
        {evidence && <div className="mdock-args">{evidence}</div>}
        {item.reason && <div className="mdock-reason">{item.reason}</div>}
        <div className="mdock-btns">
          <button type="button" className="btn pri" onClick={() => onApprove("once")}>
            允许一次
          </button>
          <button type="button" className="btn" onClick={() => onApprove("deny")}>
            拒绝
          </button>
          {fromTask && (
            <button type="button" className="btn wide" onClick={() => onApprove("always_task")}>
              这个任务里都允许
            </button>
          )}
        </div>
      </div>
    );
  }

  if (item.kind === "dirreq") {
    return (
      <div className="mdock">
        <div className="mdock-h">
          <span className="mdock-k">她想访问文件夹</span>
          <span className="mdock-title">{item.path || "另一个目录"}</span>
        </div>
        {item.reason && <div className="mdock-reason">{item.reason}</div>}
        <div className="mdock-btns">
          <button type="button" className="btn pri" onClick={() => onDirectory(true)}>
            允许{item.writable ? "（可写）" : "（只读）"}
          </button>
          <button type="button" className="btn" onClick={() => onDirectory(false)}>
            不给
          </button>
        </div>
      </div>
    );
  }

  if (item.kind === "planreq") {
    return (
      <div className="mdock">
        <div className="mdock-h">
          <span className="mdock-k">她提了个计划</span>
        </div>
        <div className="mdock-plan">{item.plan}</div>
        <div className="mdock-btns">
          <button type="button" className="btn pri" onClick={() => onPlan(true)}>
            照这个做
          </button>
          <button type="button" className="btn" onClick={() => onPlan(false)}>
            先别
          </button>
        </div>
      </div>
    );
  }

  if (item.kind === "question") {
    return (
      <div className="mdock">
        <div className="mdock-h">
          <span className="mdock-k">她在问你</span>
          <span className="mdock-title">{item.question}</span>
        </div>
        {!!item.options?.length && (
          <div className="mdock-opts">
            {item.options.map((o, i) => {
              // Item 的 question.options 类型是 string[]（富选项只存在于收件箱那条路上）
              const label = String(o);
              return (
                <button key={i} type="button" className="btn opt" onClick={() => onAnswer(label)}>
                  {label}
                </button>
              );
            })}
          </div>
        )}
        {item.allow_text !== false && <QuestionText onSubmit={onAnswer} />}
      </div>
    );
  }

  return null;
}

function QuestionText({ onSubmit }: { onSubmit: (a: string) => void }) {
  const [v, setV] = useState("");
  return (
    <div className="mdock-text">
      <input
        className="mdock-input"
        value={v}
        placeholder="回一句…"
        onChange={(e) => setV(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && v.trim()) onSubmit(v.trim());
        }}
      />
      <button type="button" className="btn pri" disabled={!v.trim()} onClick={() => v.trim() && onSubmit(v.trim())}>
        发送
      </button>
    </div>
  );
}
