import { useEffect, useState } from "react";
import type { SkillEntry } from "../../api";
import { getSkills } from "../../api";
import { SkillCard } from "./SkillCard";
import { ConfigModal } from "./ConfigModal";

const CATEGORIES = [
  { key: "all", label: "全部" },
  { key: "search", label: "搜索" },
  { key: "document", label: "文档" },
  { key: "database", label: "数据库" },
  { key: "development", label: "开发" },
  { key: "productivity", label: "效率" },
  { key: "other", label: "其他" },
];

export function SkillStore() {
  const [skills, setSkills] = useState<SkillEntry[]>([]);
  const [category, setCategory] = useState("all");
  const [configSkill, setConfigSkill] = useState<SkillEntry | null>(null);
  const [loading, setLoading] = useState(true);

  const load = async () => {
    try {
      const res = await getSkills();
      setSkills(res.skills || []);
    } catch {
      setSkills([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const filtered = skills.filter(
    (s) => category === "all" || s.category === category,
  );

  const installedCount = skills.filter(
    (s) => s.status === "installed" || s.status === "configured",
  ).length;

  return (
    <div className="flex-1 min-w-0 overflow-y-auto hairline-scroll">
      <div className="max-w-5xl mx-auto px-7 py-6">
        {/* Header */}
        <div className="mb-6">
          <h1 className="text-[20px] font-semibold tracking-tight">技能广场</h1>
          <p className="text-[13px] text-muted mt-1">
            安装 MCP 技能，扩展 AI 的能力。安装后按需配置 API Key。
          </p>
        </div>

        {/* Stats + category filter */}
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            {CATEGORIES.map((c) => (
              <button
                key={c.key}
                className={
                  "px-3 py-1.5 rounded-lg text-[12px] transition-colors " +
                  (category === c.key
                    ? "bg-accent text-white"
                    : "bg-faint/30 text-muted hover:bg-faint/50")
                }
                onClick={() => setCategory(c.key)}
              >
                {c.label}
              </button>
            ))}
          </div>
          <div className="text-[12px] text-faint">
            已安装 {installedCount} / {skills.length}
          </div>
        </div>

        {/* Skill grid */}
        {loading ? (
          <div className="text-center text-[13px] text-muted py-12">加载中…</div>
        ) : filtered.length === 0 ? (
          <div className="text-center text-[13px] text-muted py-12">暂无技能</div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {filtered.map((skill) => (
              <SkillCard
                key={skill.id}
                skill={skill}
                onInstalled={load}
              />
            ))}
          </div>
        )}

        {/* Installed section */}
        {installedCount > 0 && (
          <div className="mt-8">
            <h2 className="text-[16px] font-semibold mb-3">已安装</h2>
            <div className="space-y-2">
              {skills
                .filter((s) => s.status === "installed" || s.status === "configured")
                .map((skill) => (
                  <div
                    key={skill.id}
                    className="flex items-center gap-3 p-3 rounded-lg border border-line bg-panel"
                  >
                    <span className="text-lg">{skill.icon || "🔧"}</span>
                    <div className="flex-1 min-w-0">
                      <div className="text-[13px] font-medium truncate">{skill.name}</div>
                      <div className="text-[11px] text-faint">{skill.description}</div>
                    </div>
                    {skill.status === "configured" ? (
                      <span className="text-[11px] text-ok">已配置</span>
                    ) : (
                      <button
                        className="text-[12px] text-accent hover:underline"
                        onClick={() => setConfigSkill(skill)}
                      >
                        配置
                      </button>
                    )}
                  </div>
                ))}
            </div>
          </div>
        )}
      </div>

      {/* Config modal */}
      {configSkill && (
        <ConfigModal
          skill={configSkill}
          onClose={() => setConfigSkill(null)}
          onConfigured={load}
        />
      )}
    </div>
  );
}
