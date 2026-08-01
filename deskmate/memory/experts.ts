import type { IconName } from "../Icon";

// 桌搭「专家模式」的可扩展抽象。
// 第一个实例是「编辑专家」；未来在此注册大纲专家、人物专家、润色专家等，
// 回复角色（Transcript 的 who）与 Composer 专家条会自动跟随当前激活的专家变化。
// 专家中心（OpenWorker 侧边栏「专家」导航）直接读取 EXPERTS 渲染卡片，
// 新增专家只需在此 push 一项，UI 自动出现。

export type ExpertStatus = "ready" | "soon";

export interface Expert {
  id: string;
  displayName: string;
  /** Icon 组件支持的线性图标名 */
  icon: IconName;
  /** 专家头像 URL；未提供时使用名称首字 + 渐变占位头像 */
  avatar?: string;
  description: string;
  /** 预留：注入对话后端作为系统提示，使专家在对话中真正扮演该角色 */
  systemPrompt?: string;
  /** 卡片上展示的能力标签（短词） */
  capabilities?: string[];
  /** 就绪 / 即将上线。默认 "ready"。 */
  status?: ExpertStatus;
  /** 关联的 persona id — 召唤时切换到该 persona 的 Agent 会话 */
  personaId?: string;
  /**
   * 是否启用「书稿审阅」流程（提交书稿 + 生成深度审阅报告）。
   * 数据驱动开关：任何专家置 true 即拥有该能力，避免按 id 硬编码。
   */
  manuscriptReview?: boolean;
  /**
   * 专家的额外配置数据（各专家自由定义）。
   * 例：大纲专家可绑定 OUTLINE.md 路径，人物专家可绑定 CHARACTERS.md 路径。
   * 前端不直接消费此字段，由后端 Agent 按需读取。
   */
  metadata?: Record<string, any>;
}

export const EXPERTS: Expert[] = [
  {
    id: "editor",
    displayName: "编辑专家",
    icon: "pencil",
    description: "深度审阅你的小说稿件，逐章细抠，并产出完整的审阅报告。",
    status: "ready",
    capabilities: ["逐章深度审阅", "产出审阅报告", "人物/情节/文笔分析", "设定一致性核查"],
    personaId: "editor",
    manuscriptReview: true,
    systemPrompt:
      "你是一位资深文学编辑，尤其擅长历史小说。你会深度阅读用户的小说稿件，从人物一致性、情节与结构、文笔与语言、设定一致性等维度进行审阅，并在用户要求时产出结构化的审阅报告（含总体评价与逐章问题清单）。",
  },
  // —— 阶段4 路线图（即将上线，卡片已占位，召唤暂未启用）——
  {
    id: "outline",
    displayName: "大纲专家",
    icon: "file",
    description: "帮你搭建与打磨故事骨架：起承转合、节奏把控与悬念钩子。",
    status: "soon",
    capabilities: ["结构诊断", "章节节拍", "悬念设计"],
  },
  {
    id: "character",
    displayName: "人物专家",
    icon: "branch",
    description: "塑造立体角色：动机、弧光、关系网与对话一致性。",
    status: "soon",
    capabilities: ["人物小传", "关系图谱", "弧光设计"],
  },
  {
    id: "polish",
    displayName: "润色专家",
    icon: "sparkle",
    description: "在保留你文风的前提下，精修语句、节奏与语感。",
    status: "soon",
    capabilities: ["语感打磨", "冗余精简", "节奏优化"],
  },
  {
    id: "lore",
    displayName: "设定一致性专家",
    icon: "sliders",
    description: "跨章节追踪时间线、地名、官职等设定，揪出前后矛盾。",
    status: "soon",
    capabilities: ["时间线校验", "设定冲突检测", "名词统一"],
  },
];

export function getExpert(id: string): Expert | undefined {
  return EXPERTS.find((e) => e.id === id);
}
