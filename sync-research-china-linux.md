# 调研报告：主流「同一份文件多设备同步 + 协作」产品在【中国大陆 + Linux 服务器】下的可用性与冲突模型

- 调研时间：2026-10-01（Asia/Shanghai）
- 场景锚定：macOS 笔记本本地写作（.md/.docx/.txt + 图片素材，约 1–10GB）；腾讯云 Ubuntu 24.04 headless（大陆、40GB 盘、3.6GB 内存）跑 Python AI；云端 AI 需读稿、偶尔回传批注；笔记本可合盖；**不要**"桌面壳连云端"的方案。
- 标注约定：凡标 **[查证]** 的为我实际抓取到原文的官方/一手页面；**[URL]** 为我只确认页面存在、本环境未能取到正文；**[推断]** 为基于机制/社区实践的推断。

---

## 0. 结论（可直接落地）

### 值得用的 2 个
1. **Syncthing（主力，强烈推荐）**
   - 点对点、开源、无第三方云、无墙问题；服务器有公网 IP 时可直连，不依赖境内不通的国外中继。
   - 服务器端是纯后台服务，headless 完全可用（systemd 常驻）。
   - 冲突模型明确：冲突文件命名为 `<文件名>.sync-conflict-<日期>-<时间>-<设备ID>.<扩展名>` [查证]。
   - **可按文件夹设单向**：Mac 端 Send Only + 服务器端 Receive Only，天然满足"作者写、AI 只读"；AI 的回写放在另一个方向相反的文件夹，避免被回滚。
   - 自带文件版本化（Staggered）可兜底误删/误改。
2. **坚果云 Nutstore（兜底：云端副本 + WebDAV 给 AI）**
   - 国内厂商，速度有保障；**官方支持 Linux**（Ubuntu/Debian/Fedora）[查证]。
   - **官方 WebDAV**：服务器地址 `https://dav.jianguoyun.com/dav/`，用"账户信息→安全选项→添加应用密码"生成的专用密码 [查证]。headless 服务器可用 rclone / davfs2 直接挂载，无需跑 GUI。
   - 硬限制：**免费版每月上传 1GB、下载 3GB**；付费个人专业版 30GB（上传不限量）、高级版 72GB [查证]。1–10GB 首次全量同步免费版做不到，须付费。

### 建议架构（具体）
- Mac `~/Writing` ↔ 服务器 `/home/ubuntu/Writing`。
- 两个反向的单向文件夹：
  - `manuscript/`：Mac = **Send Only**，服务器 = **Receive Only**（作者→AI，AI 只读）。
  - `ai-feedback/`：服务器 = **Send Only**，Mac = **Receive Only**（AI 批注→作者）。
- 服务器端设 Staggered File Versioning；Mac/服务器都加 `.stignore` 排除 `.DS_Store` 等。
- AI 直接读 `/home/ubuntu/Writing/manuscript`（.md/.txt 直接读，.docx 用 python-docx，图片按路径），把批注写 `ai-feedback/`。
- 可选叠加：付费坚果云对同一份稿子做云端副本/历史版本，WebDAV 只读挂给 AI 当第二数据源。

### 直接排除（及一句话理由）
| 排除对象 | 理由 |
|---|---|
| Dropbox | 大陆被墙（Yale 明确列入不可用清单）[查证] |
| Google Drive / Google Docs | 大陆全系被墙 [查证]；且无 Linux 官方客户端 |
| OneDrive | 无官方 Linux 客户端；大陆个人版不稳、企业版走世纪互联，链路复杂 |
| iCloud Drive | 无 Linux 客户端；.icloud 占位/驱逐会让第三方读到"占位文件"而非内容；rclone 方式运维代价高 |
| Obsidian Sync | 必须跑完整 Obsidian（GUI），不能当 headless daemon；无大陆节点（最近新加坡） |
| Scrivener / Ulysses / Craft | Apple 生态或无 Linux；Scrivener 要求"一端关闭另一端才能开"，与"合盖+随时读"直接冲突 |
| Notion | 不是文件而是 block 数据库，Linux 无官方客户端，AI 只能走 API，不满足"读本地文件" |

---

## 1. 消费级网盘

### 1.1 对比表

| 产品 | 大陆可用性 | Linux 官方客户端 | headless 作"第二端" | 同步/冲突模型 | 笔记本合盖时 | 运维负担 | 结论 |
|---|---|---|---|---|---|---|---|
| **Syncthing** | 好（P2P；服务器公网 IP 可直连；公共中继/STUN 在国内可能不通）[查证/推断] | 有（官方多平台，apt 可装）[查证] | ✅ 原生后台服务 | 文件级；冲突生成 `.sync-conflict-…`；大小写冲突会报错；可单向 | 服务器保持最后已同步状态；Mac 合盖期间不上传，不丢已同步内容 | 低（apt + systemd + 防火墙） | **采用** |
| **坚果云 Nutstore** | 好（国内）[查证] | 有（Ubuntu/Debian/Fedora，GUI）[查证] | ✅ 走官方 WebDAV（rclone/davfs2），无需 GUI [查证] | 文件级，冲突生成副本（细节未取到正文）[推断] | 云端保持最后版本 | 低–中 | **采用（付费）** |
| iCloud Drive | 大陆 iCloud 由云上贵州运营、Web 可用 [URL] | ❌ 无 | ⚠️ 仅 rclone iclouddrive（Tier 4） | iCloud 版本冲突由 App 用 NSFileVersion 自行处理 [查证]；.icloud 占位是硬坑 | 云端保存；Mac 端可能只剩占位 | 高（2FA、token 30 天过期） | **排除** |
| Dropbox | ❌ 被墙 [查证] | 有（headless CLI/AUR）[查证] | ⚠️ 可以，但网络不通 | conflicted copy / case conflict [URL] | 同上 | — | **排除** |
| OneDrive | 个人版可达但慢/不稳；企业版=世纪互联 [推断] | ❌ 无官方（第三方 abraunegg/onedrive、rclone） | ⚠️ 第三方可行 | 冲突生成 "Copy" 副本 [查证] | 云端保存 | 中高 | **优先级最低** |
| Google Drive | ❌ 全系被墙 [查证] | ❌ 无 | ❌ rclone 有后端但网络不通 | Google Docs 用 OT 合并；二进制保留副本 | — | — | **排除** |

### 1.2 关键事实与引用
- **iCloud 无 Linux 客户端**：Apple 官方系统需求只覆盖 Apple 设备与 Windows；第三方方案才有 Linux（如 snap 的 icloud-for-linux、GitHub 的 icloud-linux，均非官方）。[推断/常识] 参考 https://support.apple.com/118308
- **.icloud 占位符（最致命）**：Apple 用 `.icloud` 扩展名表示"文件尚未下载到本机"，Finder 双击才按需下载；**若第三方同步工具同步了 `.icloud` 占位文件而没有触发下载，其它端会看到占位文件而不是真正内容** [查证]：https://pdfviewer.io/faq/unable-to-open-files-with-pdf-icloud-extension 。这正是"服务端读到 0 字节/打不开"的来源。解决只有关掉"优化 Mac 储存空间"。Macworld 同题说明：https://www.macworld.com/article/2478934/how-to-keep-an-icloud-drive-file-on-your-mac-while-optimizing-storage.html
- **rclone 可访问 iCloud Drive，但代价高**：认证用 Apple ID 密码 + 2FA，**不接受 App 专用密码**；trust token **30 天**过期需重新认证；启用高级数据保护(ADP)时可能需要受信设备批准 PCS cookie；Tier 4 支持等级 [查证]：https://rclone.org/iclouddrive/
- **iCloud 中国大陆数据由云上贵州运营** [URL]：https://support.apple.com/zh-cn/111754
- **Dropbox 在大陆被墙**：耶鲁 IT 的"不可用清单"在 Email/协作类里明确列出 "Gmail, Google Drive, Google Docs, Google Calendar (all Google services), Zoom, **Dropbox**, Slack" [查证]：https://its.yale.edu/internationally-blocked-websites-and-applications
- **Dropbox 的 Linux 现状**：有 GNU/Linux 客户端，可"按官网说明做 headless 命令行安装"，另有 dropbox-cli；GUI 依赖托盘（tray），服务器上无意义 [查证]：https://wiki.archlinux.org/title/Dropbox 。官方 Linux 说明页：[URL] https://help.dropbox.com/installs/linux-commands 。历史上 2018 年曾砍掉除 ext4 外的文件系统支持，后部分恢复 [查证]：https://www.storagenewsletter.com/2018/08/29/dropbox-dropping-support-for-all-linux-file-systems/
- **Dropbox 冲突命名与大小写冲突**（官方文档存在，本环境 fetch 被拒，仅确认 URL）：[URL] https://help.dropbox.com/organize/conflicted-copy 、[URL] https://help.dropbox.com/organize/case-conflict
- **OneDrive 无官方 Linux 客户端**：微软 Q&A 里用户普遍用第三方；主流第三方是 https://github.com/abraunegg/onedrive （另有 rclone 的 onedrive 后端 https://rclone.org/onedrive/）。[推断/查证]
- **OneDrive 冲突表现**：同文件多端/多应用冲突会生成带 "Copy" 的副本 [查证]：https://learn.microsoft.com/en-nz/answers/questions/5259425/saving-excel-file-to-onedrive-is-creating-a-file-w
- **OneDrive 中国版由世纪互联(21Vianet)运营**，与个人版/国际版是两套体系 [URL/推断]：https://siwei.li/how-to-register-for-onedrive-operated-by-21vianet/
- **Google 全系被墙**：同上 Yale 清单 [查证]。Google Drive 无 Linux 官方客户端，只有 rclone 等第三方 [推断/常识]。
- **坚果云**：全平台自动同步，客户端支持 Windows/macOS/**Linux(Ubuntu/Debian/Fedora)**/iOS/Android [查证]：https://help.jianguoyun.com/?p=416 。WebDAV 服务器地址 `https://dav.jianguoyun.com/dav/` + 应用密码 [查证]：https://help.jianguoyun.com/?p=3168 。价格/配额：免费 上传 1GB/月、下载 3GB/月；专业版 30GB+、上传不限量；高级版 72GB+ [查证]：https://www.jianguoyun.com/s/pricing 。Linux 客户端下载：https://www.jianguoyun.com/s/downloads/linux

---

## 2. 写作向产品

### 2.1 对比表

| 产品 | 数据形态 | 多设备/离线 | Linux 服务器可读? | 冲突处理 | 结论 |
|---|---|---|---|---|---|
| **Obsidian Sync** | 本地 vault（.md 等） | 官方同步，多端 | ❌ 需跑 Obsidian GUI；远端是加密仓库，不是服务器文件系统 | Markdown 用 diff-match-patch 自动合并；其它文件 last-modified-wins；1.9.7 起可选"生成冲突文件" | 人-人同步优秀，**不适合"服务器 AI 读文件"** |
| **Obsidian + 第三方盘(坚果云/WebDAV)** | 本地 vault 明文 | 取决于同步层 | ✅ 只要盘能在 Linux 挂上，AI 直接读 .md | 取决于同步层（副本/冲突文件） | ✅ 与 Syncthing/坚果云组合最实用 |
| **Scrivener + Dropbox/iCloud** | `.scriv` 包（内部很多小文件） | Mac/Win/iOS | ❌ 无 Linux | 官方要求：**必须一端关闭另一端才能打开**，否则警告可能丢数据 | **排除**（与"合盖+随时读"冲突） |
| **Notion** | block 数据库 | 桌面/移动；2.53(2025-08-19) 起离线模式 | ❌ 无官方 Linux 客户端；只能 API | 自有 rich-text 冲突解决（服务端） | **排除**（不是文件） |
| **Ulysses** | 库+sheet，同步走 iCloud | Mac/iOS | ❌ 无 Linux | iCloud 同步 | **排除** |
| **Craft** | 文档数据库，Craft 云同步 | macOS/iOS/Windows/Web/Android | ❌ 无 Linux 原生 | Craft 自有同步 | **排除**（Linux 无原生） |

### 2.2 关键事实与引用
- **Obsidian Sync 服务器区域**：DigitalOcean，区域为 Automatic / **Asia=新加坡** / Europe=法兰克福 / North America=旧金山 / Oceania=悉尼，**无中国大陆节点** [查证]：https://obsidian.md/help/sync/region
- **Obsidian Sync 冲突规则**：Markdown 用 Google diff-match-patch 自动合并；**其它文件（含 canvas、附件、docx 等）采用 last-modified-wins**；自 Obsidian 1.9.7 可在设置里选"自动合并 / 生成冲突文件"，且该设置**每台设备都要单独设** [查证]：https://obsidian.md/help/sync/troubleshoot
- **Obsidian Sync 需要 Obsidian 应用在跑**（桌面/移动），没有独立 headless daemon [查证/推断]：https://obsidian.md/help/sync/setup
- **Scrivener 云同步的官方警告**：项目同步到云时"必须在一台设备上关闭后再在另一台上打开，否则会提示项目可能已在另一台电脑打开，可能丢数据"；关闭时不要立刻睡眠/关机，否则可能只同步一半 [查证]：https://www.literatureandlatte.com/blog/syncing-scrivener-projects-to-the-cloud
- **Notion 离线模式**：2025-08-19 的 2.53 版本上线，桌面与移动 App 可用（Recents/Favorites 自动离线，付费版）[查证]：https://www.notion.com/releases/2025-08-19 ；实现细节（SQLite 持久层、rich-text 冲突解决）见 2025-12-11 工程博客 https://www.notion.com/blog/how-we-made-notion-available-offline
- **Notion 无 Linux 官方客户端**，Linux 只能用浏览器/Web App [查证]：https://itsfoss.com/installing-notion-linux/
- **Ulysses**：同步走 iCloud 账号，版本要求 iOS/iPadOS 26+、macOS 15.4+ [查证]：https://help.ulysses.app/faqs
- **Craft 平台**：macOS/iOS/Windows/Web/Android，**无 Linux 原生** [查证]：https://support.craft.do/en/introduction/platforms

---

## 3. 协作模型：OT / CRDT / 文件级同步 / 加锁

| 模型 | 代表 | 适用条件 | 对"作者写、AI 只读"的非对称场景 |
|---|---|---|---|
| **OT（服务端变换）** | Google Docs | 必须实时在线、内容在服务端、客户端懂 OT | ❌ 文件不是 OT 对象；且 Google 大陆不通 |
| **CRDT（无冲突复制数据类型）** | Notion 离线、Obsidian LiveSync(CouchDB/Yjs) | 客户端需 CRDT 感知；数据存数据库/服务端 | ❌ 服务器要读的是 CouchDB 记录而非 .md 文件，反而增加一层解析；对"只读方"是过度设计 |
| **文件级同步** | **Syncthing**、坚果云、Dropbox、OneDrive | 任意文件类型；冲突时生成副本 | ✅ **最省事**：AI 只是文件系统读者；用单向文件夹即可避免回写 |
| **加锁编辑** | Scrivener 的"独占打开"、WebDAV lock | 需要客户端配合、网络稳定 | ❌ 笔记本合盖即"持锁离线"，直接违背场景 |

**推荐**：**文件级同步 + 单向文件夹**。AI 作为纯读者不需要 CRDT；CRDT 解决的是"多方同时编辑同一文本"，这个场景里根本不存在（只有作者本人写）。真正要防的是"AI 把只读目录改了导致回滚/冲突"，用 Syncthing 的 Receive Only 就解决。Obsidian LiveSync 的冲突解决设计可参考（自建 CouchDB，冲突策略可配置）[URL]：https://github.com/vrtmrz/obsidian-livesync

---

## 4. 关键坑（含落地对策）

1. **冲突副本命名不统一**，AI 读目录时必须过滤：
   - Syncthing：`<filename>.sync-conflict-<date>-<time>-<modifiedBy>.<ext>`，且冲突文件会像普通文件一样继续传播 [查证]：https://docs.syncthing.net/users/syncing.html
   - Dropbox：conflicted copy [URL]：https://help.dropbox.com/organize/conflicted-copy
   - Obsidian："Create conflict file" 选项 [查证]：https://obsidian.md/help/sync/troubleshoot
   - OneDrive：带 "Copy" 的副本 [查证]：见 1.2
2. **.icloud 驱逐占位** → 服务端读到占位/0 字节（见 1.2）[查证]。用 Syncthing/坚果云就绕开了 iCloud；若坚持 iCloud，必须关闭"优化 Mac 储存空间"。
3. **大小写不敏感路径**：macOS 默认大小写不敏感，ext4 敏感。Syncthing 明确：远端两个仅大小写不同的路径无法同步到 macOS/Windows 端，会报 **case conflict**；反之亦然 [查证]：https://docs.syncthing.net/users/syncing.html 。对策：命名杜绝"仅大小写不同"。
4. **.DS_Store / ._ 资源分叉**：必须排除，否则制造无意义冲突与噪音。Syncthing 用 `.stignore`，支持 `(?d)` 前缀（允许在被删目录清理时删除被忽略文件）[查证]：https://docs.syncthing.net/users/ignoring.html
5. **中文/Unicode 文件名 NFD vs NFC**：macOS 倾向以分解形(NFD)保存文件名，Linux 按原始字节处理，跨端可能出现"同名不同码点"→ 重复文件或无法同步。社区有专门工具（nfd2nfc）和 issue 讨论 [URL/部分推断]：https://github.com/elgar328/nfd2nfc 、https://github.com/opencloud-eu/web/issues/1261 。对策：尽量用常规汉字命名、避免重音/生僻组合字符；上线前做一次文件名 NFC 规范化。
6. **单向文件夹的"回写被回滚"**：服务器端对 Receive Only 目录的本地修改不会被分发，且界面会提示 "Revert Local Changes"，点了就撤销本地改动 [查证]：https://docs.syncthing.net/users/foldertypes.html 。所以 **AI 的批注必须写到另一个反向的单向文件夹**，不能写进只读稿目录。
7. **Syncthing 在大陆的发现/中继**：公共 STUN/中继在国内可能不通或很慢（社区仍在请求增加中国大陆 STUN 节点）[查证]：https://github.com/syncthing/syncthing/issues/9842 。但腾讯云服务器有公网 IP，属"一端公网"，可直连：Mac 端把该设备地址显式配成 `tcp://<服务器公网IP>:22000`，必要时自建中继。**这一点是本方案能否成立的关键，"能直连"是推断，需在你们实际网络里验证**。
8. **版本目录膨胀**：Syncthing 文件版本化默认存 `.stversions`，1–10GB 稿子 + 版本会在 40GB 盘上累积 [查证]：https://docs.syncthing.net/users/versioning.html 。对策：设 Staggered + 清理周期，或只对文稿目录开版本化。
9. **二进制文件无法 merge**：.docx 是 zip 容器，任何文件级同步都只能整文件取胜负（Obsidian 明说非 Markdown=last-modified-wins）[查证]。AI 回传批注建议用独立小文件（.md/.txt/json），不要直接改 .docx。

---

## 5. 落地步骤（具体命令）

### 5.1 服务器（Ubuntu 24.04 headless）
```
sudo apt update && sudo apt install -y syncthing
# 以某个用户身份常驻（Debian/Ubuntu 包自带模板单元）[查证] autostart 文档
sudo systemctl enable --now syncthing@ubuntu
systemctl status syncthing@ubuntu
# 或用户级：
# systemctl --user enable --now syncthing
```
- 腾讯云安全组 / ufw 放行：**TCP 22000、UDP 22000（QUIC）、UDP 21027（本地发现）**；Web GUI 8384 只监听 `127.0.0.1`，不要暴露公网。文档：https://docs.syncthing.net/users/firewall.html
- 自动启动文档：https://docs.syncthing.net/users/autostart.html

### 5.2 Mac 端
```
brew install syncthing          # 或安装原生 app: https://github.com/syncthing/syncthing-macos
```
- 首次运行给全盘访问/文件访问权限（macOS 隐私设置）。

### 5.3 共享设置
- 服务器目录 `/home/ubuntu/Writing/manuscript` ← Mac `~/Writing/manuscript`：Mac 端 **Send Only**，服务器 **Receive Only**。
- 服务器目录 `/home/ubuntu/Writing/ai-feedback` → Mac `~/Writing/ai-feedback`：服务器 **Send Only**，Mac **Receive Only**。
- Mac 端为该设备地址显式填 `tcp://<服务器公网IP>:22000`。
- 打开 **Staggered File Versioning**（至少服务器端）。
- 双向都放 `.stignore`：
```
(?d).DS_Store
(?d)._*
(?d).Trash
(?d).Trashes
(?d)Icon?
(?d).stversions
*.tmp
*.swp
~$*
```

### 5.4 坚果云 WebDAV 挂载（headless 备用/云端副本）
```
# 1) 浏览器登录坚果云 → 账户信息 → 安全选项 → 添加应用密码，生成专用密码
# 2) 服务器上 rclone 配置 WebDAV
rclone config
#   name   = nutstore
#   storage= webdav
#   url    = https://dav.jianguoyun.com/dav/
#   vendor = other
#   user   = 你的坚果云账号
#   pass   = 上一步的应用密码
# 3) 只读挂载给 AI（本地缓存加速）
rclone mount nutstore:/写作素材 /mnt/nutstore --read-only --vfs-cache-mode full --vfs-cache-max-size 5G
```
- 端点与"应用密码"官方说明：https://help.jianguoyun.com/?p=3168
- 免费额度/付费档位：https://www.jianguoyun.com/s/pricing
- rclone WebDAV 后端：https://rclone.org/webdav/

---

## 6. 我查证过 vs 我推断的

**实际抓到正文（[查证]）**：Obsidian Sync 的区域/安全/故障排查/设置页；Syncthing 的 syncing/foldertypes/versioning/ignoring/autostart 文档；rclone 的 iclouddrive/onedrive/webdav 文档；Yale 大陆被墙清单；ArchWiki Dropbox；Apple TN2336（iCloud 版本冲突）；Notion 2.53 发布说明与离线工程博客；Craft 平台页；Ulysses FAQ；Scrivener 云同步博客；坚果云帮助 p=416 / p=3168 / 价格页；pdfviewer 的 .icloud FAQ；opencloud Unicode 规范化 issue；百度/Notion Linux 等社区文章。

**只确认 URL 存在、未能取到正文（[URL]）**：Dropbox 官方三页（help.dropbox.com 的 linux-commands / conflicted-copy / case-conflict，本环境 fetch 被拒）；Apple iCloud 系统需求页与大陆 iCloud 页（仅搜索命中）。

**推断（未直接证实）**：大陆网络下 Obsidian Sync（新加坡）/ Notion 的可达性与延迟；OneDrive 个人版在大陆的稳定性；坚果云冲突副本的具体命名；Syncthing 公网直连在你们两地网络的实际连通性；40GB 盘/3.6GB 内存的体感（机制上足够，1–10GB 数据 + 版本目录需监控）。

---

## 7. 参考链接汇总
- Syncthing 冲突：https://docs.syncthing.net/users/syncing.html
- Syncthing 文件夹类型（单向）：https://docs.syncthing.net/users/foldertypes.html
- Syncthing 版本化：https://docs.syncthing.net/users/versioning.html
- Syncthing 忽略规则：https://docs.syncthing.net/users/ignoring.html
- Syncthing 自动启动：https://docs.syncthing.net/users/autostart.html
- Syncthing 中国 STUN issue：https://github.com/syncthing/syncthing/issues/9842
- 坚果云全平台同步：https://help.jianguoyun.com/?p=416
- 坚果云 WebDAV 设置：https://help.jianguoyun.com/?p=3168
- 坚果云价格：https://www.jianguoyun.com/s/pricing
- 坚果云 Linux 下载：https://www.jianguoyun.com/s/downloads/linux
- rclone WebDAV：https://rclone.org/webdav/
- rclone OneDrive：https://rclone.org/onedrive/
- rclone iCloud Drive（Tier 4/限制）：https://rclone.org/iclouddrive/
- .icloud 占位符说明：https://pdfviewer.io/faq/unable-to-open-files-with-pdf-icloud-extension
- 大陆被墙清单（Yale）：https://its.yale.edu/internationally-blocked-websites-and-applications
- Dropbox on Linux（ArchWiki）：https://wiki.archlinux.org/title/Dropbox
- Dropbox Linux 官方页：https://help.dropbox.com/installs/linux-commands
- Dropbox conflicted copy：https://help.dropbox.com/organize/conflicted-copy
- Dropbox case conflict：https://help.dropbox.com/organize/case-conflict
- Obsidian Sync 区域：https://obsidian.md/help/sync/region
- Obsidian Sync 冲突处理：https://obsidian.md/help/sync/troubleshoot
- Notion 2.53 离线：https://www.notion.com/releases/2025-08-19
- Notion 离线实现：https://www.notion.com/blog/how-we-made-notion-available-offline
- Scrivener 云同步官方警告：https://www.literatureandlatte.com/blog/syncing-scrivener-projects-to-the-cloud
- Craft 平台：https://support.craft.do/en/introduction/platforms
- Ulysses FAQ：https://help.ulysses.app/faqs
- OneDrive Linux 第三方：https://github.com/abraunegg/onedrive
- Apple iCloud 版本冲突 TN2336：https://developer.apple.com/library/archive/technotes/tn2336/_index.html
- macOS/Linux 文件名 NFD 工具：https://github.com/elgar328/nfd2nfc
