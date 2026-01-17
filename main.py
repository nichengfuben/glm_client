# main.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chat.z.ai 高性能异步自动化交互模块

本模块提供与 chat.z.ai 网站进行高性能异步对话的功能。
采用标签页池化 + LRU策略 + 异步队列实现高并发处理。

主要特性:
    - 异步非阻塞的 chat() 接口
    - 10个标签页的连接池，支持并发请求
    - LRU策略选择最久未使用的标签页
    - FIFO队列管理等待中的请求
    - 浏览器常驻运行，避免频繁启停开销
    - 自动故障恢复和重试机制

Example:
    >>> import asyncio
    >>> from main import chat, initialize, shutdown
    >>> 
    >>> async def main():
    ...     await initialize()
    ...     
    ...     # 并发发送多个请求
    ...     tasks = [
    ...         chat("你好，请介绍Python"),
    ...         chat("请写一个排序算法"),
    ...         chat("什么是机器学习？")
    ...     ]
    ...     responses = await asyncio.gather(*tasks)
    ...     
    ...     for i, resp in enumerate(responses):
    ...         print(f"响应 {i+1}: {resp[:100]}...")
    ...     
    ...     # 浏览器保持运行，用户可手动关闭
    ...     # 或调用 await shutdown() 清理资源
    >>> 
    >>> asyncio.run(main())

Architecture:
    使用单个 Selenium WebDriver 实例管理10个标签页。
    每个标签页独立处理一个请求，通过轮询检测响应完成状态。
    请求调度采用生产者-消费者模式，确保高并发下的稳定性。

Note:
    - 浏览器窗口由用户控制关闭，程序不会自动关闭浏览器
    - chromedriver.exe 进程在初始化时会被清理
    - 支持优雅关闭，调用 shutdown() 可释放所有资源
"""

import os
import sys
import time
import asyncio
import traceback
import atexit
import threading
import uuid
from enum import Enum, auto
from typing import Optional, Any, Dict, List, Tuple, Callable, TypeVar, Generic
from dataclasses import dataclass, field
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, Future as ThreadFuture
from contextlib import contextmanager
import heapq
import weakref
import logging

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    StaleElementReferenceException,
    WebDriverException,
    InvalidSessionIdException
)
from selenium_stealth import stealth

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger('ChatManager')

# 在启动前清理残留的 chromedriver 进程
os.system('taskkill /f /im chromedriver.exe 2>nul')


class TabState(Enum):
    """
    标签页状态枚举
    
    状态转换图:
        UNINITIALIZED -> INITIALIZING -> IDLE
        IDLE -> REFRESHING -> SENDING -> WAITING_RESPONSE
        WAITING_RESPONSE -> EXTRACTING -> IDLE
        任意状态 -> ERROR -> RECOVERING -> IDLE
    """
    UNINITIALIZED = auto()    # 未初始化
    INITIALIZING = auto()     # 正在初始化
    IDLE = auto()             # 空闲，可接受新请求
    REFRESHING = auto()       # 正在刷新页面
    SENDING = auto()          # 正在发送请求
    WAITING_RESPONSE = auto() # 等待模型响应
    EXTRACTING = auto()       # 正在提取响应内容
    ERROR = auto()            # 发生错误
    RECOVERING = auto()       # 正在恢复


@dataclass
class TabInfo:
    """
    标签页信息数据类
    
    存储单个标签页的完整状态信息，包括窗口句柄、状态、
    最后使用时间、当前处理的请求等。
    
    Attributes:
        handle: 浏览器窗口句柄，用于切换标签页
        index: 标签页索引（0-9），用于日志显示
        state: 当前状态
        last_used_time: 最后使用时间戳，用于LRU排序
        current_request_id: 当前正在处理的请求ID
        request_start_time: 请求开始时间，用于超时检测
        error_count: 连续错误次数，用于故障判定
        last_error: 最后一次错误信息
    """
    handle: str
    index: int
    state: TabState = TabState.UNINITIALIZED
    last_used_time: float = field(default_factory=time.time)
    current_request_id: Optional[str] = None
    request_start_time: Optional[float] = None
    error_count: int = 0
    last_error: Optional[str] = None
    
    def reset_error_state(self) -> None:
        """重置错误状态"""
        self.error_count = 0
        self.last_error = None
    
    def record_error(self, error: str) -> None:
        """记录错误"""
        self.error_count += 1
        self.last_error = error
        self.state = TabState.ERROR
    
    def is_available(self) -> bool:
        """检查标签页是否可用于新请求"""
        return self.state == TabState.IDLE and self.error_count < 3


@dataclass(order=True)
class PendingRequest:
    """
    等待处理的请求数据类
    
    使用 dataclass 的 order=True 特性实现优先队列排序。
    priority 字段（入队时间）作为排序依据，确保FIFO顺序。
    
    Attributes:
        priority: 优先级，使用入队时间戳，越小优先级越高
        request_id: 唯一请求标识符
        content: 用户输入的问题内容
        timeout: 请求超时时间（秒）
        future: 异步Future对象，用于返回结果
        enqueue_time: 入队时间戳
        retry_count: 重试次数
    """
    priority: float = field(compare=True)
    request_id: str = field(compare=False)
    content: str = field(compare=False)
    timeout: int = field(compare=False)
    future: asyncio.Future = field(compare=False)
    enqueue_time: float = field(default_factory=time.time, compare=False)
    retry_count: int = field(default=0, compare=False)
    
    def __hash__(self) -> int:
        return hash(self.request_id)


class RequestTracker:
    """
    请求追踪器
    
    维护请求ID到请求对象的映射，支持快速查找和清理。
    使用线程安全的字典实现。
    """
    
    def __init__(self) -> None:
        self._requests: Dict[str, PendingRequest] = {}
        self._lock = threading.RLock()
    
    def add(self, request: PendingRequest) -> None:
        """添加请求到追踪器"""
        with self._lock:
            self._requests[request.request_id] = request
    
    def get(self, request_id: str) -> Optional[PendingRequest]:
        """获取请求对象"""
        with self._lock:
            return self._requests.get(request_id)
    
    def remove(self, request_id: str) -> Optional[PendingRequest]:
        """移除并返回请求对象"""
        with self._lock:
            return self._requests.pop(request_id, None)
    
    def get_all_active(self) -> List[PendingRequest]:
        """获取所有活跃请求"""
        with self._lock:
            return [r for r in self._requests.values() if not r.future.done()]
    
    def clear(self) -> None:
        """清空所有请求"""
        with self._lock:
            self._requests.clear()


class TabPool:
    """
    标签页连接池
    
    管理多个标签页的生命周期，实现LRU策略选择空闲标签页。
    提供线程安全的标签页获取和释放接口。
    
    LRU策略:
        使用 OrderedDict 维护标签页访问顺序。
        每次使用标签页后，将其移动到末尾。
        获取空闲标签页时，从头部开始查找（最久未使用）。
    
    Attributes:
        max_tabs: 最大标签页数量
        tabs: 标签页信息字典，handle -> TabInfo
        lru_order: LRU顺序列表，维护访问顺序
    """
    
    def __init__(self, max_tabs: int = 10) -> None:
        self.max_tabs = max_tabs
        self.tabs: Dict[str, TabInfo] = {}
        self.lru_order: List[str] = []
        self._lock = threading.RLock()
    
    def add_tab(self, handle: str, index: int) -> TabInfo:
        """添加新标签页到池中"""
        with self._lock:
            tab_info = TabInfo(
                handle=handle,
                index=index,
                state=TabState.UNINITIALIZED,
                last_used_time=time.time()
            )
            self.tabs[handle] = tab_info
            self.lru_order.append(handle)
            return tab_info
    
    def get_tab(self, handle: str) -> Optional[TabInfo]:
        """获取标签页信息"""
        with self._lock:
            return self.tabs.get(handle)
    
    def get_lru_idle_tab(self) -> Optional[TabInfo]:
        """
        获取最久未使用的空闲标签页（LRU策略）
        
        Returns:
            空闲的TabInfo对象，如果没有空闲标签页则返回None
        """
        with self._lock:
            for handle in self.lru_order:
                tab = self.tabs.get(handle)
                if tab and tab.is_available():
                    return tab
            return None
    
    def update_lru(self, handle: str) -> None:
        """更新标签页的LRU顺序（移动到末尾）"""
        with self._lock:
            if handle in self.lru_order:
                self.lru_order.remove(handle)
                self.lru_order.append(handle)
            if handle in self.tabs:
                self.tabs[handle].last_used_time = time.time()
    
    def set_tab_state(self, handle: str, state: TabState) -> None:
        """设置标签页状态"""
        with self._lock:
            if handle in self.tabs:
                self.tabs[handle].state = state
    
    def get_tabs_by_state(self, state: TabState) -> List[TabInfo]:
        """获取指定状态的所有标签页"""
        with self._lock:
            return [tab for tab in self.tabs.values() if tab.state == state]
    
    def get_all_handles(self) -> List[str]:
        """获取所有标签页句柄"""
        with self._lock:
            return list(self.tabs.keys())
    
    def get_idle_count(self) -> int:
        """获取空闲标签页数量"""
        with self._lock:
            return sum(1 for tab in self.tabs.values() if tab.is_available())
    
    def get_busy_count(self) -> int:
        """获取忙碌标签页数量"""
        with self._lock:
            return sum(
                1 for tab in self.tabs.values() 
                if tab.state in (
                    TabState.SENDING, 
                    TabState.WAITING_RESPONSE, 
                    TabState.EXTRACTING
                )
            )


class ResponseExtractor:
    """
    响应内容提取器
    
    封装从页面提取格式化响应内容的逻辑。
    使用 JavaScript 精确解析 HTML 结构。
    """
    
    # JavaScript 提取脚本
    EXTRACT_SCRIPT = r"""
    function extractContent(container) {
        let results = [];
        
        function getTextWithInlineCode(element) {
            let text = '';
            for (let node of element.childNodes) {
                if (node.nodeType === Node.TEXT_NODE) {
                    text += node.textContent;
                } else if (node.nodeType === Node.ELEMENT_NODE) {
                    const tag = node.tagName.toLowerCase();
                    if (tag === 'code') {
                        text += '`' + node.textContent + '`';
                    } else if (tag === 'strong' || tag === 'b') {
                        text += '**' + getTextWithInlineCode(node) + '**';
                    } else if (tag === 'em' || tag === 'i') {
                        text += '*' + getTextWithInlineCode(node) + '*';
                    } else if (tag === 'a') {
                        let href = node.getAttribute('href') || '';
                        text += '[' + getTextWithInlineCode(node) + '](' + href + ')';
                    } else {
                        text += getTextWithInlineCode(node);
                    }
                }
            }
            return text;
        }
        
        function extractCodeBlock(codeContainer) {
            let langDiv = codeContainer.querySelector('.absolute.pl-2.py-1');
            let lang = langDiv ? langDiv.textContent.trim() : '';
            
            let codeLines = [];
            let cmContent = codeContainer.querySelector('.cm-content');
            if (cmContent) {
                let lines = cmContent.querySelectorAll('.cm-line');
                lines.forEach(function(line) {
                    codeLines.push(line.textContent);
                });
            }
            
            if (codeLines.length > 0) {
                return '```' + lang + '\n' + codeLines.join('\n') + '\n```';
            }
            return null;
        }
        
        function isCodeBlockContainer(element) {
            if (!element.classList) return false;
            return element.classList.contains('relative') && 
                   element.classList.contains('my-2') && 
                   element.classList.contains('flex-col') &&
                   element.classList.contains('rounded-xl') &&
                   element.querySelector('.cm-content');
        }
        
        function processElement(element) {
            if (!element || element.nodeType !== Node.ELEMENT_NODE) {
                return null;
            }
            
            const tag = element.tagName.toLowerCase();
            const classList = element.classList;
            
            if (element.style.display === 'none' || 
                classList.contains('hidden') ||
                classList.contains('cm-cursor') ||
                classList.contains('cm-cursorLayer') ||
                classList.contains('cm-selectionLayer') ||
                classList.contains('cm-announced') ||
                classList.contains('copy-code-button')) {
                return null;
            }
            
            if (isCodeBlockContainer(element)) {
                return extractCodeBlock(element);
            }
            
            if (tag === 'p') {
                let content = getTextWithInlineCode(element);
                return content.trim() || null;
            }
            
            if (/^h[1-6]$/.test(tag)) {
                let level = parseInt(tag[1]);
                let prefix = '#'.repeat(level) + ' ';
                let content = getTextWithInlineCode(element);
                return prefix + content.trim();
            }
            
            if (tag === 'ol') {
                let items = [];
                let startNum = parseInt(element.getAttribute('start')) || 1;
                let listItems = element.querySelectorAll(':scope > li');
                listItems.forEach(function(li, index) {
                    let content = getTextWithInlineCode(li);
                    items.push((startNum + index) + '. ' + content.trim());
                });
                return items.length > 0 ? items.join('\n') : null;
            }
            
            if (tag === 'ul') {
                let items = [];
                let listItems = element.querySelectorAll(':scope > li');
                listItems.forEach(function(li) {
                    let content = getTextWithInlineCode(li);
                    items.push('- ' + content.trim());
                });
                return items.length > 0 ? items.join('\n') : null;
            }
            
            if (tag === 'blockquote') {
                let content = getTextWithInlineCode(element);
                if (content.trim()) {
                    return '> ' + content.trim().split('\n').join('\n> ');
                }
                return null;
            }
            
            if (tag === 'div') {
                let childResults = [];
                for (let child of element.children) {
                    let result = processElement(child);
                    if (result) {
                        childResults.push(result);
                    }
                }
                return childResults.length > 0 ? childResults.join('\n\n') : null;
            }
            
            return null;
        }
        
        for (let child of container.children) {
            let result = processElement(child);
            if (result) {
                results.push(result);
            }
        }
        
        let finalResult = results.join('\n\n');
        finalResult = finalResult.replace(/\n{3,}/g, '\n\n');
        return finalResult.trim();
    }
    
    return extractContent(arguments[0]);
    """
    
    @classmethod
    def extract(cls, driver: Any) -> str:
        """
        从当前页面提取响应内容
        
        Args:
            driver: Selenium WebDriver实例
            
        Returns:
            格式化的Markdown响应内容
            
        Raises:
            TimeoutException: 等待响应容器超时
        """
        container = WebDriverWait(driver, 60).until(
            EC.presence_of_element_located((By.ID, "response-content-container"))
        )
        result = driver.execute_script(cls.EXTRACT_SCRIPT, container)
        return result if result else ""


class ChatManager:
    """
    聊天管理器 - 核心类
    
    管理浏览器实例、标签页池和请求队列。
    实现LRU策略选择标签页，FIFO队列处理等待请求。
    采用单例模式确保全局只有一个浏览器实例。
    
    Architecture:
        - 使用 ThreadPoolExecutor 将同步 Selenium 操作包装为异步
        - Dispatcher 任务负责将队列中的请求分配到空闲标签页
        - Poller 任务负责轮询检测响应完成状态
        - 所有 Selenium 操作通过 _selenium_lock 互斥执行
    
    Thread Safety:
        - _selenium_lock: 保护所有 Selenium 操作
        - _queue_lock: 保护请求队列操作
        - TabPool 内部使用 RLock 保护状态
    
    Example:
        >>> manager = ChatManager.get_instance()
        >>> await manager.initialize()
        >>> response = await manager.chat("你好")
        >>> print(response)
    """
    
    _instance: Optional['ChatManager'] = None
    _instance_lock = threading.Lock()
    
    # 配置常量
    MAX_TABS: int = 10
    POLL_INTERVAL: float = 0.3  # 轮询间隔（秒）
    DISPATCH_INTERVAL: float = 0.1  # 调度间隔（秒）
    MAX_RETRY_COUNT: int = 2  # 最大重试次数
    TAB_INIT_DELAY: float = 0.2  # 标签页初始化延迟（秒）
    REQUEST_TIMEOUT_BUFFER: int = 30  # 请求超时缓冲（秒）
    
    @classmethod
    def get_instance(cls) -> 'ChatManager':
        """
        获取ChatManager单例实例
        
        Returns:
            ChatManager单例对象
        """
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance
    
    def __init__(self) -> None:
        """
        初始化ChatManager
        
        注意：应使用 get_instance() 获取实例，不要直接调用构造函数
        """
        # 防止重复初始化
        if hasattr(self, '_init_done') and self._init_done:
            return
        
        # 浏览器和标签页
        self.driver: Optional[Any] = None
        self.tab_pool: TabPool = TabPool(self.MAX_TABS)
        
        # 请求管理
        self.request_tracker: RequestTracker = RequestTracker()
        self._pending_queue: List[PendingRequest] = []  # 最小堆（优先队列）
        self._queue_lock = threading.RLock()
        self._queue_not_empty = asyncio.Event()
        
        # Selenium 操作锁
        self._selenium_lock = threading.RLock()
        
        # 线程池（单线程，确保 Selenium 操作顺序执行）
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="selenium_worker"
        )
        
        # 异步任务
        self._poll_task: Optional[asyncio.Task] = None
        self._dispatch_task: Optional[asyncio.Task] = None
        self._timeout_check_task: Optional[asyncio.Task] = None
        
        # 状态标志
        self._shutdown_flag = False
        self._browser_ready = False
        self._initialization_lock = asyncio.Lock()
        
        # 请求计数器
        self._request_counter = 0
        self._request_counter_lock = threading.Lock()
        
        # 事件循环引用
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        
        self._init_done = True
        
        logger.info("ChatManager 实例已创建")
    
    async def initialize(self) -> None:
        """
        初始化浏览器和标签页池
        
        此方法是幂等的，可以多次调用但只会初始化一次。
        初始化完成后启动后台调度和轮询任务。
        
        Raises:
            WebDriverException: 浏览器启动失败
            TimeoutException: 页面加载超时
        """
        async with self._initialization_lock:
            if self._browser_ready:
                logger.debug("浏览器已就绪，跳过初始化")
                return
            
            self._loop = asyncio.get_running_loop()
            
            logger.info("=" * 60)
            logger.info("开始初始化 ChatManager")
            logger.info("=" * 60)
            
            # 在线程池中执行同步的浏览器设置
            await self._loop.run_in_executor(
                self._executor,
                self._setup_browser_sync
            )
            
            # 启动后台任务
            self._shutdown_flag = False
            self._poll_task = asyncio.create_task(
                self._poll_responses_loop(),
                name="response_poller"
            )
            self._dispatch_task = asyncio.create_task(
                self._dispatch_requests_loop(),
                name="request_dispatcher"
            )
            self._timeout_check_task = asyncio.create_task(
                self._check_timeouts_loop(),
                name="timeout_checker"
            )
            
            self._browser_ready = True
            
            logger.info("=" * 60)
            logger.info(f"初始化完成: {self.MAX_TABS} 个标签页就绪")
            logger.info("=" * 60)
    
    def _setup_browser_sync(self) -> None:
        """
        同步方法：设置浏览器和创建标签页
        
        此方法在线程池中执行，完成以下操作：
        1. 创建 Chrome WebDriver 实例
        2. 创建指定数量的标签页
        3. 初始化每个标签页（加载页面、关闭深度思考）
        """
        logger.info("正在启动浏览器...")
        self.driver = self._create_chrome_driver()
        
        logger.info(f"正在创建 {self.MAX_TABS} 个标签页...")
        
        # 第一个标签页已存在
        first_handle = self.driver.current_window_handle
        self.tab_pool.add_tab(first_handle, 0)
        logger.debug(f"  [Tab 0] 句柄: {first_handle[:20]}...")
        
        # 创建剩余标签页
        for i in range(1, self.MAX_TABS):
            self.driver.execute_script("window.open('about:blank', '_blank');")
            time.sleep(self.TAB_INIT_DELAY)
        
        # 获取所有标签页句柄并注册到池中
        handles = self.driver.window_handles
        for i, handle in enumerate(handles):
            if handle not in self.tab_pool.tabs:
                self.tab_pool.add_tab(handle, i)
                logger.debug(f"  [Tab {i}] 句柄: {handle[:20]}...")
        
        logger.info("正在初始化所有标签页...")
        
        # 初始化每个标签页
        for handle in handles:
            self._initialize_tab_sync(handle)
        
        logger.info("所有标签页初始化完成")
    
    def _create_chrome_driver(self) -> Any:
        """
        创建 Chrome WebDriver 实例
        
        配置反检测选项和隐身模式，使用本地 chromedriver。
        
        Returns:
            配置好的 Chrome WebDriver 实例
            
        Raises:
            WebDriverException: 浏览器启动失败
        """
        import undetected_chromedriver as uc
        from undetected_chromedriver.patcher import Patcher
        
        chrome_options = Options()
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--window-size=1920,1080")
        chrome_options.add_argument("--log-level=3")
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_argument('--incognito')
        chrome_options.set_capability('goog:loggingPrefs', {'browser': 'ALL'})
        
        # 补丁 Patcher.auto 方法以使用本地 chromedriver
        _original_auto = Patcher.auto
        
        def _patched_auto(self, *args, **kwargs):
            """使用本地 chromedriver，跳过自动检测"""
            logger.debug("使用本地 chromedriver...")
            
            self.version_main = 137
            
            class CustomVersion:
                vstring = "137.0.7151.55"
                version = [137, 0, 7151, 55]
                
                def __str__(self) -> str:
                    return self.vstring
            
            self.version_full = CustomVersion()
            
            local_path = "chromedriver.exe"
            if os.path.exists(local_path):
                logger.debug(f"chromedriver 路径: {os.path.abspath(local_path)}")
                self.executable_path = local_path
                return self.patch()
            else:
                logger.warning("未找到本地 chromedriver.exe，使用自动检测")
                return _original_auto(self, *args, **kwargs)
        
        Patcher.auto = _patched_auto
        
        chrome_path = r"chrome-win64\chrome.exe"
        chrome_options.binary_location = chrome_path
        
        driver = uc.Chrome(
            headless=False,
            use_subprocess=False,
            options=chrome_options,
            version_main=137
        )
        
        # 加载 stealth.min.js
        stealth_file_path = 'stealth.min.js'
        if os.path.exists(stealth_file_path):
            try:
                with open(stealth_file_path, mode='r', encoding='utf-8') as f:
                    stealth_script = f.read()
                driver.execute_cdp_cmd(
                    'Page.addScriptToEvaluateOnNewDocument',
                    {'source': stealth_script}
                )
                logger.debug("已加载 stealth.min.js")
            except Exception as e:
                logger.warning(f"加载 stealth.min.js 失败: {e}")
        
        # 应用 selenium_stealth
        stealth(
            driver,
            languages=["en-US", "en"],
            vendor="Google Inc.",
            platform="Win32",
            webgl_vendor="Intel Inc.",
            renderer="Intel Iris OpenGL Engine",
            fix_hairline=True,
        )
        
        return driver
    
    def _initialize_tab_sync(self, handle: str) -> None:
        """
        同步方法：初始化单个标签页
        
        加载 chat.z.ai 页面，等待加载完成，关闭深度思考模式。
        
        Args:
            handle: 标签页窗口句柄
        """
        tab = self.tab_pool.get_tab(handle)
        if tab is None:
            return
        
        with self._selenium_lock:
            try:
                tab.state = TabState.INITIALIZING
                
                self.driver.switch_to.window(handle)
                self.driver.get("https://chat.z.ai/")
                
                # 等待深度思考按钮出现
                autothink_button = WebDriverWait(self.driver, 30).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "button[data-autothink]")
                    )
                )
                
                # 关闭深度思考
                current_value = autothink_button.get_attribute("data-autothink")
                if current_value == "true":
                    self.driver.execute_script(
                        "arguments[0].setAttribute('data-autothink', 'false')",
                        autothink_button
                    )
                    autothink_button.click()
                
                tab.state = TabState.IDLE
                tab.reset_error_state()
                
                logger.info(f"  [Tab {tab.index}] 初始化完成")
                
            except Exception as e:
                error_msg = f"初始化失败: {e}"
                tab.record_error(error_msg)
                logger.error(f"  [Tab {tab.index}] {error_msg}")
    
    def _generate_request_id(self) -> str:
        """生成唯一请求ID"""
        with self._request_counter_lock:
            self._request_counter += 1
            return f"req_{int(time.time() * 1000)}_{self._request_counter}"
    
    async def chat(self, content: str, timeout: int = 120) -> str:
        """
        异步发送聊天请求
        
        此方法是主要的对外接口，将请求加入队列后立即返回 Future。
        请求将由后台调度器分配到空闲标签页处理。
        
        Args:
            content: 用户输入的问题内容
            timeout: 请求超时时间（秒），默认120秒
            
        Returns:
            模型生成的响应内容（Markdown格式）
            
        Raises:
            ValueError: 输入内容为空
            TimeoutException: 请求超时
            WebDriverException: 浏览器操作失败
            
        Example:
            >>> response = await manager.chat("请解释什么是递归")
            >>> print(response)
        """
        if not content or not content.strip():
            raise ValueError("输入内容不能为空")
        
        # 确保已初始化
        if not self._browser_ready:
            await self.initialize()
        
        # 创建请求
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        
        request = PendingRequest(
            priority=time.time(),
            request_id=self._generate_request_id(),
            content=content,
            timeout=timeout,
            future=future,
            enqueue_time=time.time()
        )
        
        # 注册请求
        self.request_tracker.add(request)
        
        # 加入队列
        with self._queue_lock:
            heapq.heappush(self._pending_queue, request)
        
        # 通知调度器
        self._queue_not_empty.set()
        
        display_content = content[:50] + "..." if len(content) > 50 else content
        logger.info(f"[{request.request_id}] 请求已加入队列: {display_content}")
        
        # 等待结果
        try:
            result = await asyncio.wait_for(
                future,
                timeout=timeout + self.REQUEST_TIMEOUT_BUFFER
            )
            return result
        except asyncio.TimeoutError:
            # 清理请求
            self.request_tracker.remove(request.request_id)
            raise TimeoutException(
                f"请求超时（等待 {timeout + self.REQUEST_TIMEOUT_BUFFER} 秒）"
            )
        except asyncio.CancelledError:
            self.request_tracker.remove(request.request_id)
            raise
    
    async def _dispatch_requests_loop(self) -> None:
        """
        请求调度循环
        
        持续监控请求队列，将等待的请求分配到空闲标签页。
        使用事件驱动方式，当队列为空时等待新请求到达。
        """
        logger.info("请求调度器已启动")
        
        while not self._shutdown_flag:
            try:
                # 等待队列非空
                try:
                    await asyncio.wait_for(
                        self._queue_not_empty.wait(),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue
                
                if self._shutdown_flag:
                    break
                
                # 尝试调度请求
                dispatched = await self._loop.run_in_executor(
                    self._executor,
                    self._try_dispatch_one
                )
                
                if not dispatched:
                    # 没有空闲标签页或队列空，短暂等待
                    await asyncio.sleep(self.DISPATCH_INTERVAL)
                    
                    # 检查队列是否为空
                    with self._queue_lock:
                        if not self._pending_queue:
                            self._queue_not_empty.clear()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"调度器错误: {e}")
                traceback.print_exc()
                await asyncio.sleep(0.5)
        
        logger.info("请求调度器已停止")
    
    def _try_dispatch_one(self) -> bool:
        """
        尝试分配一个请求到空闲标签页
        
        Returns:
            是否成功分配请求
        """
        # 获取等待时间最长的请求（FIFO）
        with self._queue_lock:
            if not self._pending_queue:
                return False
            
            # 获取最久未使用的空闲标签页（LRU策略）
            idle_tab = self.tab_pool.get_lru_idle_tab()
            if idle_tab is None:
                return False
            
            # 弹出请求
            request = heapq.heappop(self._pending_queue)
        
        # 分配请求到标签页
        try:
            self._send_request_to_tab_sync(idle_tab, request)
            return True
        except Exception as e:
            logger.error(f"[{request.request_id}] 发送失败: {e}")
            # 重新加入队列或设置错误
            if request.retry_count < self.MAX_RETRY_COUNT:
                request.retry_count += 1
                with self._queue_lock:
                    heapq.heappush(self._pending_queue, request)
                logger.info(f"[{request.request_id}] 重新加入队列 (重试 {request.retry_count})")
            else:
                self._complete_request(
                    request.request_id,
                    error=Exception(f"发送失败（重试 {self.MAX_RETRY_COUNT} 次后放弃）: {e}")
                )
            return False
    
    def _send_request_to_tab_sync(self, tab: TabInfo, request: PendingRequest) -> None:
        """
        同步方法：将请求发送到指定标签页
        
        执行以下步骤：
        1. 切换到目标标签页
        2. 刷新页面重置状态
        3. 等待页面加载完成
        4. 关闭深度思考模式
        5. 输入问题内容
        6. 点击发送按钮
        
        Args:
            tab: 目标标签页信息
            request: 待发送的请求
        """
        with self._selenium_lock:
            # 更新标签页状态
            tab.state = TabState.REFRESHING
            tab.current_request_id = request.request_id
            tab.request_start_time = time.time()
            self.tab_pool.update_lru(tab.handle)
            
            logger.debug(f"[{request.request_id}] 分配到 Tab {tab.index}")
            
            # 切换到目标标签页
            self.driver.switch_to.window(tab.handle)
            
            # 刷新页面
            self.driver.refresh()
            
            # 等待输入框出现
            tab.state = TabState.SENDING
            input_box = WebDriverWait(self.driver, 30).until(
                EC.presence_of_element_located((By.ID, "chat-input"))
            )
            
            # 关闭深度思考
            self._disable_autothink_sync()
            
            # 输入内容
            self.driver.execute_script(
                """
                const textarea = arguments[0];
                const text = arguments[1];
                textarea.value = text;
                textarea.dispatchEvent(new Event('input', { bubbles: true }));
                textarea.dispatchEvent(new Event('change', { bubbles: true }));
                const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLTextAreaElement.prototype, 'value'
                ).set;
                nativeInputValueSetter.call(textarea, text);
                textarea.dispatchEvent(new Event('input', { bubbles: true }));
                """,
                input_box, request.content
            )
            
            # 等待发送按钮可用
            send_button = WebDriverWait(self.driver, 30).until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, "#send-message-button:not([disabled])")
                )
            )
            
            # 点击发送
            send_button.click()
            
            # 更新状态为等待响应
            tab.state = TabState.WAITING_RESPONSE
            
            logger.info(f"[{request.request_id}] 已发送到 Tab {tab.index}")
    
    def _disable_autothink_sync(self) -> None:
        """关闭深度思考模式"""
        try:
            autothink_button = self.driver.find_element(
                By.CSS_SELECTOR, "button[data-autothink]"
            )
            if autothink_button.get_attribute("data-autothink") == "true":
                self.driver.execute_script(
                    "arguments[0].setAttribute('data-autothink', 'false')",
                    autothink_button
                )
                autothink_button.click()
        except NoSuchElementException:
            pass
        except Exception as e:
            logger.debug(f"关闭深度思考时发生错误: {e}")
    
    async def _poll_responses_loop(self) -> None:
        """
        响应轮询循环
        
        持续检查所有正在等待响应的标签页，检测响应是否完成。
        响应完成后提取内容并设置结果。
        """
        logger.info("响应轮询器已启动")
        
        while not self._shutdown_flag:
            try:
                # 获取所有等待响应的标签页
                waiting_tabs = self.tab_pool.get_tabs_by_state(
                    TabState.WAITING_RESPONSE
                )
                
                if waiting_tabs:
                    # 在执行器中检查响应状态
                    await self._loop.run_in_executor(
                        self._executor,
                        self._check_waiting_tabs_sync,
                        waiting_tabs
                    )
                
                await asyncio.sleep(self.POLL_INTERVAL)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"轮询器错误: {e}")
                await asyncio.sleep(0.5)
        
        logger.info("响应轮询器已停止")
    
    def _check_waiting_tabs_sync(self, tabs: List[TabInfo]) -> None:
        """
        同步方法：检查等待响应的标签页
        
        Args:
            tabs: 需要检查的标签页列表
        """
        for tab in tabs:
            if self._shutdown_flag:
                break
            self._check_single_tab_sync(tab)
    
    def _check_single_tab_sync(self, tab: TabInfo) -> None:
        """
        同步方法：检查单个标签页的响应状态
        
        Args:
            tab: 要检查的标签页
        """
        with self._selenium_lock:
            try:
                self.driver.switch_to.window(tab.handle)
                
                # 检查发送按钮是否被禁用（表示响应完成）
                try:
                    send_button = self.driver.find_element(
                        By.ID, "send-message-button"
                    )
                    is_disabled = send_button.get_attribute("disabled") is not None
                except NoSuchElementException:
                    return
                
                if is_disabled:
                    # 检查响应容器是否存在
                    response_containers = self.driver.find_elements(
                        By.ID, "response-content-container"
                    )
                    
                    if response_containers:
                        # 响应完成，提取内容
                        tab.state = TabState.EXTRACTING
                        
                        try:
                            content = ResponseExtractor.extract(self.driver)
                            self._complete_request(tab.current_request_id, content=content)
                            logger.info(
                                f"[{tab.current_request_id}] "
                                f"响应完成 (Tab {tab.index}, {len(content)} 字符)"
                            )
                        except Exception as e:
                            self._complete_request(
                                tab.current_request_id,
                                error=Exception(f"提取响应失败: {e}")
                            )
                        
                        # 重置标签页状态
                        tab.state = TabState.IDLE
                        tab.current_request_id = None
                        tab.request_start_time = None
                        tab.reset_error_state()
                        
            except InvalidSessionIdException:
                logger.error("浏览器会话已失效")
                self._browser_ready = False
            except Exception as e:
                logger.debug(f"检查 Tab {tab.index} 时发生错误: {e}")
    
    def _complete_request(
        self,
        request_id: str,
        content: Optional[str] = None,
        error: Optional[Exception] = None
    ) -> None:
        """
        完成请求处理
        
        设置 Future 的结果或异常，并从追踪器中移除请求。
        
        Args:
            request_id: 请求ID
            content: 响应内容（成功时）
            error: 错误信息（失败时）
        """
        request = self.request_tracker.remove(request_id)
        if request is None:
            return
        
        if request.future.done():
            return
        
        try:
            if error is not None:
                # 使用 call_soon_threadsafe 确保线程安全
                self._loop.call_soon_threadsafe(
                    request.future.set_exception,
                    error
                )
            else:
                self._loop.call_soon_threadsafe(
                    request.future.set_result,
                    content
                )
        except Exception as e:
            logger.error(f"设置请求结果时发生错误: {e}")
    
    async def _check_timeouts_loop(self) -> None:
        """
        超时检查循环
        
        定期检查正在处理的请求是否超时。
        """
        logger.info("超时检查器已启动")
        
        while not self._shutdown_flag:
            try:
                await asyncio.sleep(5.0)
                
                if self._shutdown_flag:
                    break
                
                current_time = time.time()
                
                # 检查等待响应的标签页
                waiting_tabs = self.tab_pool.get_tabs_by_state(
                    TabState.WAITING_RESPONSE
                )
                
                for tab in waiting_tabs:
                    if tab.current_request_id and tab.request_start_time:
                        request = self.request_tracker.get(tab.current_request_id)
                        if request:
                            elapsed = current_time - tab.request_start_time
                            if elapsed > request.timeout:
                                logger.warning(
                                    f"[{request.request_id}] 超时 "
                                    f"({elapsed:.1f}s > {request.timeout}s)"
                                )
                                self._complete_request(
                                    request.request_id,
                                    error=TimeoutException(
                                        f"请求处理超时（{request.timeout}秒）"
                                    )
                                )
                                # 重置标签页
                                tab.state = TabState.IDLE
                                tab.current_request_id = None
                                tab.request_start_time = None
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"超时检查器错误: {e}")
        
        logger.info("超时检查器已停止")
    
    def get_status(self) -> Dict[str, Any]:
        """
        获取当前状态信息
        
        Returns:
            包含标签页状态、队列长度等信息的字典
        """
        with self._queue_lock:
            queue_length = len(self._pending_queue)
        
        return {
            "browser_ready": self._browser_ready,
            "total_tabs": len(self.tab_pool.tabs),
            "idle_tabs": self.tab_pool.get_idle_count(),
            "busy_tabs": self.tab_pool.get_busy_count(),
            "pending_requests": queue_length,
            "active_requests": len(self.request_tracker.get_all_active()),
            "tabs": {
                tab.index: {
                    "state": tab.state.name,
                    "current_request": tab.current_request_id,
                    "error_count": tab.error_count
                }
                for tab in self.tab_pool.tabs.values()
            }
        }
    
    async def shutdown(self) -> None:
        """
        关闭管理器
        
        停止后台任务，清理资源。浏览器窗口不会关闭，由用户手动控制。
        """
        logger.info("正在关闭 ChatManager...")
        
        self._shutdown_flag = True
        self._queue_not_empty.set()  # 唤醒可能在等待的调度器
        
        # 取消后台任务
        tasks_to_cancel = [
            self._poll_task,
            self._dispatch_task,
            self._timeout_check_task
        ]
        
        for task in tasks_to_cancel:
            if task and not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
        
        # 关闭线程池
        self._executor.shutdown(wait=False)
        
        # 取消所有等待中的请求
        with self._queue_lock:
            for request in self._pending_queue:
                if not request.future.done():
                    request.future.cancel()
            self._pending_queue.clear()
        
        # 取消所有活跃请求
        for request in self.request_tracker.get_all_active():
            if not request.future.done():
                request.future.cancel()
        self.request_tracker.clear()
        
        self._browser_ready = False
        
        logger.info("ChatManager 已关闭（浏览器窗口保持运行）")


# 模块级便捷函数

_manager: Optional[ChatManager] = None


async def initialize() -> None:
    """
    初始化聊天管理器
    
    创建 ChatManager 单例并初始化浏览器和标签页池。
    此函数应在使用 chat() 之前调用，但 chat() 也会自动调用它。
    
    Example:
        >>> await initialize()
        >>> response = await chat("你好")
    """
    global _manager
    _manager = ChatManager.get_instance()
    await _manager.initialize()


async def chat(content: str, timeout: int = 120) -> str:
    """
    异步发送聊天请求（模块级便捷函数）
    
    此函数是主要的对外接口，可以方便地从其他模块导入使用。
    如果管理器未初始化，会自动进行初始化。
    
    Args:
        content: 用户输入的问题内容
        timeout: 请求超时时间（秒），默认120秒
        
    Returns:
        模型生成的响应内容（Markdown格式）
        
    Raises:
        ValueError: 输入内容为空
        TimeoutException: 请求超时
        
    Example:
        >>> import asyncio
        >>> from main import chat
        >>> 
        >>> async def main():
        ...     response = await chat("你好，请介绍一下Python")
        ...     print(response)
        >>> 
        >>> asyncio.run(main())
    """
    global _manager
    if _manager is None:
        _manager = ChatManager.get_instance()
    return await _manager.chat(content, timeout)


async def shutdown() -> None:
    """
    关闭聊天管理器（模块级便捷函数）
    
    停止后台任务，清理资源。浏览器窗口不会关闭。
    
    Example:
        >>> await shutdown()
    """
    global _manager
    if _manager is not None:
        await _manager.shutdown()


def get_status() -> Dict[str, Any]:
    """
    获取当前状态信息（模块级便捷函数）
    
    Returns:
        状态信息字典，如果管理器未初始化则返回空字典
    """
    global _manager
    if _manager is None:
        return {"initialized": False}
    return _manager.get_status()


# 同步包装函数（用于不支持异步的场景）

def chat_sync(content: str, timeout: int = 120) -> str:
    """
    同步发送聊天请求
    
    为不支持异步的场景提供的同步包装函数。
    内部创建新的事件循环执行异步操作。
    
    Args:
        content: 用户输入的问题内容
        timeout: 请求超时时间（秒）
        
    Returns:
        模型生成的响应内容
        
    Note:
        此函数会阻塞当前线程直到获得响应。
        推荐使用异步版本 chat() 以获得更好的性能。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    
    if loop is not None:
        # 已有事件循环，需要在新线程中执行
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(
                lambda: asyncio.run(chat(content, timeout))
            )
            return future.result()
    else:
        # 没有事件循环，直接运行
        return asyncio.run(chat(content, timeout))


async def main() -> None:
    """
    主函数，用于测试和演示
    
    执行多个并发对话测试，验证模块功能和性能。
    """
    print("\n" + "=" * 70)
    print("chat.z.ai 高性能异步自动化交互测试")
    print("=" * 70)
    
    try:
        # 初始化
        await initialize()
        
        # 打印初始状态
        status = get_status()
        print(f"\n初始状态: {status['idle_tabs']} 个空闲标签页")
        
        # 测试单个请求
        print("\n" + "-" * 70)
        print("测试 1: 单个请求")
        print("-" * 70)
        
        start_time = time.time()
        response = await chat("你好，请用一句话介绍Python")
        elapsed = time.time() - start_time
        
        print(f"\n响应时间: {elapsed:.2f}秒")
        print(f"响应内容:\n{response}")
        
        # 测试并发请求
        print("\n" + "-" * 70)
        print("测试 2: 并发请求（3个）")
        print("-" * 70)
        
        questions = [
            "什么是递归？用一句话回答",
            "什么是面向对象？用一句话回答",
            "什么是函数式编程？用一句话回答"
        ]
        
        start_time = time.time()
        tasks = [chat(q, timeout=60) for q in questions]
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        elapsed = time.time() - start_time
        
        print(f"\n并发响应时间: {elapsed:.2f}秒")
        for i, (q, r) in enumerate(zip(questions, responses)):
            if isinstance(r, Exception):
                print(f"\n问题 {i+1}: {q}")
                print(f"错误: {r}")
            else:
                print(f"\n问题 {i+1}: {q}")
                print(f"回答: {r[:200]}..." if len(r) > 200 else f"回答: {r}")
        
        # 打印最终状态
        print("\n" + "-" * 70)
        status = get_status()
        print(f"最终状态: {status}")
        
        print("\n" + "=" * 70)
        print("测试完成！浏览器保持运行，可手动关闭")
        print("=" * 70)
        
        # 保持运行，等待用户手动关闭
        print("\n按 Ctrl+C 退出程序...")
        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            print("\n收到退出信号")
        
    except Exception as e:
        print(f"\n测试失败: {e}")
        traceback.print_exc()
    finally:
        await shutdown()


if __name__ == "__main__":
    asyncio.run(main())
