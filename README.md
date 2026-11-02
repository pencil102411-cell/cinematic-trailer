# cinematic-trailer

**影视预告片全流程**：从故事大纲、剧本或分镜脚本出发，一路做到可交付的预告片——钩子与结构、镜头语言、切点与转场、配乐与台词、字卡动效、图像与视频素材生成方案、animatic 粗剪和后期。核心做法是"给声音配画面"：先定音乐和台词，再按拍切画面。

规律来自对五支预告片的逐帧分析：《铁证》《甜蜜家园》《奥本海默》《剑来》和一支好莱坞套路模板。切点、黑场、闪白、叠化、运镜、响度、冲击、静音、节拍和台词时间都用脚本算过，结论和硬数字写在 [references/09-样片拆解.md](references/09-样片拆解.md)。

采用 [Agent Skills](https://agentskills.io) 开放格式：Claude Code、Codex 等支持该格式的 agent 可以直接安装；不支持技能的平台也能当系统提示词使用。

## 能做什么

| 任务 | 交付 |
| --- | --- |
| 立项与钩子 | 一句话钩子、核心问题、世界规则、转折点、主题句、剧透红线 |
| 预告片蓝图 | 节奏型（加速 / 前慢后爆 / 恒速 / 高速卡点）、时间预算、声音剧本、音乐形状、高潮装置、尾钩、色彩弧线 |
| 分镜表 | 逐镜时长、景别、运镜、画面、转场、字卡、字幕、声音，CSV 可直接喂给 animatic 脚本 |
| 配乐 | Suno 出版与选版、按小节剪长度、打点、按拍改镜头时长 |
| 图像素材 | 素材清单、设定卡与关键帧方案（MJ / GPT-image-2） |
| 视频素材 | 生成清单与 Seedance 2.5 / MiniMax H3 提示词，每条写清成片取段；制作级别 L1–L4（能剪辑、能静图做的不生成视频）、跑法选择、避坑清单 |
| 生成以后 | 小测、看片、挑种子、按动作实际时间找入点、挑片结果记录、按镜号逐刀返修（剪辑 / 重跑 / 重出图 / 换模型，先做不花钱的） |
| animatic 与粗剪 | 分镜表一键出片：图片缓推、视频按入点截取、占位卡、字卡、转场、黑闪 / 故障 / 冲推、字幕、遮幅、配乐混音；开剪前检查；镜头号版给返修用 |
| 剪辑小工具 | 点名蒙太奇（越切越快、推镜残影）、频闪、冲推、甩镜、故障、口吃剪、截参考音频、逐帧检查图 |
| 自查 | 蓝图评分表、分镜检查清单（含"用户看片最常挑的毛病"） |
| 字卡与片名动效 | fade / slam / glitch / sweep / split 五种预设，可透明底导出叠进剪辑 |
| 后期 | DaVinci 剪辑调色混音、AE / Remotion / Blender 字卡，响度与交付规格 |
| 拆解参考片 | 镜头表、平均镜头长、分段节奏、运镜分布、声音事件、接触表和时间线图 |

本仓库只提供方法、模板和本地脚本，不调用任何生成服务。

## 安装

仓库根目录就是技能目录（`SKILL.md` + `references/` + `assets/` + `scripts/`）。克隆到所用 agent 的技能目录，文件夹名保持 `cinematic-trailer`。

| Agent | 技能目录 |
| --- | --- |
| Claude Code | `~/.claude/skills/`（只给当前项目用：项目内 `.claude/skills/`） |
| Codex | `$CODEX_HOME/skills/`，默认 `~/.codex/skills/` |
| 其他支持 Agent Skills 的工具 | 各工具的 skills 目录，以其文档为准 |

Windows PowerShell（以 Claude Code 为例；Codex 把 `.claude` 换成 `.codex`）：

```powershell
git clone https://github.com/pencil102411-cell/cinematic-trailer.git "$HOME\.claude\skills\cinematic-trailer"
```

macOS / Linux：

```bash
git clone https://github.com/pencil102411-cell/cinematic-trailer.git ~/.claude/skills/cinematic-trailer
```

安装后新开一个会话生效。以后更新：进入该目录执行 `git pull`。

**脚本依赖**：Python 3.10+，`ffmpeg` / `ffprobe` 在 PATH，以及

```bash
pip install numpy opencv-python scipy pillow
```

台词转写另需 `pip install faster-whisper`（可选）。只用方法和模板、不跑脚本的话可以不装。

**不支持技能的平台**（ChatGPT 自定义 GPT、扣子 Coze、Dify 等）：把 `SKILL.md` 里第二个 `---` 之后的正文粘贴为系统提示词，`references/` 和 `assets/` 下的文件上传为知识库。脚本需要本地运行。

## 搭配的技能

本 skill 管预告片的结构、节奏和后期，单张图像和视频提示词的细写法交给专门的技能：

| 环节 | 技能 |
| --- | --- |
| 角色设定卡、带主角脸的关键帧（GPT-image-2） | [character-scene-asset-prompts](https://github.com/pencil102411-cell/character-scene-asset-prompts) |
| 氛围空镜、奇观、纹理底板（Midjourney） | mj-cinematic-prompts |
| Seedance 横屏真人提示词 | horizontal-cinematic-prompt-agent-v2 |
| 已生成镜头的局部修改 | video-local-edit |
| 无剧情的纯氛围短片 | mood-short-film |

没装这些技能时，本 skill 仍能给出素材清单和提示词要点。

## 使用示例

直接描述需求即可自动触发。也可以显式调用：Claude Code 输入 `/cinematic-trailer`，Codex 输入 `$cinematic-trailer`。

```text
我有一部仙侠剧的大纲（附后），帮我做一支 90 秒的正式预告：先给钩子五行，确认后排蓝图和分镜表。
```

```text
这是我的 40 镜分镜脚本，改成一支 45 秒的先导预告，不要推倒重写，挑镜头、改顺序、压时长。
```

```text
配乐定了（附 wav），打点后把分镜表里的字卡、片名和点名蒙太奇改到拍上，再出一版 animatic。
```

```text
给片名《剑来》做一个 slam 砸入的字卡，副标 THE ONE，3 秒，要透明底能叠进 DaVinci。
```

```text
拆解一下这支参考预告片（附 mp4）的剪辑节奏和配乐，我想模仿它的第三幕。
```

## 脚本

| 脚本 | 用途 |
| --- | --- |
| [scripts/analyze_trailer.py](scripts/analyze_trailer.py) | 拆解参考预告片：镜头表、ASL、分段节奏、运镜、闪白 / 黑场 / 叠化、响度 / 冲击 / 静音、接触表、时间线图 |
| [scripts/music_markers.py](scripts/music_markers.py) | 配乐打点：BPM、拍、小节、冲击、静音、段落；导出 CSV、DaVinci 标记 EDL、AE 脚本、JSON、总览图 |
| [scripts/animatic.py](scripts/animatic.py) | 分镜表 → animatic / 粗剪，同时导出 SRT 和带时间码的剪辑表；`--label` 出镜头号版，`--check` 只做开剪前检查，时长可以按拍写 |
| [scripts/title_card.py](scripts/title_card.py) | 字卡动效：五种预设、纹理填字、背景图 / 视频、ProRes 4444 透明导出 |
| [scripts/fx.py](scripts/fx.py) | 剪辑小工具：抽帧 / 逐帧检查图、点名蒙太奇（越切越快、推镜残影）、频闪、冲推、甩镜、故障、口吃剪、截参考音频 |

每个脚本的参数、分镜表列名和转场写法见各自文件头；用法速查在 [SKILL.md](SKILL.md) 末尾。

## 文件说明

| 文件 | 用途 |
| --- | --- |
| [SKILL.md](SKILL.md) | 入口：样片硬数字、工具分工、流程地图、每段要点、默认值、项目文件夹约定、脚本速查 |
| [references/01-结构与节奏.md](references/01-结构与节奏.md) | 三幕结构、时间比例、节奏型、高潮装置、尾钩 |
| [references/02-镜头语言.md](references/02-镜头语言.md) | 景别、运镜、构图在预告片里的用法 |
| [references/03-切点与转场.md](references/03-切点与转场.md) | 按声音下刀、转场库 |
| [references/04-声音-配乐与配音.md](references/04-声音-配乐与配音.md) | 配乐、音效、台词、旁白四层与 Suno 用法 |
| [references/05-文字与字幕动效.md](references/05-文字与字幕动效.md) | 字卡清单、片名、字幕与动效 |
| [references/06-素材-图像.md](references/06-素材-图像.md) | 资产层级、设定卡与首帧图（MJ / GPT-image-2）、什么时候一张图就够 |
| [references/07-素材-视频.md](references/07-素材-视频.md) | Seedance 2.5 / MiniMax H3 生成方案与取段、制作级别、"四个一"、跑法选择、锚点句、避坑清单 |
| [references/08-后期剪辑与合成.md](references/08-后期剪辑与合成.md) | 剪辑、合成、调色、混音、导出规格、重新编码保画质、水印 |
| [references/09-样片拆解.md](references/09-样片拆解.md) | 五支样片的数据总表和逐段拆解 |
| [references/10-生成以后.md](references/10-生成以后.md) | 小测、看片、挑种子、找入点、粗剪两版、按镜号逐刀返修 |
| `references/本机工具链.md` | 可选的本机文件（已写进 .gitignore，不入库）：记你自己的工作站地址、画布命令行、超分和转写工具路径，skill 要真正跑东西时读 |
| [assets/预告片蓝图模板.md](assets/预告片蓝图模板.md) | 立项与蓝图模板（含 AI 制作路线） |
| [assets/分镜表模板.csv](assets/分镜表模板.csv) | 分镜表列定义，animatic.py 直接读 |
| [assets/视频提示词模板.txt](assets/视频提示词模板.txt) | 视频提示词文件模板 |
| [assets/挑片结果模板.md](assets/挑片结果模板.md) | 每批生成的挑片记录：用哪条、入点、为什么、毛病、返修意见 |
| [assets/进度记录模板.md](assets/进度记录模板.md) | 项目进度记录：做到哪、每镜用哪条，新开对话接着做 |
| [rubrics/蓝图评分.md](rubrics/蓝图评分.md) | 蓝图自评（满分 100） |
| [rubrics/分镜检查清单.md](rubrics/分镜检查清单.md) | 分镜表和提示词交付前、每轮返修后的检查清单 |

## 验证边界

样片数字是机器检测结果，叠化和字卡段会影响镜头数，只作量级参考。模型的时长、分辨率、首尾帧和参考素材上限以你实际用的入口为准。脚本在 Windows 上开发和测试，字体默认找 `C:/Windows/Fonts` 下的中文字体，其他系统用 `--font` / `--sub-font` 指定。本仓库不包含参考预告片的视频、抽帧和拆解原始数据。
