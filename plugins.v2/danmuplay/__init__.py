import datetime
import re
import shutil
import threading
import traceback
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional
import hashlib
import base64
import time

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from app import schemas
from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.chain.tmdb import TmdbChain
from app.chain.transfer import TransferChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.event import eventmanager, Event
from app.core.metainfo import MetaInfoPath
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.helper.directory import DirectoryHelper
from app.log import logger
from app.modules.filemanager import FileManagerModule
from app.plugins import _PluginBase
from app.schemas import NotificationType, TransferInfo, TransferDirectoryConf
from app.schemas.types import EventType, MediaType, SystemConfigKey
from app.utils.string import StringUtils
from app.utils.system import SystemUtils
from app.utils.http import RequestUtils

lock = threading.Lock()


class FileMonitorHandler(FileSystemEventHandler):
    """
    目录监控响应类
    """

    def __init__(self, monpath: str, sync: Any, **kwargs):
        super(FileMonitorHandler, self).__init__(**kwargs)
        self._watch_path = monpath
        self.sync = sync

    def on_created(self, event):
        self.sync.event_handler(
            event=event,
            text="创建",
            mon_path=self._watch_path,
            event_path=event.src_path,
        )

    def on_moved(self, event):
        self.sync.event_handler(
            event=event,
            text="移动",
            mon_path=self._watch_path,
            event_path=event.dest_path,
        )


class DanmuPlay(_PluginBase):
    # 插件名称
    plugin_name = "弹幕下载"
    # 插件描述
    plugin_desc = "监控目录文件变化，自动下载弹幕。"
    # 插件图标
    plugin_icon = "danmu.png"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "dompling"
    # 作者主页
    author_url = "https://github.com/dompling"
    # 插件配置项ID前缀
    plugin_config_prefix = "danmuplay_"
    # 加载顺序
    plugin_order = 4
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _scheduler = None
    transferhis = None
    downloadhis = None
    transferchian = None
    tmdbchain = None
    storagechain = None
    _observer = []
    _enabled = False
    _notify = False
    _onlyonce = False
    _history = False
    _refresh = False
    _cron = None
    filetransfer = None
    mediaChain = None
    _app_id = None
    _app_secret = None
    _size = 0
    # 模式 compatibility/fast
    _mode = "compatibility"
    # 转移方式
    _monitor_dirs = ""
    _exclude_keywords = ""
    _interval: int = 10
    # 存储源目录与目的目录关系
    _dirconf: Dict[str, Optional[Path]] = {}
    # 存储源目录转移方式
    _transferconf: Dict[str, Optional[str]] = {}
    _overwrite_mode: Dict[str, Optional[str]] = {}
    _medias = {}
    # 退出事件
    _event = threading.Event()

    def init_plugin(self, config: dict = None):
        self.transferhis = TransferHistoryOper()
        self.downloadhis = DownloadHistoryOper()
        self.transferchian = TransferChain()
        self.tmdbchain = TmdbChain()
        self.mediaChain = MediaChain()
        self.storagechain = StorageChain()
        self.filetransfer = FileManagerModule()
        # 清空配置
        self._dirconf = {}
        self._transferconf = {}
        self._overwrite_mode = {}

        # 读取配置
        if config:
            self._enabled = config.get("enabled")
            self._notify = config.get("notify")
            self._onlyonce = config.get("onlyonce")
            self._history = config.get("history")
            self._history = config.get("history")
            self._app_id = config.get("app_id")
            self._app_secret = config.get("app_secret")
            self._mode = config.get("mode")
            self._monitor_dirs = config.get("monitor_dirs") or ""
            self._exclude_keywords = config.get("exclude_keywords") or ""
            self._interval = config.get("interval") or 10
            # self._cron = config.get("cron")
            self._size = config.get("size") or 0

        # 停止现有任务
        self.stop_service()

        if self._enabled or self._onlyonce:
            # 定时服务管理器
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            if self._notify:
                # 追加入库消息统一发送服务
                self._scheduler.add_job(self.send_msg, trigger="interval", seconds=15)

            # 读取目录配置
            monitor_dirs = self._monitor_dirs.split("\n")
            if not monitor_dirs:
                return
            for mon_path in monitor_dirs:
                # 目的目录
                if not mon_path:
                    continue

                self._dirconf[mon_path] = mon_path

                # 启用目录监控
                if self._enabled:
                    try:
                        if self._mode == "compatibility":
                            # 兼容模式，目录执行性能降低且NAS不能休眠，但可以兼容挂载的远程共享目录如SMB
                            observer = PollingObserver(timeout=10)
                        else:
                            # 内部处理系统操作类型选择最优解
                            observer = Observer(timeout=10)
                        self._observer.append(observer)
                        observer.schedule(
                            FileMonitorHandler(mon_path, self),
                            path=mon_path,
                            recursive=True,
                        )
                        observer.daemon = True
                        observer.start()
                        logger.info(f"{mon_path} 的实时监控服务启动")
                    except Exception as e:
                        err_msg = str(e)
                        if "inotify" in err_msg and "reached" in err_msg:
                            logger.warn(
                                f"实时监控服务启动出现异常：{err_msg}，请在宿主机上（不是docker容器内）执行以下命令并重启："
                                + """
                                     echo fs.inotify.max_user_watches=524288 | sudo tee -a /etc/sysctl.conf
                                     echo fs.inotify.max_user_instances=524288 | sudo tee -a /etc/sysctl.conf
                                     sudo sysctl -p
                                     """
                            )
                        else:
                            logger.error(f"{mon_path} 启动目实时监控失败：{err_msg}")
                        self.systemmessage.put(
                            f"{mon_path} 启动实时监控失败：{err_msg}"
                        )

            # 运行一次定时服务
            if self._onlyonce:
                logger.info("弹幕下载服务启动，立即运行一次")
                self._scheduler.add_job(
                    name="弹幕下载",
                    func=self.sync_all,
                    trigger="date",
                    run_date=datetime.datetime.now(tz=pytz.timezone(settings.TZ))
                    + datetime.timedelta(seconds=3),
                )
                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

            # 启动定时服务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def __update_config(self):
        """
        更新配置
        """
        self.update_config(
            {
                "enabled": self._enabled,
                "notify": self._notify,
                "onlyonce": self._onlyonce,
                "mode": self._mode,
                "monitor_dirs": self._monitor_dirs,
                "exclude_keywords": self._exclude_keywords,
                "interval": self._interval,
                "history": self._history,
                "size": self._size,
                "refresh": self._refresh,
            }
        )

    @eventmanager.register(EventType.PluginAction)
    def remote_sync(self, event: Event):
        """
        远程全量执行
        """
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "danmu_down":
                return
            self.post_message(
                channel=event.event_data.get("channel"),
                title="开始执行弹幕下载目录 ...",
                userid=event.event_data.get("user"),
            )
        self.sync_all()
        if event:
            self.post_message(
                channel=event.event_data.get("channel"),
                title="弹幕下载目录执行完成！",
                userid=event.event_data.get("user"),
            )

    def sync_all(self):
        """
        立即运行一次，全量执行目录中所有文件
        """
        logger.info("开始全量执行弹幕下载目录 ...")
        # 遍历所有监控目录
        for mon_path in self._dirconf.keys():
            logger.info(f"开始处理监控目录 {mon_path} ...")
            list_files = SystemUtils.list_files(Path(mon_path), settings.RMT_MEDIAEXT)
            logger.info(f"监控目录 {mon_path} 共发现 {len(list_files)} 个文件")
            # 遍历目录下所有文件
            for file_path in list_files:
                logger.info(f"开始处理文件 {file_path} ...")
                self.__handle_file(event_path=str(file_path))
        logger.info("全量执行弹幕下载目录完成！")

    def event_handler(self, event, mon_path: str, text: str, event_path: str):
        """
        处理文件变化
        :param event: 事件
        :param mon_path: 监控目录
        :param text: 事件描述
        :param event_path: 事件文件路径
        """
        if not event.is_directory:
            # 文件发生变化
            logger.debug("文件%s：%s" % (text, event_path))
            self.__handle_file(event_path=event_path, mon_path=mon_path)

    def generate_signature(self, path):
        timestamp = int(time.time())
        data = f"{self._app_id}{timestamp}{path}{self._app_secret}"
        sha256_hash = hashlib.sha256(data.encode()).digest()
        return base64.b64encode(sha256_hash).decode()

    def __get_danmu_play_download(mediainfo: MediaInfo):
        """
        获取后端V2最新版本
        """
        try:
            # 获取所有发布的版本列表
            response = RequestUtils(
                proxies=settings.PROXY, headers=settings.GITHUB_HEADERS
            ).get_res("https://api.github.com/repos/jxxghp/MoviePilot/releases")
            if response:
                releases = [release["tag_name"] for release in response.json()]
                v2_releases = [tag for tag in releases if re.match(r"^v2\.", tag)]
                if not v2_releases:
                    logger.warn("获取v2后端最新版本版本出错！")
                else:
                    # 找到最新的v2版本
                    latest_v2 = sorted(
                        v2_releases, key=lambda s: list(map(int, re.findall(r"\d+", s)))
                    )[-1]
                    logger.info(f"获取到后端最新版本：{latest_v2}")
                    return latest_v2
            else:
                logger.error("无法获取后端版本信息，请检查网络连接或GitHub API请求。")
        except Exception as err:
            logger.error(f"获取后端最新版本失败：{str(err)}")
        return None

    def __handle_file(self, event_path: str):
        """
        执行一个文件
        :param event_path: 事件文件路径
        :param mon_path: 监控目录
        """
        file_path = Path(event_path)
        try:
            if not file_path.exists():
                return
            # 全程加锁
            with lock:
                transfer_history = self.transferhis.get_by_src(event_path)
                if transfer_history:
                    logger.info("文件已处理过：%s" % event_path)
                    return

                # 回收站及隐藏的文件不处理
                if (
                    event_path.find("/@Recycle/") != -1
                    or event_path.find("/#recycle/") != -1
                    or event_path.find("/.") != -1
                    or event_path.find("/@eaDir") != -1
                ):
                    logger.debug(f"{event_path} 是回收站或隐藏的文件")
                    return

                # 命中过滤关键字不处理
                if self._exclude_keywords:
                    for keyword in self._exclude_keywords.split("\n"):
                        if keyword and re.findall(keyword, event_path):
                            logger.info(
                                f"{event_path} 命中过滤关键字 {keyword}，不处理"
                            )
                            return

                # 整理屏蔽词不处理
                transfer_exclude_words = self.systemconfig.get(
                    SystemConfigKey.TransferExcludeWords
                )
                if transfer_exclude_words:
                    for keyword in transfer_exclude_words:
                        if not keyword:
                            continue
                        if keyword and re.search(
                            r"%s" % keyword, event_path, re.IGNORECASE
                        ):
                            logger.info(
                                f"{event_path} 命中整理屏蔽词 {keyword}，不处理"
                            )
                            return

                # 不是媒体文件不处理
                if file_path.suffix not in settings.RMT_MEDIAEXT:
                    logger.debug(f"{event_path} 不是媒体文件")
                    return

                # 元数据
                file_meta = MetaInfoPath(file_path)
                if not file_meta.name:
                    logger.error(f"{file_path.name} 无法识别有效信息")
                    return
                
                # 识别媒体信息
                mediainfo: MediaInfo = self.chain.recognize_media(meta=file_meta)
                if not mediainfo:
                    logger.warn(f"未识别到媒体信息，标题：{file_meta.name}")
                    # 新增转移成功历史记录
                    if self._notify:
                        self.post_message(
                            mtype=NotificationType.Manual,
                            title=f"{file_path.name} 未识别到媒体信息，无法下载弹幕！\n"
                            f"回复：```\n/danmu_download 媒体名称[季]\n``` 手动识别下载。",
                        )
                    return
                print(mediainfo)
                
                
        except Exception as e:
            logger.error("目录监控发生错误：%s - %s" % (str(e), traceback.format_exc()))

    def send_msg(self):
        """
        定时检查是否有媒体处理完，发送统一消息
        """
        if not self._medias or not self._medias.keys():
            return

        # 遍历检查是否已刮削完，发送消息
        for medis_title_year_season in list(self._medias.keys()):
            media_list = self._medias.get(medis_title_year_season)
            logger.info(f"开始处理媒体 {medis_title_year_season} 消息")

            if not media_list:
                continue

            # 获取最后更新时间
            last_update_time = media_list.get("time")
            media_files = media_list.get("files")
            if not last_update_time or not media_files:
                continue

            transferinfo = media_files[0].get("transferinfo")
            file_meta = media_files[0].get("file_meta")
            mediainfo = media_files[0].get("mediainfo")
            # 判断剧集最后更新时间距现在是已超过10秒或者电影，发送消息
            if (datetime.datetime.now() - last_update_time).total_seconds() > int(
                self._interval
            ) or mediainfo.type == MediaType.MOVIE:
                # 发送通知
                if self._notify:

                    # 汇总处理文件总大小
                    total_size = 0
                    file_count = 0

                    # 剧集汇总
                    episodes = []
                    for file in media_files:
                        transferinfo = file.get("transferinfo")
                        total_size += transferinfo.total_size
                        file_count += 1

                        file_meta = file.get("file_meta")
                        if file_meta and file_meta.begin_episode:
                            episodes.append(file_meta.begin_episode)

                    transferinfo.total_size = total_size
                    # 汇总处理文件数量
                    transferinfo.file_count = file_count

                    # 剧集季集信息 S01 E01-E04 || S01 E01、E02、E04
                    season_episode = None
                    # 处理文件多，说明是剧集，显示季入库消息
                    if mediainfo.type == MediaType.TV:
                        # 季集文本
                        season_episode = (
                            f"{file_meta.season} {StringUtils.format_ep(episodes)}"
                        )
                    # 发送消息
                    self.transferchian.send_transfer_message(
                        meta=file_meta,
                        mediainfo=mediainfo,
                        transferinfo=transferinfo,
                        season_episode=season_episode,
                    )
                # 发送完消息，移出key
                del self._medias[medis_title_year_season]
                continue

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [
            {
                "cmd": "/danmu_download",
                "event": EventType.PluginAction,
                "desc": "弹幕下载",
                "category": "",
                "data": {"action": "danmu_download"},
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/danmu_download",
                "endpoint": self.sync,
                "methods": ["GET"],
                "summary": "弹幕下载",
                "description": "弹幕下载",
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        if self._enabled and self._cron:
            return [
                {
                    "id": "danmuplay",
                    "name": "弹幕下载全量执行服务",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.sync_all,
                    "kwargs": {},
                }
            ]
        return []

    def sync(self) -> schemas.Response:
        """
        API调用目录执行
        """
        self.sync_all()
        return schemas.Response(success=True)

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify",
                                            "label": "发送通知",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "立即运行一次",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VForm",
                        "content": [
                            {
                                "component": "VRow",
                                "content": [
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 12, "md": 4},
                                        "content": [
                                            {
                                                "component": "VSwitch",
                                                "props": {
                                                    "model": "history",
                                                    "label": "存储历史记录",
                                                },
                                            }
                                        ],
                                    },
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 12, "md": 4},
                                        "content": [
                                            {
                                                "component": "VSwitch",
                                                "props": {
                                                    "model": "refresh",
                                                    "label": "刷新媒体库",
                                                },
                                            }
                                        ],
                                    },
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 12, "md": 4},
                                        "content": [
                                            {
                                                "component": "VTextField",
                                                "props": {
                                                    "model": "app_id",
                                                    "label": "应用ID",
                                                    "placeholder": "AppId",
                                                },
                                            }
                                        ],
                                    },
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 12, "md": 4},
                                        "content": [
                                            {
                                                "component": "VTextField",
                                                "props": {
                                                    "model": "app_secret",
                                                    "label": "应用密钥",
                                                    "placeholder": "AppSecret",
                                                },
                                            }
                                        ],
                                    },
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "mode",
                                            "label": "监控模式",
                                            "items": [
                                                {
                                                    "title": "兼容模式",
                                                    "value": "compatibility",
                                                },
                                                {"title": "性能模式", "value": "fast"},
                                            ],
                                        },
                                    },
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "monitor_dirs",
                                            "label": "监控目录",
                                            "rows": 5,
                                            "placeholder": "每一行一个目录",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {
                                    "cols": 12,
                                },
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "exclude_keywords",
                                            "label": "排除关键词",
                                            "rows": 2,
                                            "placeholder": "每一行一个关键词",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {
                                    "cols": 12,
                                },
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "AppId 和 AppSecret 请到官网申请 https://doc.dandanplay.com/open/#_3-%E7%94%B3%E8%AF%B7-appid-%E5%92%8C-appsecret",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": False,
            "onlyonce": False,
            "history": False,
            "refresh": True,
            "mode": "fast",
            "monitor_dirs": "",
            "exclude_keywords": "",
            "interval": 10,
            "app_id": "",
            "app_secret": "",
            "cron": "",
            "size": 0,
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        if self._observer:
            for observer in self._observer:
                try:
                    observer.stop()
                    observer.join()
                except Exception as e:
                    print(str(e))
        self._observer = []
        if self._scheduler:
            self._scheduler.remove_all_jobs()
            if self._scheduler.running:
                self._event.set()
                self._scheduler.shutdown()
                self._event.clear()
            self._scheduler = None
