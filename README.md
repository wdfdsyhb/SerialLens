# SerialLens · AI 串口侦探

> 传统串口助手只「显示」数据，SerialLens 会「看懂」数据。

SerialLens 是一个给嵌入式学生和硬件工程师的串口调试助手：
在普通串口收发之上，叠加**自动波特率检测**、**协议启发式识别**和**AI 分析**，
把「一屏乱码」变成「这是什么协议、帧长什么样、下一步该干什么」。

```text
$ seriallens demo

┌────────────────────────────── 本地启发式识别 ───────────────────────────────┐
│ NMEA 0183（GPS/北斗等导航语句）（置信度 95%）— 3/3 行以 $/! 开头；           │
│ NMEA 校验和 2/2 通过； Talker ID: GP                                        │
└─────────────────────────────────────────────────────────────────────────────┘
```

## 为什么需要它

每个嵌入式学生的第一次翻车几乎都一样：

- 接上模块，屏幕全是乱码 —— 不知道该试哪个波特率
- 买了个传感器，只给了「见通信协议附录」，对着 HEX 流肉眼猜帧格式
- 模块明明在发数据，串口助手里却什么也看不懂

SerialLens 把这些苦力活交给软件启发式 + 大模型：

| 能力 | 怎么做到 |
| --- | --- |
| 自动波特率 | 逐档试收 + 可读性打分（可打印率、行完整性、校验和通过率） |
| 协议识别 | NMEA 0183 校验和验证 / JSON 解析 / 常见帧头统计 / 文本判别 |
| AI 分析 | 采样 + 启发式结论一起喂给大模型，输出协议判断、帧结构推测、异常原因、下一步建议 |
| 报告存档 | 一键导出 Markdown，可直接贴到帖子/Issue 里求助 |

## 快速开始

```bash
pip install seriallens        # 发布后；开发版: pip install -e .
```

**没有硬件？30 秒体验：**

```bash
seriallens demo               # 合成 GPS 数据流，完整走一遍识别管线
seriallens demo --garbled     # 波特率不匹配的乱码场景（按位错位合成，不是假装的）
seriallens demo --detect      # 顺带演示自动波特率扫描
```

**接上真实设备：**

```bash
seriallens ports              # 列出系统串口
seriallens detect COM3        # 不知道波特率？先扫一遍
seriallens watch COM3 -b 115200        # 实时监视（HEX/文本双视图）
seriallens analyze COM3 -b 9600 --ai   # 采样 + AI 分析 -> Markdown 报告
```

## 协议学习模式（v0.2 新增）

SerialLens 的招牌功能：对着不明设备抓几帧、标注一两个你已知的真值，
它自动推断出**帧头、长度规则、字段偏移/类型/字节序/比例尺**，生成可复用的解码画像。

```bash
# 无硬件演示：内置虚拟温湿度传感器，多轮采样自动改变读数
seriallens learn --demo-sensor --rounds 5 --name my-sensor

# 真实设备：每轮采样前改变读数（如按一下按键、对着温度传感器哈口气）
seriallens learn COM3 -b 9600 --rounds 5 --gap 5 \
    --truth "温度=25.3" --truth "湿度=60" --name my-sensor

# 之后用画像实时解码——乱码从此变成物理量
seriallens watch COM3 -b 9600 --profile my-sensor
# ● 温度=26.1  湿度=58.0
# ● 温度=26.2  湿度=58.2
```

画像保存在 `~/.seriallens/profiles/<名字>.json`，是**可纠正的假设**：
每个字段带置信度与标注来源，推断不对直接改 JSON 重用。

工作原理（纯算法，离线可复现）：

1. **帧切分**：行式 / 帧头发现（等间距重复前缀）/ 定长
2. **逐字节对齐**：多帧对比，恒定字节 = 帧头/ID/填充，变化字节 = 候选字段
3. **真值绑定**：遍历 (类型 × 字节序 × 比例尺) 组合，精确匹配你标注的真值
4. **覆盖推断**：量程窄导致部分字节恰好恒定时，二次滑动窗口补绑
5. 绑定不上的动态字节标「动态-未知」，不编造语义 —— 交给 AI 或人工确认

## 实测对照：纯 LLM 推理 vs SerialLens 真值绑定

我们做了一次对照实验（v0.2 开发期间，2026-09）：同一份 4 帧采样
（`AA55 | len | temp u16le ×0.01 | humi u16be ×0.1 | seq | CRC8` 的虚拟传感器流），
分别交给「纯大模型推理」和「SerialLens 学习模式」。

**给 AI 的输入**：HEX 视图 + 启发式已知条件（帧头 AA55 等间距 9 字节）+ 环境变化提示。
**给 SerialLens 的输入**：同样 4 帧 + 2 个真值标注（温度 25.3°C、湿度 60%）。

| 结果 | 纯 LLM（deepseek-chat 系，API） | SerialLens v0.2 learn |
| --- | --- | --- |
| 帧头 / 帧长 | ✅ 对 | ✅ 对 |
| 长度字节 | ✅ 对（猜测） | ✅ 对（含 `byte@2+5` 规则） |
| 温度字段位置+编码 | ❌ 错（误判为序号） | ✅ 精确：`u16le@3 ×0.01` |
| 湿度字段位置+编码 | ❌ 错（被巧合诱导） | ✅ 精确：`u16be@5 ×0.1` |
| seq / 校验 | ✅ 对 | ✅ seq 对，CRC 标未知（不编造） |
| 消耗 | 924 tokens / 4.4 秒（联网） | 0 token / <1 秒（纯本地） |

两个值得注意的细节：

- LLM 被两个巧合骗了：温度小端的高字节（09→0A→0B）长得像递增序号；湿度低字节恰好随环境递增，被误当成温度候选。
- LLM 的报告最后自己开出药方：「**做受控实验：单独改变温度（湿度恒定），定位温度字节**」——这正是 SerialLens 学习模式自动化的流程。

**结论**：数据字段的精确定位靠算法+真值标注，不靠模型猜。AI 在 SerialLens 里的角色
是给方向性假设、解释异常、生成下一步实验建议——而不是替你猜字节。

> 注：单次实验、单一模型、教学向帧格式，不构成严格基准。欢迎用你自己的设备复现并在
> [Discussions](https://github.com/wdfdsyhb/SerialLens/discussions) 分享对照结果。

## 文件回放与 C 代码生成（v0.4 新增）

**文件回放**——手上没有设备，但有抓包？直接分析：

```bash
seriallens replay capture.bin               # 二进制捕获
seriallens replay forum_dump.txt            # 论坛求助帖里的 hexdump（带偏移前缀+ASCII 尾注）
seriallens replay log.hex --ai              # 纯 HEX 文本，附加 AI 分析
```

支持格式自动识别：二进制 / 纯 HEX（空格冒号换行分隔均可）/ hexdump 带偏移格式
（兼容 `hexdump -C`、逻辑分析仪导出、SerialLens 自己的报告）。可读文本但不是
HEX 会明确拒绝——不猜语义。

**C 代码生成**——画像直接变嵌入式解析器：

```bash
seriallens export-c my-sensor --out firmware/
# -> firmware/my-sensor_parser.h / .c
```

生成特点：
- **零依赖自包含**：不 include 任何系统头（stdint 等价物内联 typedef），
  裸机/RTOS/PC 任何编译器直接编
- **流式状态机**：`feed()` 任意分块喂入，跨块帧不丢（尾部自动结转）
- 端序按画像显式生成（u16le/u16be…），物理值 = 原始值 × scale
- 非 ASCII 字段名自动转合法 C 标识符，中文语义名保留在注释
- 诚实原则：不生成校验代码（画像未推断出校验算法），注释明说

一条链路：**抓包 → learn → export-c → 编进固件**。

## MCP server：把串口能力接进 AI Agent（v0.3 新增）

`seriallens mcp` 以 stdio 运行 MCP server，让 ZCode、Claude Desktop、Cursor 等
任何 MCP 客户端直接获得串口侦探能力。全部工具无硬件可用（内置虚拟传感器靶场）：

| 工具 | 作用 |
| --- | --- |
| `serial__ports` | 列出系统串口 |
| `serial__demo_frames` | 生成虚拟传感器帧（学习模式靶场，布局对 agent 保密） |
| `serial__analyze` | HEX 采样分析（启发式 + 可选 AI） |
| `serial__detect_baud` | 真实串口波特率扫描 |
| `serial__capture` | 真实串口采样（唯一有副作用的工具，只读） |
| `serial__learn` | 多帧学习 → 协议画像（保存并返回解码验证） |
| `serial__decode` | 用画像实时解码 HEX 流 |
| `serial__profiles` / `serial__server_info` | 画像清单 / 版本信息 |

**ZCode 配置**（`~/.zcode/cli/config.json` → `mcp.servers`）：

```json
"seriallens": {
  "type": "stdio",
  "command": "seriallens",
  "args": ["mcp"]
}
```

**Claude Desktop 配置**（`claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "seriallens": {
      "command": "uvx",
      "args": ["seriallens", "mcp"]
    }
  }
}
```

接入后你可以直接对 agent 说：「用 serial__demo_frames 生成一段靶场数据，
学习它的协议，然后告诉我温度字段在哪」——agent 全程自己调工具完成闭环。

## 配置 AI 分析

任何 OpenAI 兼容接口都可以（DeepSeek / GLM / OpenRouter / 本地 vLLM …），
key 只从环境变量读取，不落盘、不进日志：

| 环境变量 | 说明 |
| --- | --- |
| `SERIALLENS_API_KEY` | 优先使用 |
| `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` | 备选 |
| `SERIALLENS_BASE_URL` | 默认 `https://api.deepseek.com` |
| `SERIALLENS_MODEL` | 默认 `deepseek-chat` |

不配 key 也能用：启发式识别全部本地完成，AI 部分显示配置指引。

## 工作原理（诚实版）

- **波特率检测是软件层的试错扫描**，不是示波器式的电平测量：同一份线路数据用各档波特率收一段，
  「正确的波特率」下 ASCII 可读率、行完整性、校验和通过率会显著更好。设备静默或电平异常时检测不出，会如实告诉你。
- **协议识别是启发式**，只陈述证据（几行以 `$` 开头、校验和几条通过、帧头出现几次），不替你下最终结论。
- **AI 输出仅供参考**，报告里注明「以实测为准」。逆向未知协议请遵守当地法律与设备条款。

## Lens 系列

SerialLens 是 **Lens 系列**的一员——同一套理念：面对未知硬件与协议，只报告证据，不下断言。

| 工具 | 领域 | 状态 |
| --- | --- | --- |
| **SerialLens** | 串口 / UART 协议识别与解码 | ✅ 可用 |
| [modbus-lens](https://github.com/wdfdsyhb/modbus-lens) | Modbus RTU 从站发现与寄存器侦察 | ✅ 可用 |
| can-lens | CAN 总线流量画像 | 🚧 规划中 |
| ble-lens | BLE GATT 服务侦察 | 💡 构想中 |

## 路线图

- [x] v0.1 串口收发 / 自动波特率 / 启发式识别 / AI 分析 / Markdown 报告
- [x] v0.2 协议学习模式：多轮采样对比，自动推断字段含义并生成解码画像
- [x] v0.3 MCP server：9 个工具接入 ZCode / Claude Desktop，无硬件全流程可用
- [x] v0.4 文件回放（.bin / HEX / hexdump）+ 协议画像 → C 解析器代码生成
- [ ] v0.4.x agent skill 形态、Python 目标代码生成
- [ ] v0.5 TUI 实时解码视图、Modbus RTU 配对、ESP32 无线串口网关

## 适合谁

- 电子 / 物联网 / 自动化专业学生：第一个被乱码支配的夜晚，有它好过一些
- 硬件工程师：拿到不明模块，先让 AI 帮你把协议猜个七七八八
- 逆向爱好者：配合逻辑分析仪，把人工抠协议的苦力活自动化一部分

## License

MIT
