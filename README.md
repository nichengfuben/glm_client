\# chat.z.ai 高性能异步自动化交互模块



\[!\[Python Version](https://img.shields.io/badge/python-3.14+-blue.svg)](https://www.python.org/downloads/)

\[!\[License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)



一个与 \[chat.z.ai](https://chat.z.ai) 网站进行高性能异步对话的 Python 自动化模块。采用标签页池化 + LRU 策略 + 异步队列实现高并发处理。



\## 特性



\- \*\*异步非阻塞接口\*\*: 基于 `asyncio` 的异步 `chat()` 接口

\- \*\*高并发支持\*\*: 10 个标签页连接池，支持并发请求处理

\- \*\*智能调度\*\*: LRU 策略选择最久未使用的标签页，FIFO 队列管理等待请求

\- \*\*常驻运行\*\*: 浏览器常驻运行，避免频繁启停开销

\- \*\*故障恢复\*\*: 自动故障恢复和重试机制

\- \*\*反检测\*\*: 集成 `undetected-chromedriver` 和 `selenium-stealth` 绑过检测



\## 系统要求



\- Python 3.14.0+

\- Chrome 浏览器（需要与 chromedriver 版本匹配）

\- Windows 操作系统（当前版本）



\## 安装



\### 1. 克隆仓库



```bash

git clone https://github.com/yourusername/chat-z-ai-automation.git

cd chat-z-ai-automation

```



\### 2. 创建虚拟环境



```bash

python -m venv venv



\# Windows

.\\venv\\Scripts\\activate



\# Linux/macOS

source venv/bin/activate

```



\### 3. 安装依赖



```bash

pip install -r requirements.txt

```



\### 4. 准备运行环境



确保以下文件存在于项目根目录：



```

project/

├── main.py

├── chromedriver.exe      # Chrome 驱动程序

├── stealth.min.js        # 反检测脚本（可选）

└── chrome-win64/         # Chrome 浏览器目录

&nbsp;   └── chrome.exe

```



\## 快速开始



\### 基本用法



```python

import asyncio

from main import chat, initialize, shutdown



async def main():

&nbsp;   # 初始化（可选，chat() 会自动调用）

&nbsp;   await initialize()

&nbsp;   

&nbsp;   # 发送单个请求

&nbsp;   response = await chat("你好，请介绍一下 Python")

&nbsp;   print(response)

&nbsp;   

&nbsp;   # 关闭管理器（可选）

&nbsp;   await shutdown()



asyncio.run(main())

```



\### 并发请求



```python

import asyncio

from main import chat, initialize



async def main():

&nbsp;   await initialize()

&nbsp;   

&nbsp;   questions = \[

&nbsp;       "什么是 Python?",

&nbsp;       "什么是 JavaScript?",

&nbsp;       "什么是 Go?",

&nbsp;   ]

&nbsp;   

&nbsp;   # 并发发送多个请求

&nbsp;   tasks = \[chat(q) for q in questions]

&nbsp;   responses = await asyncio.gather(\*tasks)

&nbsp;   

&nbsp;   for q, r in zip(questions, responses):

&nbsp;       print(f"问: {q}")

&nbsp;       print(f"答: {r}\\n")



asyncio.run(main())

```



\### 同步用法



对于不支持异步的场景，可以使用同步包装函数：



```python

from main import chat\_sync



response = chat\_sync("你好，请介绍一下 Python")

print(response)

```



\## API 参考



\### 模块级函数



\#### `async initialize() -> None`



初始化聊天管理器，创建浏览器实例和标签页池。



此函数是幂等的，可以多次调用但只会初始化一次。



\#### `async chat(content: str, timeout: int = 120) -> str`



异步发送聊天请求。



\*\*参数:\*\*

\- `content` (str): 用户输入的问题内容

\- `timeout` (int): 请求超时时间（秒），默认 120 秒



\*\*返回:\*\*

\- `str`: 模型生成的响应内容（Markdown 格式）



\*\*异常:\*\*

\- `ValueError`: 输入内容为空

\- `TimeoutException`: 请求超时



\#### `async shutdown() -> None`



关闭聊天管理器，停止后台任务，清理资源。



浏览器窗口不会关闭，由用户手动控制。



\#### `get\_status() -> Dict\[str, Any]`



获取当前状态信息。



\*\*返回:\*\*

```python

{

&nbsp;   "browser\_ready": bool,      # 浏览器是否就绪

&nbsp;   "total\_tabs": int,          # 总标签页数量

&nbsp;   "idle\_tabs": int,           # 空闲标签页数量

&nbsp;   "busy\_tabs": int,           # 忙碌标签页数量

&nbsp;   "pending\_requests": int,    # 等待中的请求数量

&nbsp;   "active\_requests": int,     # 活跃请求数量

&nbsp;   "tabs": Dict                # 各标签页状态详情

}

```



\#### `chat\_sync(content: str, timeout: int = 120) -> str`



同步发送聊天请求（阻塞版本）。



\### ChatManager 类



单例模式的聊天管理器核心类。



```python

from main import ChatManager



\# 获取单例实例

manager = ChatManager.get\_instance()



\# 初始化

await manager.initialize()



\# 发送请求

response = await manager.chat("你好")



\# 获取状态

status = manager.get\_status()



\# 关闭

await manager.shutdown()

```



\## 架构设计



\### 整体架构



```

+-------------------+     +------------------+     +------------------+

|   用户请求        | --> |   请求队列       | --> |   调度器         |

|   (async chat)    |     |   (FIFO)         |     |   (Dispatcher)   |

+-------------------+     +------------------+     +------------------+

&nbsp;                                                          |

&nbsp;                                                          v

+-------------------+     +------------------+     +------------------+

|   响应返回        | <-- |   轮询器         | <-- |   标签页池       |

|   (Future)        |     |   (Poller)       |     |   (LRU)          |

+-------------------+     +------------------+     +------------------+

```



\### 核心组件



1\. \*\*TabPool\*\*: 标签页连接池，管理多个标签页的生命周期

2\. \*\*RequestTracker\*\*: 请求追踪器，维护请求 ID 到请求对象的映射

3\. \*\*ResponseExtractor\*\*: 响应内容提取器，使用 JavaScript 解析 HTML

4\. \*\*ChatManager\*\*: 核心管理器，协调所有组件



\### 标签页状态机



```

UNINITIALIZED -> INITIALIZING -> IDLE

IDLE -> REFRESHING -> SENDING -> WAITING\_RESPONSE

WAITING\_RESPONSE -> EXTRACTING -> IDLE

任意状态 -> ERROR -> RECOVERING -> IDLE

```



\## 配置选项



在 `ChatManager` 类中可调整以下常量：



| 常量名 | 默认值 | 描述 |

|--------|--------|------|

| `MAX\_TABS` | 10 | 最大标签页数量 |

| `POLL\_INTERVAL` | 0.3 | 响应轮询间隔（秒） |

| `DISPATCH\_INTERVAL` | 0.1 | 请求调度间隔（秒） |

| `MAX\_RETRY\_COUNT` | 2 | 最大重试次数 |

| `TAB\_INIT\_DELAY` | 0.2 | 标签页初始化延迟（秒） |

| `REQUEST\_TIMEOUT\_BUFFER` | 30 | 请求超时缓冲（秒） |



\## 注意事项



1\. \*\*浏览器控制\*\*: 浏览器窗口由用户控制关闭，程序不会自动关闭浏览器

2\. \*\*进程清理\*\*: 初始化时会自动清理残留的 `chromedriver.exe` 进程

3\. \*\*反检测\*\*: 使用隐身模式和反检测脚本，但仍需注意使用频率

4\. \*\*资源管理\*\*: 建议在程序结束时调用 `shutdown()` 清理资源



\## 故障排除



\### 常见问题



\*\*Q: 浏览器启动失败\*\*

\- 检查 `chrome-win64/chrome.exe` 路径是否正确

\- 确认 `chromedriver.exe` 版本与 Chrome 版本匹配



\*\*Q: 响应提取为空\*\*

\- 检查页面是否正常加载

\- 确认网络连接正常



\*\*Q: 请求超时\*\*

\- 增加 `timeout` 参数值

\- 检查网络延迟



\## 开发



\### 运行测试



```bash

pytest tests/ -v --cov=. --cov-report=html

```



\### 代码格式化



```bash

black main.py

isort main.py

```



\### 类型检查



```bash

mypy main.py

```



\## 许可证



本项目采用 MIT 许可证。详见 \[LICENSE](LICENSE) 文件。



\## 作者



\- nichengfuben@outlook.com



\## 致谢



\- \[Selenium](https://www.selenium.dev/)

\- \[undetected-chromedriver](https://github.com/ultrafunkamsterdam/undetected-chromedriver)

\- \[selenium-stealth](https://github.com/diprajpatra/selenium-stealth)

