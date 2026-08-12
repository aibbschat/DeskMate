#!/usr/bin/env python3
"""
Update MSApps open-source skills catalog for OpenWorker.

Usage:
  python scripts/update-msapps-skills.py
Generates/updates: coworker/skills/catalog.json
"""

import json
import sys
from pathlib import Path

# Cloned from: https://github.com/MSApps-Mobile/openwork-plugins
MSAPPS_REPO = "/tmp/openwork-plugins"
OUTPUT = Path(__file__).parent.parent / "coworker" / "skills" / "catalog.json"

# Category mapping
CAT_MAP = {
    "development": "development",
    "devops": "devops",
    "productivity": "productivity",
    "document": "document",
    "scraping": "document",
    "data": "document",
    "search": "search",
    "database": "database",
    "internationalization": "other",
    "google": "other",
    "marketing": "other",
    "ecommerce": "other",
    "communication": "other",
}

# Icon mapping
ICON_MAP = {
    # 开发优化
    "token-efficiency-audit": "📉",
    "agents-md-optimizer": "✂️",
    "claude-md-composer": "📝",
    "sosa-audit": "🔍",
    "sosa-governor": "⚖️",
    "sosa-orchestrator": "🎼",
    
    # 记忆/生产力
    "knowledge-agent": "🧠",
    "notion-memory": "🧠",
    "cowork-mem": "💾",
    "mem-search": "🔍",
    "timeline-report": "📊",
    "daily-cowork-backup": "💾",
    "weekly-work-plan": "📅",
    "smart-explore": "🔍",
    "prospect": "🔭",
    "make-plan": "📋",
    "digital-presence": "🌐",
    "toggl": "⏱️",
    
    # DevOps/健康检查
    "gcloud-cli-health-check": "☁️",
    "github-cli-health-check": "🐙",
    "chrome-health-check": "🌐",
    "disk-explorer": "💽",
    "vm-cleanup": "🧹",
    
    # 爬虫/数据
    "apify-scraping": "🕷️",
    "bulk-scrape": "🔽",
    "youtube-transcriber": "🎬",
    "x-insights": "📈",
    
    # 语言/IDE LSP
    "kotlin-lsp": "🟣",
    "swift-lsp": "🔶",
    
    # 工具/修复
    "fix-stuck-session": "🔧",
    "auto-symlink-project": "🔗",
    
    # 云/电商/邮件
    "google-drive-upload": "☁️",
    "shopify-partner-marketing": "🛒",
    "wp-woocommerce": "🛒",
    "zoho-mail-health-check": "📧",
    
    # 国际化
    "rtl-chat": "🌍",
    "rtl-chat-fixer": "🔄",
    
    # 集成
    "campfire": "🔥",
    "do": "✅",
    "whatsapp-messages": "💬",
}

def main():
    if not Path(MSAPPS_REPO).exists():
        print(f"ERROR: {MSAPPS_REPO} does not exist. Please git clone first:")
        print("cd /tmp && git clone https://github.com/MSApps-Mobile/openwork-plugins.git")
        sys.exit(1)
    
    skills_dir = Path(MSAPPS_REPO) / "skills"
    catalog = {
        "sources": [
            {
                "name": "MSApps 开源技能集市",
                "url": "https://github.com/MSApps-Mobile/openwork-plugins",
                "skills": []
            }
        ]
    }
    
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_json = skill_dir / "skill.json"
        if not skill_json.exists():
            continue
        
        with open(skill_json, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        # Map to OpenWorker format
        categories = data.get("categories", ["other"])
        category = CAT_MAP.get(categories[0] if categories else "other", "other")
        
        entry = {
            "id": data["id"],
            "name": data["name"],
            "description": data["description"].strip(),
            "category": category,
            "icon": ICON_MAP.get(data["id"], "🔧"),
            "version": data.get("version", "1.0.0"),
            "install": data["install"],
            "author": data.get("author", "MSApps"),
            "homepage": data.get("homepage", f"https://github.com/MSApps-Mobile/{data['id']}"),
        }
        # Config mapping
        if data.get("config"):
            cfg = data["config"]
            entry["config_type"] = cfg.get("type", "none")
            entry["config_key_name"] = cfg.get("key_name", "")
            entry["config_key_hint"] = cfg.get("key_hint", "")
            entry["config_env_key"] = cfg.get("env_key", "")
        
        # Cleanup description to one line
        desc = entry["description"]
        lines = [l.strip() for l in desc.splitlines() if l.strip()]
        entry["description"] = " ".join(lines)
        if len(entry["description"]) > 160:
            entry["description"] = entry["description"][:157] + "..."
        
        catalog["sources"][0]["skills"].append(entry)
    
    # Write output
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)
    
    print(f"✅ 更新完成: {len(catalog['sources'][0]['skills'])} 个技能写入 {OUTPUT}")

if __name__ == "__main__":
    main()
