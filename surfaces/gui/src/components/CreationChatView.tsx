/**
 * 创作对话流（CreationChatView）
 *
 * 把创作流水线放入原生对话流：用户说话推进创作，专家以对话参与者身份介入，
 * 所有产出/审核/定稿以 Markdown 文本气泡呈现，操作以快捷回复触达。
 *
 * 后端：deskmate/creation/chat_flow.py + service.py stream_chat
 * 前端：api.ts getChatHistory / streamChat
 */
import React, { useEffect, useRef, useState, useCallback } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  createPipeline,
  pickNovelFileViaServer,
  getChatHistory,
  streamChat,
  getRoundtablePersonas,
  getCreationExperts,
  addCreationExpert,
  type CreationChatMessage,
  type CreationItem,
  type Persona,
} from "../api";

interface Props {
  onBack?: () => void;
}

// 宽松的快照类型（createPipeline 返回部分字段，后续可补全）
interface ChatSnap {
  pid: string;
  concept: string;
  episodes?: number;
  phase?: string;
  items?: CreationItem[];
}

// ---- 工具 ----
const stageLabel = (snap?: ChatSnap | null): string => {
  if (!snap) return "准备中";
  const ep = snap.episodes ?? 1;
  const phase = snap.phase ?? "";
  if (phase.includes("outline") && phase !== "lead_outline") return "大纲阶段";
  if (phase === "lead_outline" || phase.includes("screen")) {
    if (ep > 1 && snap.items) {
      const done = snap.items.filter((i) => i.episode && i.state === "done").length;
      return `剧本阶段 ${done}/${ep}`;
    }
    return "剧本阶段";
  }
  if (phase === "done") return "已完成";
  return "大纲阶段";
};

// ---- 消息气泡 ----
const Bubble: React.FC<{ msg: CreationChatMessage; onQuick: (text: string) => void; onReviewExpert?: () => void }> = ({ msg, onQuick, onReviewExpert }) => {
  const isUser = msg.type === "user";
  const text = msg.text ?? msg.message ?? msg.title ?? "";

  // 快捷回复（根据消息类型）
  const quickReplies: string[] = [];
  if (msg.type === "inspire") {
    quickReplies.push("再给我点灵感");
  }
  if (msg.type === "system") {
    if (text.includes("写大纲")) quickReplies.push("写大纲");
    if (text.includes("审一下")) quickReplies.push("审一下");
    if (text.includes("定稿")) quickReplies.push("定稿");
    if (text.includes("写第1集") || text.includes("写第")) quickReplies.push("写第1集");
    if (text.includes("一键直通")) quickReplies.push("一键直通");
  }
  if (msg.type === "artifact") {
    quickReplies.push("审一下", "定稿");
  }
  if (msg.type === "review") {
    quickReplies.push(msg.approved ? "定稿" : "重写，按意见修改");
  }
  if (msg.type === "milestone" && msg.kind === "outline") {
    quickReplies.push("写第1集", "一键直通");
  }

  return (
    <div style={{ display: "flex", justifyContent: isUser ? "flex-end" : "flex-start", marginBottom: 12 }}>
      <div style={{ maxWidth: "82%", display: "flex", flexDirection: "column", alignItems: isUser ? "flex-end" : "flex-start" }}>
        {/* 消息头 */}
        {!isUser && (
          <div style={{ fontSize: 11, color: "#8b8b8b", marginBottom: 3, display: "flex", gap: 6, alignItems: "center" }}>
            {msg.type === "expert" && <span style={{ color: "#7c6cf5", fontWeight: 600 }}>🧠 {msg.expert}</span>}
            {msg.type === "artifact" && <span style={{ color: "#2d8cf0", fontWeight: 600 }}>📄 {msg.title}</span>}
            {msg.type === "review" && <span style={{ color: msg.approved ? "#19b36e" : "#e85d5d", fontWeight: 600 }}>
              {msg.approved ? "✅ 审核通过" : "⚠️ 审核打回"}
            </span>}
            {msg.type === "milestone" && <span style={{ color: "#d4a017", fontWeight: 600 }}>🎉 {msg.title}</span>}
            {msg.type === "progress" && <span style={{ color: "#8b8b8b" }}>⏳ {msg.label}</span>}
            {msg.type === "help" && <span style={{ color: "#8b8b8b" }}>💡 帮助</span>}
            {msg.type === "error" && <span style={{ color: "#e85d5d", fontWeight: 600 }}>❌ 错误</span>}
            {msg.type === "inspire" && <span style={{ color: "#d4a017", fontWeight: 600 }}>💡 小桔的灵感</span>}
            {(msg.type === "system" || msg.type === "roundtable" || msg.type === "expert_card") && <span style={{ color: "#8b8b8b" }}>系统</span>}
            <span style={{ opacity: 0.6 }}>{msg.ts?.slice(11, 16)}</span>
          </div>
        )}

        {/* 气泡主体 */}
        <div style={{
          background: isUser ? "#2d6cdf" : (msg.type === "inspire" ? "#fff8e8" : "#f5f5f7"),
          color: isUser ? "#fff" : "#1d1d1f",
          borderRadius: isUser ? "14px 14px 4px 14px" : "14px 14px 14px 4px",
          border: msg.type === "inspire" ? "1px solid #f0d9a0" : "none",
          padding: "10px 14px",
          fontSize: 14,
          lineHeight: 1.6,
          wordBreak: "break-word",
          boxShadow: "0 1px 2px rgba(0,0,0,0.04)",
        }}>
          {/* progress 进度条 */}
          {msg.type === "progress" && (
            <div style={{ marginBottom: 6 }}>
              <div style={{ height: 4, background: "rgba(0,0,0,0.08)", borderRadius: 2, overflow: "hidden" }}>
                <div style={{ height: "100%", width: `${msg.progress ?? 50}%`, background: "#2d6cdf", borderRadius: 2, transition: "width 0.3s" }} />
              </div>
            </div>
          )}
          {/* review 标准 */}
          {msg.type === "review" && msg.criteria && (
            <div style={{ fontSize: 11, color: "#8b8b8b", marginBottom: 6, fontStyle: "italic" }}>审核标准：{msg.criteria}</div>
          )}
          {/* Markdown 内容 */}
          {text && (
            <div className="md-bubble" style={{ color: isUser ? "#fff" : "#1d1d1f" }}>
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={{
                a: ({ ...p }) => <a style={{ color: isUser ? "#a8d0ff" : "#2d6cdf" }} {...p} />,
                code: ({ ...p }) => <code style={{ background: isUser ? "rgba(255,255,255,0.15)" : "rgba(0,0,0,0.06)", borderRadius: 3, padding: "1px 4px", fontSize: 13 }} {...p} />,
                table: ({ ...p }) => <table style={{ borderCollapse: "collapse", fontSize: 13, margin: "6px 0" }} {...p} />,
                th: ({ ...p }) => <th style={{ border: "1px solid rgba(128,128,128,0.3)", padding: "4px 8px", textAlign: "left" }} {...p} />,
                td: ({ ...p }) => <td style={{ border: "1px solid rgba(128,128,128,0.3)", padding: "4px 8px" }} {...p} />,
              }}>{text}</ReactMarkdown>
            </div>
          )}
        </div>

        {/* 快捷回复 */}
        {quickReplies.length > 0 && (
          <div style={{ display: "flex", gap: 6, marginTop: 6, flexWrap: "wrap" }}>
            {quickReplies.map((q) => (
              <button key={q} onClick={() => {
                if (q === "审一下" && onReviewExpert) onReviewExpert();
                else onQuick(q);
              }} style={{
                fontSize: 12, padding: "4px 12px", borderRadius: 14,
                border: "1px solid #d0d0d5", background: "#fff", color: "#2d6cdf",
                cursor: "pointer", transition: "all 0.15s",
              }} onMouseEnter={(e) => { e.currentTarget.style.background = "#f0f4ff"; e.currentTarget.style.borderColor = "#2d6cdf"; }}
                onMouseLeave={(e) => { e.currentTarget.style.background = "#fff"; e.currentTarget.style.borderColor = "#d0d0d5"; }}>
                {q}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};

// ---- 主组件 ----
export const CreationChatView: React.FC<Props> = ({ onBack }) => {
  const [pid, setPid] = useState<string | null>(null);
  const [snap, setSnap] = useState<ChatSnap | null>(null);
  const [messages, setMessages] = useState<CreationChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [personas, setPersonas] = useState<Persona[]>([]);
  const [showExpertPicker, setShowExpertPicker] = useState(false);
  const [pickerMode, setPickerMode] = useState<"review" | "summon">("review");
  // C+ 动态加人：团队在册专家 + 可临时加入的专家
  const [roster, setRoster] = useState<{ actor: string; persona: string; reason?: string }[]>([]);
  const [available, setAvailable] = useState<{ persona: string; name: string; tagline?: string; joined: boolean }[]>([]);
  const [showAddExpert, setShowAddExpert] = useState(false);

  // 创建表单
  const [concept, setConcept] = useState("");
  const [novelRef, setNovelRef] = useState("");
  const [episodes, setEpisodes] = useState(1);
  const [executor, setExecutor] = useState<"stub" | "llm" | "agent">("agent");

  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // 自动滚动到底部
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages]);

  // 加载专家列表
  useEffect(() => {
    getRoundtablePersonas().then(setPersonas).catch((e) => console.error("加载专家列表失败", e));
  }, []);

  // 加载团队花名册与可加专家（C+ 动态加人）
  const loadExperts = useCallback(async (p: string) => {
    try {
      const r = await getCreationExperts(p);
      setRoster(r.roster ?? []);
      setAvailable(r.available ?? []);
    } catch (e) {
      console.error("加载团队成员失败", e);
    }
  }, []);

  useEffect(() => {
    if (pid) loadExperts(pid);
  }, [pid, loadExperts]);

  const addExpert = useCallback(async (persona: string) => {
    if (!pid) return;
    setShowAddExpert(false);
    try {
      const r = await addCreationExpert(pid, persona);
      if (!r.approved) {
        alert(`加人失败：${r.error ?? "未知原因"}`);
        return;
      }
      setRoster(r.roster ?? []);
      // 新成员加入后刷新可选列表（joined 标记随之更新）
      loadExperts(pid);
    } catch (e) {
      alert(`加人失败：${e}`);
    }
  }, [pid, loadExperts]);

  // 加载历史
  const loadHistory = useCallback(async (p: string) => {
    try {
      const msgs = await getChatHistory(p);
      setMessages(msgs);
    } catch (e) {
      console.error("加载对话历史失败", e);
    }
  }, []);

  // 发消息
  const send = useCallback(async (text: string) => {
    if (!pid || busy || !text.trim()) return;
    setBusy(true);
    setInput("");
    try {
      for await (const msg of streamChat(pid, text)) {
        setMessages((prev) => {
          // 去重（同 id 不重复加）
          if (prev.some((m) => m.id === msg.id)) return prev;
          return [...prev, msg];
        });
      }
    } catch (e) {
      console.error("发送失败", e);
      setMessages((prev) => [...prev, { id: "err", type: "error", ts: new Date().toISOString(), text: `发送失败：${e}` }]);
    } finally {
      setBusy(false);
      inputRef.current?.focus();
    }
  }, [pid, busy]);

  // 专家选中处理
  const handleExpertSelect = (p: Persona) => {
    setShowExpertPicker(false);
    if (pickerMode === "review") {
      send(`审一下，由@${p.name}审核`);
    } else {
      // summon 模式：在 composer 插入 @专家名
      setInput((prev) => (prev ? prev + ` @${p.name} ` : `@${p.name} `));
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  };

  // 创建 pipeline
  const start = async () => {
    if (!concept.trim() || creating) return;
    setCreating(true);
    try {
      const r = await createPipeline("screenplay", concept, executor, novelRef || undefined, { episodes });
      setPid(r.pid);
      setSnap(r);
      // 加载历史（会自动收到欢迎消息）
      setTimeout(() => loadHistory(r.pid), 200);
    } catch (e) {
      console.error("创建失败", e);
      alert(`创建失败：${e}`);
    } finally {
      setCreating(false);
    }
  };

  // 浏览原著
  const browseNovel = async () => {
    try {
      const p = await pickNovelFileViaServer();
      if (p) setNovelRef(p);
    } catch (e) {
      console.error("浏览失败", e);
    }
  };

  // ---- 渲染：创建表单 ----
  if (!pid) {
    return (
      <div style={{ height: "100%", display: "flex", alignItems: "center", justifyContent: "center", background: "#fafafa" }}>
        <div style={{ width: 460, background: "#fff", borderRadius: 16, padding: 32, boxShadow: "0 4px 24px rgba(0,0,0,0.06)" }}>
          <h2 style={{ margin: "0 0 6px", fontSize: 22, fontWeight: 700 }}>🎬 新建创作</h2>
          <p style={{ margin: "0 0 24px", color: "#8b8b8b", fontSize: 13 }}>用对话的方式写剧本，专家随叫随到</p>

          <div style={{ marginBottom: 16 }}>
            <label style={{ display: "block", fontSize: 13, fontWeight: 600, marginBottom: 6 }}>故事概念 *</label>
            <input value={concept} onChange={(e) => setConcept(e.target.value)} placeholder="例如：大明水师风起宁波"
              style={{ width: "100%", padding: "10px 12px", borderRadius: 8, border: "1px solid #d0d0d5", fontSize: 14, boxSizing: "border-box" }} />
          </div>

          <div style={{ marginBottom: 16 }}>
            <label style={{ display: "block", fontSize: 13, fontWeight: 600, marginBottom: 6 }}>原著路径（改编时填）</label>
            <div style={{ display: "flex", gap: 8 }}>
              <input value={novelRef} onChange={(e) => setNovelRef(e.target.value)} placeholder="/path/to/novel.docx"
                style={{ flex: 1, padding: "10px 12px", borderRadius: 8, border: "1px solid #d0d0d5", fontSize: 13, fontFamily: "monospace", boxSizing: "border-box" }} />
              <button onClick={browseNovel} style={{ padding: "10px 16px", borderRadius: 8, border: "1px solid #d0d0d5", background: "#f5f5f7", cursor: "pointer", fontSize: 13, whiteSpace: "nowrap" }}>浏览</button>
            </div>
          </div>

          <div style={{ display: "flex", gap: 16, marginBottom: 24 }}>
            <div style={{ flex: 1 }}>
              <label style={{ display: "block", fontSize: 13, fontWeight: 600, marginBottom: 6 }}>集数</label>
              <input type="number" min={1} max={99} value={episodes} onChange={(e) => setEpisodes(Math.max(1, parseInt(e.target.value) || 1))}
                style={{ width: "100%", padding: "10px 12px", borderRadius: 8, border: "1px solid #d0d0d5", fontSize: 14, boxSizing: "border-box" }} />
            </div>
            <div style={{ flex: 1 }}>
              <label style={{ display: "block", fontSize: 13, fontWeight: 600, marginBottom: 6 }}>执行器</label>
              <select value={executor} onChange={(e) => setExecutor(e.target.value as "stub" | "llm" | "agent")}
                style={{ width: "100%", padding: "10px 12px", borderRadius: 8, border: "1px solid #d0d0d5", fontSize: 14, boxSizing: "border-box" }}>
                <option value="agent">真专家团队（专家不可用时回退 LLM）</option>
                <option value="llm">LLM（真实创作）</option>
                <option value="stub">Stub（演示）</option>
              </select>
            </div>
          </div>

          <button onClick={start} disabled={creating || !concept.trim()}
            style={{ width: "100%", padding: "12px", borderRadius: 10, border: "none", background: creating || !concept.trim() ? "#c0c0c5" : "#2d6cdf", color: "#fff", fontSize: 15, fontWeight: 600, cursor: creating || !concept.trim() ? "not-allowed" : "pointer" }}>
            {creating ? "创建中…" : "开始创作"}
          </button>

          {onBack && (
            <button onClick={onBack} style={{ width: "100%", marginTop: 10, padding: "8px", border: "none", background: "none", color: "#8b8b8b", fontSize: 13, cursor: "pointer" }}>返回</button>
          )}
        </div>
      </div>
    );
  }

  // ---- 渲染：对话流 ----
  // 产出总览（从消息里提取 artifact/milestone）
  const artifacts = messages.filter((m) => m.type === "artifact" || (m.type === "milestone" && m.kind !== "complete"));

  return (
    <div style={{ height: "100%", display: "flex", background: "#fafafa" }}>
      {/* 左栏 */}
      <div style={{ width: 220, background: "#fff", borderRight: "1px solid #ececef", padding: 16, display: "flex", flexDirection: "column", overflowY: "auto" }}>
        <div style={{ marginBottom: 16 }}>
          <div style={{ fontSize: 11, color: "#8b8b8b", marginBottom: 4 }}>项目</div>
          <div style={{ fontSize: 14, fontWeight: 600, lineHeight: 1.4 }}>{snap?.concept ?? "未命名"}</div>
          <div style={{ fontSize: 11, color: "#2d6cdf", marginTop: 4 }}>{stageLabel(snap)} · {snap?.episodes ?? 1} 集</div>
        </div>

        <div style={{ marginBottom: 16 }}>
          <div style={{ fontSize: 11, color: "#8b8b8b", marginBottom: 8, fontWeight: 600 }}>产出总览</div>
          {artifacts.length === 0 && <div style={{ fontSize: 12, color: "#c0c0c5" }}>暂无产出</div>}
          {artifacts.map((m) => (
            <div key={m.id} style={{ fontSize: 12, padding: "6px 8px", borderRadius: 6, marginBottom: 4, background: m.type === "milestone" ? "#f0f9f4" : "#f5f5f7", color: m.type === "milestone" ? "#19b36e" : "#1d1d1f", cursor: "pointer" }}
              onClick={() => {
                const el = document.getElementById(`msg-${m.id}`);
                el?.scrollIntoView({ behavior: "smooth", block: "center" });
              }}>
              {m.type === "milestone" ? "✅ " : "📄 "}{m.title ?? m.kind}
            </div>
          ))}
        </div>

        <div style={{ marginBottom: 16 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
            <div style={{ fontSize: 11, color: "#8b8b8b", fontWeight: 600 }}>团队成员</div>
            <button onClick={() => setShowAddExpert(true)} title="临时再加一位专家"
              style={{ fontSize: 11, padding: "2px 8px", borderRadius: 10, border: "1px solid #d0d0d5", background: "#fff", color: "#2d6cdf", cursor: "pointer" }}>＋ 加专家</button>
          </div>
          {roster.length === 0 && <div style={{ fontSize: 12, color: "#c0c0c5" }}>暂无成员</div>}
          {roster.map((w) => (
            <div key={w.actor} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, padding: "4px 0" }}>
              <span style={{ width: 6, height: 6, borderRadius: "50%", background: "#19b36e", flexShrink: 0 }} />
              <span style={{ fontWeight: 500 }}>{w.actor}</span>
              {w.reason && <span style={{ color: "#8b8b8b", fontSize: 11, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{w.reason}</span>}
            </div>
          ))}
        </div>

        <div style={{ marginTop: "auto" }}>
          <div style={{ fontSize: 11, color: "#8b8b8b", marginBottom: 8, fontWeight: 600 }}>快捷操作</div>
          <button onClick={() => send("帮助")} style={{ width: "100%", fontSize: 12, padding: "8px", borderRadius: 6, border: "1px solid #d0d0d5", background: "#fff", marginBottom: 6, cursor: "pointer" }}>查看指令</button>
          <button onClick={() => send("一键直通")} style={{ width: "100%", fontSize: 12, padding: "8px", borderRadius: 6, border: "none", background: "#2d6cdf", color: "#fff", cursor: "pointer" }}>⚡ 一键直通</button>
        </div>
      </div>

      {/* 主区 */}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
        {/* 头部 */}
        <div style={{ padding: "12px 20px", background: "#fff", borderBottom: "1px solid #ececef", display: "flex", alignItems: "center", gap: 12 }}>
          {onBack && <button onClick={onBack} style={{ border: "none", background: "none", cursor: "pointer", fontSize: 18, color: "#8b8b8b" }}>←</button>}
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 15, fontWeight: 600 }}>{snap?.concept ?? "创作中"}</div>
            <div style={{ fontSize: 11, color: "#8b8b8b" }}>{stageLabel(snap)} · 对话式创作</div>
          </div>
          <div style={{ display: "flex", gap: 6 }}>
            <button onClick={() => send("写大纲")} style={{ fontSize: 12, padding: "6px 14px", borderRadius: 14, border: "1px solid #d0d0d5", background: "#fff", cursor: "pointer" }}>写大纲</button>
            <button onClick={() => { setPickerMode("summon"); setShowExpertPicker(true); }} style={{ fontSize: 12, padding: "6px 14px", borderRadius: 14, border: "1px solid #d0d0d5", background: "#fff", cursor: "pointer" }}>召唤专家</button>
          </div>
        </div>

        {/* 消息列表 */}
        <div ref={scrollRef} style={{ flex: 1, overflowY: "auto", padding: "20px 24px" }}>
          {messages.map((m) => (
            <div key={m.id} id={`msg-${m.id}`}>
              <Bubble msg={m} onQuick={send} onReviewExpert={() => { setPickerMode("review"); setShowExpertPicker(true); }} />
            </div>
          ))}
          {busy && (
            <div style={{ display: "flex", justifyContent: "flex-start", marginBottom: 12 }}>
              <div style={{ background: "#f5f5f7", borderRadius: "14px 14px 14px 4px", padding: "10px 14px", fontSize: 13, color: "#8b8b8b", display: "flex", alignItems: "center", gap: 6 }}>
                <span style={{ display: "inline-block", width: 6, height: 6, borderRadius: "50%", background: "#2d6cdf", animation: "cre-breathe 1.2s infinite" }} />
                思考中…
              </div>
            </div>
          )}
        </div>

        {/* Composer */}
        <div style={{ padding: "12px 20px 16px", background: "#fff", borderTop: "1px solid #ececef" }}>
          <div style={{ display: "flex", gap: 8, alignItems: "flex-end" }}>
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  send(input);
                }
              }}
              placeholder="说点什么推进创作…（写大纲 / 写第1集 / 审一下 / 定稿 / @专家 问题 / 一键直通）"
              rows={1}
              style={{
                flex: 1, padding: "10px 14px", borderRadius: 10, border: "1px solid #d0d0d5",
                fontSize: 14, resize: "none", maxHeight: 120, fontFamily: "inherit", boxSizing: "border-box",
                outline: "none",
              }}
              onFocus={(e) => { e.currentTarget.style.borderColor = "#2d6cdf"; }}
              onBlur={(e) => { e.currentTarget.style.borderColor = "#d0d0d5"; }}
            />
            <button onClick={() => send(input)} disabled={busy || !input.trim()}
              style={{ padding: "10px 20px", borderRadius: 10, border: "none", background: busy || !input.trim() ? "#c0c0c5" : "#2d6cdf", color: "#fff", fontSize: 14, fontWeight: 600, cursor: busy || !input.trim() ? "not-allowed" : "pointer" }}>
              发送
            </button>
          </div>
          <div style={{ fontSize: 11, color: "#c0c0c5", marginTop: 6, textAlign: "center" }}>Enter 发送 · Shift+Enter 换行</div>
        </div>
      </div>

      <style>{`
        @keyframes cre-breathe { 0%,100% { opacity: 0.3; } 50% { opacity: 1; } }
        .md-bubble h1 { font-size: 18px; margin: 8px 0 6px; font-weight: 700; }
        .md-bubble h2 { font-size: 16px; margin: 8px 0 4px; font-weight: 700; }
        .md-bubble h3 { font-size: 14px; margin: 6px 0 4px; font-weight: 600; }
        .md-bubble p { margin: 4px 0; }
        .md-bubble ul, .md-bubble ol { margin: 4px 0; padding-left: 20px; }
        .md-bubble li { margin: 2px 0; }
        .md-bubble blockquote { margin: 6px 0; padding: "4px 12px"; border-left: 3px solid rgba(128,128,128,0.3); color: rgba(0,0,0,0.6); font-style: italic; }
        .md-bubble hr { border: none; border-top: 1px solid rgba(128,128,128,0.2); margin: 8px 0; }
        .md-bubble strong { font-weight: 600; }
      `}</style>

      {/* 专家选择器弹出层 */}
      {showExpertPicker && (
        <div style={{
          position: "fixed", inset: 0, background: "rgba(0,0,0,0.4)",
          display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000,
        }} onClick={() => setShowExpertPicker(false)}>
          <div style={{
            background: "#fff", borderRadius: 16, padding: 24, width: 420, maxHeight: "70vh",
            overflowY: "auto", boxShadow: "0 8px 40px rgba(0,0,0,0.15)",
          }} onClick={(e) => e.stopPropagation()}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
              <h3 style={{ margin: 0, fontSize: 17, fontWeight: 700 }}>
                {pickerMode === "review" ? "选择审核专家" : "召唤专家"}
              </h3>
              <button onClick={() => setShowExpertPicker(false)} style={{ border: "none", background: "none", fontSize: 20, cursor: "pointer", color: "#8b8b8b" }}>×</button>
            </div>
            <p style={{ margin: "0 0 16px", fontSize: 12, color: "#8b8b8b" }}>
              {pickerMode === "review" ? "选择哪位专家负责本次审核，审核方向由专家专长决定" : "选择专家后，在输入框继续输入你的问题"}
            </p>
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {personas.map((p) => (
                <button key={p.id} onClick={() => handleExpertSelect(p)} style={{
                  display: "flex", alignItems: "center", gap: 12, padding: "12px 14px",
                  borderRadius: 10, border: "1px solid #ececef", background: "#fff",
                  cursor: "pointer", textAlign: "left", transition: "all 0.15s",
                }} onMouseEnter={(e) => { e.currentTarget.style.background = "#f0f4ff"; e.currentTarget.style.borderColor = "#2d6cdf"; }}
                  onMouseLeave={(e) => { e.currentTarget.style.background = "#fff"; e.currentTarget.style.borderColor = "#ececef"; }}>
                  <div style={{ width: 36, height: 36, borderRadius: "50%", background: "linear-gradient(135deg, #667eea, #764ba2)", display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontSize: 16, fontWeight: 700, flexShrink: 0 }}>
                    {p.name.charAt(0)}
                  </div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 14, fontWeight: 600, color: "#1d1d1f" }}>{p.name}</div>
                    <div style={{ fontSize: 12, color: "#8b8b8b", marginTop: 2 }}>{p.roundtable_role || p.tagline}</div>
                  </div>
                  {p.roundtable_market && <span style={{ fontSize: 10, padding: "2px 6px", borderRadius: 4, background: "#fff3e0", color: "#e65100" }}>市场</span>}
                </button>
              ))}
              {personas.length === 0 && <div style={{ textAlign: "center", color: "#8b8b8b", fontSize: 13, padding: 20 }}>加载专家列表中…</div>}
            </div>
          </div>
        </div>
      )}

      {/* 加专家弹出层（C+ 动态加人：加进团队花名册，后续派活可派给他） */}
      {showAddExpert && (
        <div style={{
          position: "fixed", inset: 0, background: "rgba(0,0,0,0.4)",
          display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000,
        }} onClick={() => setShowAddExpert(false)}>
          <div style={{
            background: "#fff", borderRadius: 16, padding: 24, width: 420, maxHeight: "70vh",
            overflowY: "auto", boxShadow: "0 8px 40px rgba(0,0,0,0.15)",
          }} onClick={(e) => e.stopPropagation()}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
              <h3 style={{ margin: 0, fontSize: 17, fontWeight: 700 }}>加一位专家</h3>
              <button onClick={() => setShowAddExpert(false)} style={{ border: "none", background: "none", fontSize: 20, cursor: "pointer", color: "#8b8b8b" }}>×</button>
            </div>
            <p style={{ margin: "0 0 16px", fontSize: 12, color: "#8b8b8b" }}>
              临时补进这支团队（不重建团队、不动已完成的工作），后续写作/审核可派给他。
            </p>
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {available.filter((p) => !p.joined).map((p) => (
                <button key={p.persona} onClick={() => addExpert(p.persona)} style={{
                  display: "flex", alignItems: "center", gap: 12, padding: "12px 14px",
                  borderRadius: 10, border: "1px solid #ececef", background: "#fff",
                  cursor: "pointer", textAlign: "left", transition: "all 0.15s",
                }} onMouseEnter={(e) => { e.currentTarget.style.background = "#f0f4ff"; e.currentTarget.style.borderColor = "#2d6cdf"; }}
                  onMouseLeave={(e) => { e.currentTarget.style.background = "#fff"; e.currentTarget.style.borderColor = "#ececef"; }}>
                  <div style={{ width: 36, height: 36, borderRadius: "50%", background: "linear-gradient(135deg, #667eea, #764ba2)", display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontSize: 16, fontWeight: 700, flexShrink: 0 }}>
                    {p.name.charAt(0)}
                  </div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 14, fontWeight: 600, color: "#1d1d1f" }}>{p.name}</div>
                    {p.tagline && <div style={{ fontSize: 12, color: "#8b8b8b", marginTop: 2 }}>{p.tagline}</div>}
                  </div>
                </button>
              ))}
              {available.filter((p) => !p.joined).length === 0 && (
                <div style={{ textAlign: "center", color: "#8b8b8b", fontSize: 13, padding: 20 }}>
                  {available.length === 0 ? "专家列表加载中…" : "当前可用专家都已在团队里了"}
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default CreationChatView;
